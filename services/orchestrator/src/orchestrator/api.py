"""REST API 层（Web 并存入口，V2.1）：job 提交/状态/产物/复测/审核回流。

只做协议转换与校验，业务下沉 service（.rules/python-code-style）。
口径：
- POST /api/v1/jobs：提交全流程 job（文档 base64，可多份 C-10 + 仓库配置 + 被测服务地址），
  后台 asyncio 任务驱动 PipelineRunner（F-ORCH-01 同一入口，CLI 与 Web 等价）；
- 断点续跑（F-ORCH-02）：resume=true（默认）保留历史产物；false 时清空重跑；
- 产物读取白名单：仅 9 节点产物 + 调用留痕（llm_calls/review_log）；
- 单条复测（07 §3.5）：retest_defects 核心（与节点同源判定）；
- 人工审核（F-REVW-06）：review_log.jsonl 追加；驳回回流至生成节点
  （force_node=testcase_generate，旧产物归档留痕）——人工审核不在 9 节点口径内（D6/D7）。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import logging
import os
import shutil
import socket
import time
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from orchestrator.artifact_lifecycle import find_expired_jobs, purge_quarantine, quarantine_jobs
from orchestrator.artifacts import ArtifactStore, ensure_job_id
from orchestrator.config import Settings
from orchestrator.engine import NODE_ARTIFACTS, Engine
from orchestrator.errors import PipelineError
from orchestrator.javaparser_client import JavaParserClient
from orchestrator.job_queue import LocalJobQueue, QueueConflict
from orchestrator.metrics import MetricsRegistry
from orchestrator.nodes.auto_execute import login_for_headers
from orchestrator.nodes.retest_regression import retest_defects
from orchestrator.object_storage import ObjectStorageError, build_object_storage
from orchestrator.persistence import Database
from orchestrator.pipeline import NODE_ORDER, PipelineState
from orchestrator.repository_resolver import resolve_repository_inputs
from orchestrator.runner import PipelineOutcome, PipelineRunner
from orchestrator.security import (
    AccessPolicy,
    AccessPrincipal,
    SecretStore,
    SecretStoreError,
    redact_secrets,
    write_private_json,
)

logger = logging.getLogger(__name__)

# 产物读取白名单（9 节点产物 + 留痕文件；job_input.json 属服务内部元数据不外露）
_ARTIFACT_WHITELIST: frozenset[str] = frozenset(
    name for names in NODE_ARTIFACTS.values() for name in names
) | {"requirement_diff.json", "llm_calls.jsonl", "review_log.jsonl"}

_DOC_SUFFIXES = frozenset({".md", ".docx", ".xlsx"})

# C-12 限额：文档数 / 单文档解码后字节 / 请求体字节（base64 膨胀约 4/3）
MAX_DOCS = 10
MAX_DOC_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024 * 1024
AUTH_SESSION_TTL_SECONDS = 10 * 60
MAX_PENDING_AUTH_SESSIONS = 256
MAX_IN_MEMORY_OUTCOMES = 512


class RepoConfig(BaseModel):
    """仓库配置（支持本地目录或内部 Git 地址）。"""

    name: str
    source: Literal["local", "git"] = "local"
    path: str = ""
    remote_url: str | None = None
    branch: str = "main"
    commit: str | None = None
    baseline_branch: str | None = None  # C-09：基线分支（可选，指定则产出变更清单）
    is_fallback: bool = False
    include_paths: list[str] = Field(default_factory=list)
    # C-13：仓库语言（java → java-parser；python → 内置 AST 扫描器）
    language: str = "java"
    # C-07：同一任务多服务时，接口执行按 code_location.repo 路由到该地址。
    service_base_url: str | None = None
    access_token: str | None = Field(default=None, max_length=512)


class DocInput(BaseModel):
    """单份需求文档（C-10 多文档；名称 + base64 内容）。"""

    doc_name: str
    doc_content_b64: str


class RequirementDocumentPair(BaseModel):
    """一行需求版本输入：被测文档必填，基线文档可选。"""

    target: DocInput
    baseline: DocInput | None = None


class AuthConfig(BaseModel):
    """鉴权配置（C-15：D8 升版——fixtures 优先、表单为兜底）。

    auto_execute/retest_regression 执行前 POST login_url 登录，
    token → Authorization: Token；无 token 则回带 Set-Cookie。
    数值/凭据不经 LLM（红线）；insecure_tls 用于 https 自签场景
    （被测环境常为自签证书，默认跳过校验，表单可关）。
    """

    login_url: str
    username: str
    password: str
    insecure_tls: bool = True


class AuthVerificationRequest(AuthConfig):
    """配置页鉴权预检；Token/Cookie 只驻留服务端内存。"""

    job_id: str | None = None


class LlmConfig(BaseModel):
    """本次任务的 LLM 中转站运行时配置；密钥只驻留后台任务内存。"""

    api_key: str = Field(min_length=8, max_length=512)
    model: str = Field(default="MiniMax-M3", min_length=1, max_length=128)
    base_url: str = Field(default="https://api.minimaxi.com/v1", min_length=8, max_length=256)
    max_tokens: int = Field(default=16384, ge=1024, le=16384)


class E2eConfig(BaseModel):
    """可选浏览器功能测试配置；默认不生成、不执行，保持 API 流程不变。"""

    base_url: str
    generate_cases: bool = False
    execute_cases: bool = False
    insecure_tls: bool = True  # 测试环境常用自签名证书，默认跳过浏览器证书校验
    auth_mode: Literal["reuse", "browser"] = "browser"


class FixtureStep(BaseModel):
    """可审计的接口前置动作（造数/清理）；不经 LLM。"""

    name: str = Field(min_length=1, max_length=128)
    phase: Literal["setup", "teardown"] = "setup"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    path: str = Field(min_length=1, max_length=512, pattern=r"^/")
    query: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)
    expected: str = Field(default="status_class=2xx", min_length=1, max_length=512)
    required: bool = True


class JobSubmitRequest(BaseModel):
    """job 提交请求（文档以 base64 传递，免 python-multipart 依赖）。

    C-10：docs 列表支持多份；旧单文档字段（doc_name/doc_content_b64）
    仍接受（兼容旧客户端），两组二选一、docs 优先。
    """

    job_id: str | None = None
    docs: list[DocInput] = Field(default_factory=list)
    document_pairs: list[RequirementDocumentPair] = Field(default_factory=list)
    doc_name: str | None = None  # C-10 前旧字段（兼容保留）
    doc_content_b64: str | None = None  # C-10 前旧字段（兼容保留）
    repos: list[RepoConfig] = Field(min_length=1)  # C-10：≥1 个仓库（原 ≥2 放宽）
    rest_url: str
    # REST 证书策略独立于鉴权；被测内网常见自签，默认跳过但可由用户关闭。
    rest_insecure_tls: bool = True
    resume: bool = True
    # C-15：鉴权配置（可选；填写则执行前登录被测服务注入凭据头）
    auth: AuthConfig | None = None
    auth_session_id: str | None = Field(default=None, max_length=64)
    llm: LlmConfig | None = None
    e2e: E2eConfig | None = None
    fixtures: list[FixtureStep] = Field(default_factory=list)


class PreflightRequest(JobSubmitRequest):
    """配置预检请求：复用提交字段，但不创建 job、不写业务产物。"""


class EditableAuthConfig(BaseModel):
    """编辑任务时的鉴权配置；空密码表示保留原任务的密码，绝不回传。"""

    login_url: str
    username: str
    password: str | None = Field(default=None, max_length=256)
    insecure_tls: bool = True


class EditableLlmConfig(BaseModel):
    """编辑任务时的模型配置；API Key 可省略以复用 SecretStore 中的密钥。"""

    api_key: str | None = Field(default=None, min_length=8, max_length=512)
    model: str = Field(default="MiniMax-M3", min_length=1, max_length=128)
    base_url: str = Field(default="https://api.minimaxi.com/v1", min_length=8, max_length=256)
    max_tokens: int = Field(default=16384, ge=1024, le=16384)


class JobConfigurationUpdateRequest(BaseModel):
    """完整任务配置编辑请求。

    未上传 docs 时保留当前需求文档；鉴权密码与模型 Key 为空时复用 SecretStore
    中的值，接口从不读取或返回它们。replace_fixtures 允许编辑页明确清空夹具。
    """

    docs: list[DocInput] = Field(default_factory=list)
    document_pairs: list[RequirementDocumentPair] = Field(default_factory=list)
    keep_existing_docs: bool = True
    repos: list[RepoConfig] = Field(min_length=1)
    rest_url: str
    rest_insecure_tls: bool = True
    resume: bool = True
    run_after_save: bool = True
    # None 保持旧客户端语义；False 明确切换为服务级 LLM 配置并清除任务级覆盖。
    llm_enabled: bool | None = None
    auth: EditableAuthConfig | None = None
    auth_session_id: str | None = Field(default=None, max_length=64)
    llm: EditableLlmConfig | None = None
    e2e: E2eConfig | None = None
    fixtures: list[FixtureStep] = Field(default_factory=list)
    replace_fixtures: bool = False


class RetestRequest(BaseModel):
    """复测请求：defect_id 为空 → 全量（F-DEF-02）；指定 → 单条（07 §3.5）。"""

    defect_id: str | None = None


class ReviewLogRequest(BaseModel):
    """人工审核（F-REVW-06）：approved 仅记录；rejected 回流至生成节点。"""

    decision: str  # approved | rejected
    reviewer: str = "web-user"
    note: str = ""


class RerunRequest(BaseModel):
    """重跑请求；可覆盖非敏感执行目标，不暴露既有鉴权或模型密钥。"""

    from_node: str | None = None
    llm: LlmConfig | None = None
    rest_url: str | None = None
    e2e: E2eConfig | None = None


def _resolve_local_repo_path(path: str, repositories_root: str | None = None) -> Path:
    """将服务端仓库根目录下的相对目录解析为安全的绝对路径。"""
    raw = Path(path.strip())
    if raw.is_absolute():
        return raw
    if not repositories_root:
        raise HTTPException(status_code=400, detail="本地仓库必须填写绝对路径")
    root = Path(repositories_root).resolve()
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="仓库路径不能逸出服务端源码根目录") from exc
    return candidate


def _repo_metadata(repo: RepoConfig, repositories_root: str | None = None) -> dict[str, Any]:
    """任务输入只保留可审计的仓库元数据，不落盘访问令牌。"""
    payload = repo.model_dump(exclude={"access_token"})
    # 仅记录配置状态，不记录令牌本身；用于重启后配置页的脱敏展示。
    payload["access_token_configured"] = bool((repo.access_token or "").strip())
    if payload.get("source") == "local":
        payload["path"] = str(_resolve_local_repo_path(repo.path, repositories_root))
        payload.pop("remote_url", None)
        payload.pop("commit", None)
    return payload


def _repo_git_tokens(repositories: list[RepoConfig]) -> dict[str, str]:
    """把仓库访问令牌按名称/地址映射给 SecretStore。"""
    tokens: dict[str, str] = {}
    for repo in repositories:
        token = (repo.access_token or "").strip()
        if token and repo.source == "git":
            tokens[repo.name] = token
            if repo.remote_url:
                tokens[repo.remote_url] = token
    return tokens


def _validate_repo_config(repo: RepoConfig, repositories_root: str | None = None) -> None:
    """提交前验证本地/远程仓库的互斥字段和协议。"""
    if repo.language not in ("java", "python"):
        raise HTTPException(
            status_code=400,
            detail=f"仓库 {repo.name} 的 language 仅支持 java/python（C-13）：{repo.language}",
        )
    if repo.service_base_url is not None and not repo.service_base_url.startswith(
        ("http://", "https://")
    ):
        raise HTTPException(
            status_code=400,
            detail=f"仓库 {repo.name} 的 service_base_url 须为 http(s) 地址（C-07）",
        )
    if repo.source == "local":
        if not repo.path.strip():
            raise HTTPException(status_code=400, detail=f"仓库 {repo.name} 的本地 path 不能为空")
        repo_path = _resolve_local_repo_path(repo.path, repositories_root)
        if not repo_path.is_dir():
            raise HTTPException(
                status_code=400,
                detail=f"仓库 {repo.name} 路径不存在或不是目录：{repo.path}",
            )
        return
    if not repo.remote_url or not (
        repo.remote_url.startswith(("http://", "https://", "ssh://", "git://", "file://"))
        or repo.remote_url.startswith("git@")
    ):
        raise HTTPException(
            status_code=400,
            detail=f"仓库 {repo.name} 需要合法的 Git remote_url（https/ssh/git/file）",
        )


def create_app(settings: Settings, runner: PipelineRunner | None = None) -> FastAPI:
    """构建 FastAPI 应用实例（runner 可注入：测试替身）。"""
    app = FastAPI(title="LLM-AutoTest orchestrator", version="0.2.0")
    app.state.settings = settings
    app.state.metrics = MetricsRegistry()
    object_storage = build_object_storage(settings)
    database = Database(settings.database_url) if settings.database_url else None
    if database is not None and settings.database_auto_create:
        database.migrate_schema()
    orch_store = ArtifactStore(
        Path(settings.artifacts_root),
        Path(settings.schemas_dir),
        "orchestrator",
        object_storage=object_storage,
        database=database,
    )
    te_store = ArtifactStore(
        Path(settings.artifacts_root),
        Path(settings.schemas_dir),
        "test-executor",
        object_storage=object_storage,
        database=database,
    )
    app.state.orch_store = orch_store
    app.state.te_store = te_store
    app.state.object_storage = object_storage
    app.state.secret_store = SecretStore(
        Path(settings.artifacts_root), settings.secrets_encryption_key
    )
    app.state.access_policy = AccessPolicy(
        settings.access_tokens_json, single_user_mode=settings.single_user_mode
    )
    app.state.engine = Engine(orch_store)
    app.state.database = database
    worker_id = os.environ.get("WORKER_ID") or (
        f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    app.state.worker_id = worker_id
    app.state.job_queue = LocalJobQueue(
        settings.job_max_concurrency,
        database=database,
        worker_id=worker_id,
        lease_seconds=settings.job_lease_seconds,
    )
    # 保留原有观察入口，实际提交由队列统一管理。
    app.state.job_tasks = app.state.job_queue.tasks
    app.state.job_outcomes: dict[str, PipelineOutcome | dict[str, Any]] = {}  # type: ignore[misc]
    app.state.job_llm_configs = {}  # 仅运行期，禁止落盘 API key
    app.state.job_repo_tokens: dict[str, dict[str, str]] = {}  # type: ignore[misc]
    app.state.job_auth_headers: dict[str, dict[str, str]] = {}  # type: ignore[misc]
    app.state.pending_auth_sessions: dict[str, dict[str, Any]] = {}  # type: ignore[misc]
    app.state.artifact_cleanup_task = None

    @app.middleware("http")
    async def _audit_mutating_requests(request: Request, call_next: Any) -> Any:
        """将成功或业务失败的写操作留痕；不记录请求体，避免凭据进入审计。"""
        response = await call_next(request)
        if request.url.path != "/metrics":
            route = getattr(request.scope.get("route"), "path", request.url.path)
            app.state.metrics.record_request(request.method, str(route), response.status_code)
        if (
            app.state.database is not None
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and request.url.path.startswith("/api/v1/")
            and response.status_code < 500
        ):
            parts = request.url.path.strip("/").split("/")
            job_id = parts[3] if len(parts) >= 4 and parts[0:3] == ["api", "v1", "jobs"] else None
            if job_id:
                principal = getattr(request.state, "access_principal", None)
                created_by = principal.user_id if isinstance(principal, AccessPrincipal) else None
                try:
                    await asyncio.to_thread(
                        app.state.database.append_event,
                        job_id,
                        f"audit_{request.method.lower()}",
                        payload={
                            "path": request.url.path,
                            "status_code": response.status_code,
                        },
                        created_by=created_by,
                    )
                except Exception:  # pragma: no cover - 审计故障不阻断业务响应
                    logger.exception("操作审计写入失败：%s", request.url.path)
        return response

    recovered = _recover_interrupted_jobs(app)
    if recovered:
        logger.warning("服务启动恢复中断任务：%s", recovered)
    if runner is None:
        java_client = JavaParserClient(base_url=settings.java_parser_url)
        runner = PipelineRunner(
            settings,
            app.state.engine,
            orch_store,
            te_store,
            java_client,
            database=database,
            worker_id=worker_id,
            lease_seconds=settings.job_lease_seconds,
        )
    app.state.runner = runner

    async def _recover_database_jobs() -> None:
        """启动后把无有效租约的历史任务重新交给本实例队列。"""
        if app.state.database is None:
            return
        try:
            job_ids = await asyncio.to_thread(app.state.database.list_recoverable_jobs)
        except Exception as exc:  # pragma: no cover - 未迁移/数据库暂不可用由 /ready 报告
            logger.warning("启动恢复暂不可用，等待数据库迁移或恢复：%s", exc)
            return
        for job_id in job_ids:
            try:
                _spawn_run(app, job_id)
                logger.info("恢复历史任务并重新入队：%s", job_id)
            except (HTTPException, PipelineError, OSError) as exc:
                logger.warning("历史任务恢复失败（%s）：%s", job_id, exc)

    app.router.on_startup.append(_recover_database_jobs)

    async def _artifact_maintenance_loop() -> None:
        """执行产物回收和审计保留清理；两个策略均关闭时不启动。"""
        interval = max(
            60,
            min(settings.artifact_cleanup_interval_s, settings.audit_cleanup_interval_s),
        )
        while True:
            moved: list[str] = []
            removed: list[str] = []
            if settings.artifact_retention_days > 0:
                active_job_ids = {
                    job_id for job_id, task in app.state.job_tasks.items() if not task.done()
                }
                candidates = await asyncio.to_thread(
                    find_expired_jobs,
                    Path(settings.artifacts_root),
                    retention_days=settings.artifact_retention_days,
                    active_job_ids=active_job_ids,
                )
                moved = await asyncio.to_thread(
                    quarantine_jobs,
                    Path(settings.artifacts_root),
                    candidates,
                    active_job_ids=active_job_ids,
                )
                if app.state.database is not None:
                    for job_id in moved:
                        try:
                            await asyncio.to_thread(app.state.database.archive_job, job_id)
                        except Exception as exc:  # pragma: no cover
                            logger.warning(
                                "任务已进入回收区但数据库标记失败（%s）：%s", job_id, exc
                            )
                removed = await asyncio.to_thread(
                    purge_quarantine,
                    Path(settings.artifacts_root),
                    grace_days=settings.artifact_trash_grace_days,
                )
            purged_events = 0
            if app.state.database is not None and settings.audit_retention_days > 0:
                cutoff = datetime.now(UTC) - timedelta(days=settings.audit_retention_days)
                try:
                    purged_events = await asyncio.to_thread(
                        app.state.database.purge_events_before, cutoff
                    )
                except Exception as exc:  # pragma: no cover
                    logger.warning("审计事件清理失败：%s", exc)
            if moved or removed or purged_events:
                logger.info(
                    "后台保留清理：回收任务 %d 个，清除批次 %d 个，审计事件 %d 条",
                    len(moved),
                    len(removed),
                    purged_events,
                )
            await asyncio.sleep(interval)

    async def _start_artifact_maintenance() -> None:
        if settings.artifact_retention_days > 0 or settings.audit_retention_days > 0:
            app.state.artifact_cleanup_task = asyncio.create_task(
                _artifact_maintenance_loop(), name="artifact-maintenance"
            )

    app.router.on_startup.append(_start_artifact_maintenance)

    async def _shutdown_job_queue() -> None:
        cleanup_task = app.state.artifact_cleanup_task
        if cleanup_task is not None and not cleanup_task.done():
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
        await app.state.job_queue.shutdown()

    # 直接挂到 Starlette 路由生命周期，避免 FastAPI on_event 的弃用告警。
    app.router.on_shutdown.append(_shutdown_job_queue)

    # C-12：ArtifactStore/节点层拍出的 PipelineError 统一映射 400（协议层不炸 500）
    @app.exception_handler(PipelineError)
    async def _pipeline_error_handler(_request: Any, exc: PipelineError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": exc.message})

    # C-12：请求体大小限制（超大提交在解析前拒绝，色 413）
    @app.middleware("http")
    async def _limit_request_body(request: Any, call_next: Any) -> Any:
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > MAX_REQUEST_BYTES:
            limit_mb = MAX_REQUEST_BYTES // (1024 * 1024)
            return JSONResponse(
                status_code=413,
                content={"detail": f"请求体超过上限 {limit_mb} MB（C-12）"},
            )
        return await call_next(request)

    @app.middleware("http")
    async def _access_control(request: Request, call_next: Any) -> Any:
        """可选部门令牌校验；健康检查和本地兼容模式不受影响。"""
        if request.url.path.startswith("/api/v1/") and request.url.path != "/api/v1/system/access-mode":
            principal = app.state.access_policy.authenticate(request.headers)
            if principal is None:
                return JSONResponse(status_code=401, content={"detail": "访问令牌无效或缺失"})
            request.state.access_principal = principal
        return await call_next(request)

    # ---- 基础端点 ----

    @app.get("/api/v1/system/access-mode")
    async def access_mode() -> dict[str, str]:
        """公开返回入口模式，让前端不在单用户部署中展示技术令牌输入。"""
        return {
            "mode": "single_user" if not app.state.access_policy.enabled else "token",
            "user": "admin" if settings.single_user_mode else "",
        }

    @app.get("/health")
    async def health() -> dict[str, str]:
        """健康检查（docker-compose 编排依赖）。"""
        return {"status": "ok"}

    @app.get("/ready")
    async def readiness() -> Any:
        """就绪检查：进程存活之外，确认数据库可用且 Schema 已迁移。"""
        database: Database | None = app.state.database
        database_ready = True
        schema_ready = True
        schema_version: str | None = None
        object_storage_ready = True
        if database is not None:
            try:
                await asyncio.to_thread(database.healthcheck)
                schema_version = await asyncio.to_thread(database.schema_version)
                schema_ready = schema_version == Database.CURRENT_SCHEMA_VERSION
            except Exception:  # pragma: no cover - 驱动/网络异常由部署环境触发
                database_ready = False
                schema_ready = False
        object_storage = app.state.object_storage
        if object_storage is not None:
            try:
                await asyncio.to_thread(object_storage.healthcheck)
            except Exception:  # pragma: no cover - 对象存储网络/权限由部署环境触发
                object_storage_ready = False
        queue = app.state.job_queue
        ready = database_ready and schema_ready and object_storage_ready
        payload = {
            "status": "ready" if ready else "not_ready",
            "database": "ok" if database_ready else "unavailable",
            "schema": {
                "status": (
                    "disabled"
                    if database is None
                    else "ok"
                    if schema_ready
                    else "migration_required"
                    if database_ready
                    else "unavailable"
                ),
                "version": schema_version,
                "expected": Database.CURRENT_SCHEMA_VERSION if database is not None else None,
            },
            "object_storage": (
                "disabled"
                if object_storage is None
                else "ok"
                if object_storage_ready
                else "unavailable"
            ),
            "queue": {
                "active": queue.active_count,
                "queued": queue.queued_count,
            },
        }
        if not ready:
            return JSONResponse(status_code=503, content=payload)
        return payload

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        """Prometheus 抓取端点；不暴露任务内容、凭据或 job_id。"""
        queue = app.state.job_queue
        app.state.metrics.set_gauge("autotest_queue_active_jobs", queue.active_count)
        app.state.metrics.set_gauge("autotest_queue_queued_jobs", queue.queued_count)
        database: Database | None = app.state.database
        database_ready = 1.0
        schema_ready = 1.0
        object_storage_ready = 1.0
        if database is not None:
            try:
                await asyncio.to_thread(database.healthcheck)
                schema_ready = float(
                    await asyncio.to_thread(database.schema_version)
                    == Database.CURRENT_SCHEMA_VERSION
                )
            except Exception:
                database_ready = 0.0
                schema_ready = 0.0
        object_storage = app.state.object_storage
        if object_storage is not None:
            try:
                await asyncio.to_thread(object_storage.healthcheck)
            except Exception:
                object_storage_ready = 0.0
        app.state.metrics.set_gauge("autotest_database_up", database_ready)
        app.state.metrics.set_gauge("autotest_schema_ready", schema_ready)
        app.state.metrics.set_gauge("autotest_object_storage_up", object_storage_ready)
        return PlainTextResponse(
            content=app.state.metrics.render(),
            media_type="text/plain; version=0.0.4",
        )

    @app.post("/api/v1/system/pick-directory")
    async def pick_local_directory() -> dict[str, str]:
        """打开运行 orchestrator 的本机目录选择框（适合 Windows 本地部署）。"""
        try:
            selected = await asyncio.to_thread(_pick_native_directory)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="本机目录选择不可用；请在运行服务的机器上手动填写绝对路径",
            ) from exc
        if not selected:
            raise HTTPException(status_code=400, detail="未选择目录")
        return {"path": selected, "name": Path(selected).name}

    @app.get("/api/v1/system/repository-root")
    async def repository_root(http_request: Request) -> dict[str, Any]:
        """返回服务端源码根目录，供目录选择后自动拼接可执行路径。"""
        _request_scope(app, http_request)
        root = Path(settings.repositories_root)
        return {"root": str(root), "exists": root.is_dir()}

    @app.post("/api/v1/auth/verify")
    async def verify_authentication(
        request: AuthVerificationRequest, http_request: Request
    ) -> dict[str, Any]:
        """配置阶段真实登录；凭据头仅留在当前服务进程，绝不返回浏览器。"""
        auth_payload = request.model_dump(exclude={"job_id"})
        try:
            headers = await login_for_headers(auth_payload)
        except PipelineError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        now = time.monotonic()
        # 只保留短期未消费的会话，避免用户反复点击验证导致服务进程内存增长。
        app.state.pending_auth_sessions = {
            key: value
            for key, value in app.state.pending_auth_sessions.items()
            if now - float(value.get("created_at", now)) < AUTH_SESSION_TTL_SECONDS
        }
        if len(app.state.pending_auth_sessions) >= MAX_PENDING_AUTH_SESSIONS:
            oldest = min(
                app.state.pending_auth_sessions,
                key=lambda key: float(
                    app.state.pending_auth_sessions[key].get("created_at", now)
                ),
            )
            app.state.pending_auth_sessions.pop(oldest, None)
        session_id = uuid.uuid4().hex
        app.state.pending_auth_sessions[session_id] = {
            "headers": headers,
            "fingerprint": _auth_fingerprint(auth_payload),
            "created_at": now,
        }
        if request.job_id:
            _require_job_access(app, request.job_id, http_request)
            app.state.job_auth_headers[request.job_id] = headers
        return {
            "authenticated": True,
            "session_id": session_id,
            "credential_type": "token" if "Authorization" in headers else "cookie",
        }

    @app.post("/api/v1/preflight")
    async def preflight_job(request: PreflightRequest, http_request: Request) -> dict[str, Any]:
        """在提交前检查文档、仓库、REST、鉴权和模型配置。"""
        _request_scope(app, http_request)
        checks: list[dict[str, str]] = []
        try:
            docs: list[DocInput] = []
            if request.document_pairs:
                for pair in request.document_pairs:
                    docs.append(pair.target)
                    if pair.baseline is not None:
                        docs.append(pair.baseline)
            else:
                docs = _request_docs(request)
            _validate_doc_inputs(docs)
            checks.append(
                {"name": "documents", "status": "ok", "message": f"已检查 {len(docs)} 份文档"}
            )
        except HTTPException as exc:
            checks.append({"name": "documents", "status": "error", "message": str(exc.detail)})

        repo_error: str | None = None
        try:
            for repo in request.repos:
                _validate_repo_config(repo, settings.repositories_root)
            repo_tokens = _repo_git_tokens(request.repos)
            await asyncio.to_thread(
                resolve_repository_inputs,
                [_repo_metadata(repo, settings.repositories_root) for repo in request.repos],
                cache_root=Path(settings.artifacts_root),
                git_tokens=repo_tokens or None,
            )
        except (HTTPException, PipelineError) as exc:
            repo_error = exc.detail if isinstance(exc, HTTPException) else exc.message
        if repo_error:
            checks.append({"name": "repositories", "status": "error", "message": repo_error})
        else:
            checks.append({"name": "repositories", "status": "ok", "message": "源码来源可用"})

        if request.rest_url.startswith(("http://", "https://")):
            checks.append({"name": "rest", "status": "ok", "message": "REST 地址格式正确"})
        else:
            checks.append({"name": "rest", "status": "error", "message": "REST 地址须为 http(s)"})

        if request.auth is not None:
            try:
                await login_for_headers(request.auth.model_dump())
                checks.append({"name": "auth", "status": "ok", "message": "鉴权登录成功"})
            except PipelineError as exc:
                checks.append({"name": "auth", "status": "error", "message": exc.message})
        else:
            checks.append({"name": "auth", "status": "skipped", "message": "未配置鉴权"})

        if request.llm is not None and request.llm.base_url.startswith(("http://", "https://")):
            checks.append({"name": "llm", "status": "ok", "message": "LLM 中转站配置格式正确"})
        elif request.llm is not None:
            checks.append({"name": "llm", "status": "error", "message": "模型地址须为 http(s)"})
        else:
            checks.append(
                {
                    "name": "llm",
                    "status": "skipped",
                    "message": "未填写任务级 LLM，将使用服务级配置",
                }
            )
        return {
            "ready": all(item["status"] in {"ok", "skipped"} for item in checks),
            "checks": checks,
        }

    @app.get("/api/v1/pipeline/nodes")
    async def list_nodes() -> dict[str, object]:
        """9 节点清单与当前状态（R-MTR-01 九节点固定口径）。"""
        state = PipelineState.initial()
        return {"nodes": [state.node_summary(name) for name in NODE_ORDER]}

    # ---- job 管理 ----

    @app.post("/api/v1/jobs", status_code=202)
    async def submit_job(request: JobSubmitRequest, http_request: Request) -> dict[str, Any]:
        """提交全流程 job：校验 → 落盘输入 → 后台驱动 PipelineRunner。"""
        owner_id, project_id = _request_scope(app, http_request)
        await _enforce_submission_quota(app, http_request, owner_id, project_id)
        job_id = request.job_id or f"job-web-{uuid.uuid4().hex[:12]}"
        # C-12：完整正则校验（ensure_job_id 同源；不合规 400，免产物校验级联降级）
        try:
            ensure_job_id(job_id)
        except PipelineError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        if await _task_alive_async(app, job_id):
            raise HTTPException(status_code=409, detail=f"job {job_id} 正在运行中")
        if request.resume and (orch_store.job_dir(job_id) / "state.json").is_file():
            raise HTTPException(
                status_code=409,
                detail=f"job {job_id} 已存在，请使用重跑或任务配置页，避免重复提交覆盖结果",
            )
        if not request.resume:
            # 断点续跑开关关闭 → 清空既有 job 目录重跑（F-ORCH-02 反向口径）。
            # 须在 _save_docs 之前：否则会把刚落盘的文档连同目录一起删掉（P-11）。
            existing = orch_store.job_dir(job_id)
            if existing.is_dir():
                shutil.rmtree(existing)
        document_pairs = _save_document_pairs(orch_store, job_id, request.document_pairs)
        doc_paths = (
            [Path(str(pair["target_path"])) for pair in document_pairs]
            if document_pairs
            else _save_docs(orch_store, job_id, _request_docs(request))
        )
        for repo in request.repos:
            _validate_repo_config(repo, settings.repositories_root)
        if not request.rest_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="rest_url 须为 http(s) 地址")
        if request.e2e is not None and not request.e2e.base_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="e2e.base_url 须为 http(s) 地址")
        llm_payload: dict[str, Any] | None = None
        if request.llm is not None:
            if not request.llm.base_url.startswith(("http://", "https://")):
                raise HTTPException(status_code=400, detail="llm.base_url 须为 http(s) 地址")
            llm_payload = request.llm.model_dump()
        auth_payload: dict[str, Any] | None = None
        if request.auth is not None:
            auth = request.auth
            if not auth.login_url.startswith(("http://", "https://")):
                raise HTTPException(
                    status_code=400, detail="auth.login_url 须为 http(s) 地址（C-15）"
                )
            if not auth.username or not auth.password:
                raise HTTPException(
                    status_code=400,
                    detail="auth.username/password 不能为空（C-15）",
                )
            # 凭据长度上限（防滥用；加密存储启用时不进入 job_input.json）
            if len(auth.username) > 128 or len(auth.password) > 256:
                raise HTTPException(
                    status_code=400,
                    detail="auth.username/password 超长（C-15：用户名≤128、密码≤256）",
                )
            auth_payload = auth.model_dump()
            headers = _take_verified_auth_headers(app, auth_payload, request.auth_session_id)
            if headers:
                app.state.job_auth_headers[job_id] = headers

        job_input: dict[str, Any] = {
            "job_id": job_id,
            "doc_paths": [str(path) for path in doc_paths],
            "repos": [_repo_metadata(repo, settings.repositories_root) for repo in request.repos],
            "rest_url": request.rest_url,
            "rest_insecure_tls": request.rest_insecure_tls,
            "owner_id": owner_id,
            "project_id": project_id,
            "submitted_at": _now_iso(),
        }
        if document_pairs:
            job_input["document_pairs"] = document_pairs
        if auth_payload is not None:
            if app.state.secret_store.enabled:
                job_input["auth"] = {
                    "login_url": auth_payload["login_url"],
                    "username": auth_payload["username"],
                    "insecure_tls": auth_payload["insecure_tls"],
                    "password_configured": True,
                }
            else:
                # 本地兼容模式：未配置 Secret Store 时保持旧任务可重跑语义；
                # 生产部署应配置 SECRETS_ENCRYPTION_KEY。
                job_input["auth"] = auth_payload
        if llm_payload is not None:
            # 仅保存可审计的非敏感模型选择；api_key 绝不写入 job_input/产物/调用留痕。
            job_input["llm"] = {
                "model": llm_payload["model"],
                "base_url": llm_payload["base_url"],
                "max_tokens": llm_payload["max_tokens"],
                "api_key_configured": True,
            }
        if request.e2e is not None:
            job_input["e2e"] = request.e2e.model_dump()
        if request.fixtures:
            job_input["fixtures"] = [fixture.model_dump() for fixture in request.fixtures]
        _persist_job_secrets(
            app,
            job_id,
            auth=auth_payload,
            llm_api_key=llm_payload.get("api_key") if llm_payload else None,
            git_tokens=_repo_git_tokens(request.repos),
        )
        repo_tokens = _repo_git_tokens(request.repos)
        if repo_tokens:
            app.state.job_repo_tokens[job_id] = repo_tokens
        orch_store.job_dir(job_id).mkdir(parents=True, exist_ok=True)
        write_private_json(orch_store.job_dir(job_id) / "job_input.json", job_input)
        if llm_payload is not None:
            app.state.job_llm_configs[job_id] = llm_payload
        if app.state.database is not None:
            await asyncio.to_thread(
                app.state.database.ensure_job,
                job_id,
                created_by=owner_id,
                project_id=project_id,
            )
            await asyncio.to_thread(
                app.state.database.save_job_config,
                job_id,
                job_input,
                created_by=owner_id,
            )
            await _append_audit_event(
                app,
                http_request,
                job_id,
                "job_submitted",
                {"project_id": project_id},
            )
        _spawn_run(app, job_id)
        return {"job_id": job_id, "status": "queued"}

    @app.get("/api/v1/jobs")
    async def list_jobs(http_request: Request) -> dict[str, Any]:
        """任务清单；配置数据库时以数据库索引为主，文件产物仅补充兼容字段。"""
        owner_id, project_id = _request_scope(app, http_request)
        jobs: list[dict[str, Any]] = []
        if app.state.database is not None:
            records = await asyncio.to_thread(
                app.state.database.list_jobs,
                created_by=owner_id if app.state.access_policy.enabled else None,
                project_id=project_id if app.state.access_policy.enabled else None,
            )
            for record in records:
                job_id = record.job_id
                job_dir = orch_store.job_dir(job_id)
                state = _read_state_if_present(job_dir)
                db_nodes = await asyncio.to_thread(app.state.database.list_nodes, job_id)
                nodes = _merge_node_payloads(state.get("nodes", []), db_nodes)
                artifact_rows = await asyncio.to_thread(app.state.database.list_artifacts, job_id)
                artifact_names = {row.filename for row in artifact_rows}
                jobs.append(
                    {
                        "job_id": job_id,
                        "created_at": _db_datetime_iso(record.created_at),
                        "updated_at": _db_datetime_iso(record.updated_at),
                        "running": record.status in {"queued", "running"}
                        or _task_alive(app, job_id),
                        "completed": sum(1 for n in nodes if n.get("status") == "completed"),
                        "degraded": sum(
                            1 for n in nodes if n.get("status") == "manual_intervention"
                        ),
                        "has_report": "test_report.md" in artifact_names
                        or (job_dir / "test_report.md").is_file(),
                    }
                )
            jobs.sort(key=lambda j: str(j.get("created_at") or ""), reverse=True)
            jobs.sort(key=lambda j: j["running"], reverse=True)
            return {"jobs": jobs}

        # 未配置数据库时保留本地开发/旧任务兼容路径。
        root = Path(settings.artifacts_root)
        if root.is_dir():
            for entry in sorted(root.iterdir()):
                state_file = entry / "state.json"
                if not entry.is_dir() or not state_file.is_file():
                    continue
                try:
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if app.state.access_policy.enabled:
                    try:
                        job_input = _load_job_input(
                            orch_store, str(state.get("job_id", entry.name))
                        )
                    except HTTPException:
                        continue
                    if (
                        job_input.get("owner_id") != owner_id
                        or job_input.get("project_id") != project_id
                    ):
                        continue
                nodes = state.get("nodes", [])
                jobs.append(
                    {
                        "job_id": state.get("job_id", entry.name),
                        "created_at": state.get("created_at"),
                        "updated_at": state.get("updated_at"),
                        "running": _task_alive(app, str(state.get("job_id", entry.name))),
                        "completed": sum(1 for n in nodes if n.get("status") == "completed"),
                        "degraded": sum(
                            1 for n in nodes if n.get("status") == "manual_intervention"
                        ),
                        "has_report": (entry / "test_report.md").is_file(),
                    }
                )
        # 创建时间倒排（最新提交在前）；运行中的任务再稳定置顶（两趟稳定排序叠加）
        jobs.sort(key=lambda j: str(j.get("created_at") or ""), reverse=True)
        jobs.sort(key=lambda j: j["running"], reverse=True)
        return {"jobs": jobs}

    @app.get("/api/v1/jobs/{job_id}")
    async def job_status(job_id: str, http_request: Request) -> dict[str, Any]:
        """job 状态：9 节点状态 + 降级清单 + 产物清单（断点诊断，F-ORCH-02）。"""
        _require_job_access(app, job_id, http_request)
        state = _read_state_if_present(orch_store.job_dir(job_id))
        record = None
        db_nodes: list[Any] = []
        artifacts: list[str]
        if app.state.database is not None:
            record = await asyncio.to_thread(app.state.database.get_job, job_id)
            if record is not None:
                db_nodes = await asyncio.to_thread(app.state.database.list_nodes, job_id)
            artifact_rows = (
                await asyncio.to_thread(app.state.database.list_artifacts, job_id)
                if record is not None
                else []
            )
            artifacts = sorted(
                {
                    row.filename
                    for row in artifact_rows
                    if row.filename in _ARTIFACT_WHITELIST
                }
            )
        else:
            artifacts = [
                f.name
                for f in sorted(orch_store.job_dir(job_id).iterdir())
                if f.is_file() and f.name in _ARTIFACT_WHITELIST
            ]
        nodes = _merge_node_payloads(state.get("nodes", []), db_nodes)
        degraded = state.get("degraded", [])
        if not degraded:
            degraded = [
                {
                    "node": node.get("name"),
                    "reason": (node.get("error") or {}).get("message", "人工介入")
                    if isinstance(node.get("error"), dict)
                    else "人工介入",
                }
                for node in nodes
                if node.get("status") == "manual_intervention"
            ]
        running = (
            record is not None and record.status in {"queued", "running"}
        ) or await _task_alive_async(app, job_id)
        return {
            "job_id": job_id,
            "running": running,
            "updated_at": (
                _db_datetime_iso(record.updated_at)
                if record is not None
                else state.get("updated_at")
            ),
            "nodes": nodes,
            "degraded": degraded,
            "artifacts": artifacts,
            "runtime_error": (
                app.state.job_outcomes.get(job_id, {}).get("error")
                if isinstance(app.state.job_outcomes.get(job_id), dict)
                else (record.error_message if record is not None else None)
            ),
            # 任务输入仅暴露 E2E 开关和地址，不暴露鉴权/模型密钥；供前端动态呈现可选子流程。
            "rest_url": _job_rest_url(orch_store, job_id),
            "e2e": _job_e2e_config(orch_store, job_id),
        }

    @app.get("/api/v1/jobs/{job_id}/configuration")
    async def get_job_configuration(job_id: str, http_request: Request) -> dict[str, Any]:
        """读取可编辑的完整任务配置，敏感字段只返回“是否已配置”。"""
        _require_job_access(app, job_id, http_request)
        job_input = _load_job_input(orch_store, job_id)
        auth = job_input.get("auth")
        llm = job_input.get("llm")
        secrets = _load_job_secrets(app, job_id)
        stored_auth = secrets.get("auth")
        stored_llm_key = secrets.get("llm_api_key")
        stored_git_tokens = secrets.get("git_tokens")
        git_token_map = dict(stored_git_tokens) if isinstance(stored_git_tokens, dict) else {}
        git_token_map.update(app.state.job_repo_tokens.get(job_id, {}))
        doc_paths = job_input.get("doc_paths", [])
        if not doc_paths and job_input.get("doc_path"):
            doc_paths = [job_input["doc_path"]]
        return {
            "job_id": job_id,
            "documents": [Path(str(path)).name for path in doc_paths],
            "document_pairs": _public_document_pairs(job_input),
            "repos": [
                {
                    **repo,
                    "access_token_configured": bool(
                        git_token_map.get(str(repo.get("name", "")))
                        or git_token_map.get(str(repo.get("remote_url", "")))
                        or repo.get("access_token_configured")
                    ),
                }
                for repo in job_input.get("repos", [])
                if isinstance(repo, dict)
            ],
            "rest_url": job_input.get("rest_url", ""),
            "rest_insecure_tls": bool(job_input.get("rest_insecure_tls", True)),
            "resume": bool(job_input.get("resume", True)),
            "auth": (
                {
                    "login_url": auth.get("login_url", ""),
                    "username": auth.get("username", ""),
                    "insecure_tls": bool(auth.get("insecure_tls", True)),
                    "password_configured": bool(
                        auth.get("password")
                        or auth.get("password_configured")
                        or isinstance(stored_auth, dict)
                    ),
                }
                if isinstance(auth, dict)
                else None
            ),
            "llm": (
                {
                    "model": llm.get("model", "MiniMax-M3"),
                    "base_url": llm.get("base_url", "https://api.minimaxi.com/v1"),
                    "max_tokens": llm.get("max_tokens", 16384),
                    "api_key_configured": bool(
                        job_id in app.state.job_llm_configs or stored_llm_key
                        or llm.get("api_key_configured")
                    ),
                }
                if isinstance(llm, dict)
                else None
            ),
            "e2e": job_input.get("e2e") if isinstance(job_input.get("e2e"), dict) else None,
            "fixtures": (
                job_input.get("fixtures")
                if isinstance(job_input.get("fixtures"), list)
                else []
            ),
        }

    @app.put("/api/v1/jobs/{job_id}/configuration", status_code=202)
    async def update_job_configuration(
        job_id: str, request: JobConfigurationUpdateRequest, http_request: Request
    ) -> dict[str, Any]:
        """保存整份任务配置，并从需求解析起归档旧产物后全量重新执行。"""
        _require_job_access(app, job_id, http_request)
        owner_id, project_id = _request_scope(app, http_request)
        if await _task_alive_async(app, job_id):
            raise HTTPException(
                status_code=409, detail=f"job {job_id} 正在运行中，请先停止后再修改配置"
            )
        previous = _load_job_input(orch_store, job_id)
        previous_secrets = _load_job_secrets(app, job_id)
        for repo in request.repos:
            _validate_repo_config(repo, settings.repositories_root)
        if not request.rest_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="rest_url 须为 http(s) 地址")
        if request.e2e is not None and not request.e2e.base_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="e2e.base_url 须为 http(s) 地址")

        auth_payload: dict[str, Any] | None = None
        if request.auth is not None:
            if (
                not request.auth.login_url.startswith(("http://", "https://"))
                or not request.auth.username
            ):
                raise HTTPException(
                    status_code=400, detail="auth.login_url 须为 http(s)，且用户名不能为空"
                )
            old_auth: dict[str, Any] = {}
            if isinstance(previous.get("auth"), dict):
                old_auth.update(previous["auth"])
            if isinstance(previous_secrets.get("auth"), dict):
                old_auth.update(previous_secrets["auth"])
            password = request.auth.password or old_auth.get("password")
            if not password:
                raise HTTPException(
                    status_code=400, detail="请填写鉴权密码（当前任务没有可复用的密码）"
                )
            if len(request.auth.username) > 128 or len(password) > 256:
                raise HTTPException(
                    status_code=400, detail="auth.username/password 超长（用户名≤128、密码≤256）"
                )
            auth_payload = {**request.auth.model_dump(), "password": password}
            headers = _take_verified_auth_headers(app, auth_payload, request.auth_session_id)
            if headers:
                app.state.job_auth_headers[job_id] = headers

        llm_payload: dict[str, Any] | None = None
        if request.llm is not None:
            if not request.llm.base_url.startswith(("http://", "https://")):
                raise HTTPException(status_code=400, detail="llm.base_url 须为 http(s) 地址")
            old_runtime = app.state.job_llm_configs.get(job_id, {})
            api_key = (
                request.llm.api_key
                or old_runtime.get("api_key")
                or previous_secrets.get("llm_api_key")
            )
            if not api_key:
                raise HTTPException(
                    status_code=400,
                    detail="请填写 LLM 中转站 API Key（当前任务没有可复用的密钥）",
                )
            llm_payload = {**request.llm.model_dump(), "api_key": api_key}

        doc_paths = previous.get("doc_paths", [])
        document_pairs = previous.get("document_pairs")
        if request.document_pairs:
            document_pairs = _replace_document_pairs(orch_store, job_id, request.document_pairs)
            doc_paths = [str(pair["target_path"]) for pair in document_pairs]
        elif request.docs:
            doc_paths = [str(path) for path in _replace_docs(orch_store, job_id, request.docs)]
            document_pairs = None
        elif not request.keep_existing_docs:
            raise HTTPException(status_code=400, detail="未选择新需求文档时必须保留当前需求文档")
        if not doc_paths:
            raise HTTPException(status_code=400, detail="当前任务缺少可保留的需求文档，请重新上传")

        job_input: dict[str, Any] = {
            "job_id": job_id,
            "doc_paths": doc_paths,
            "repos": [_repo_metadata(repo, settings.repositories_root) for repo in request.repos],
            "rest_url": request.rest_url.rstrip("/"),
            "rest_insecure_tls": request.rest_insecure_tls,
            "owner_id": owner_id,
            "project_id": project_id,
            "resume": request.resume,
            "submitted_at": previous.get("submitted_at", _now_iso()),
            "updated_at": _now_iso(),
        }
        if isinstance(document_pairs, list):
            job_input["document_pairs"] = document_pairs
        if auth_payload is not None:
            if app.state.secret_store.enabled:
                job_input["auth"] = {
                    "login_url": auth_payload["login_url"],
                    "username": auth_payload["username"],
                    "insecure_tls": auth_payload["insecure_tls"],
                    "password_configured": True,
                }
            else:
                job_input["auth"] = auth_payload
        if llm_payload is not None:
            job_input["llm"] = {
                key: llm_payload[key] for key in ("model", "base_url", "max_tokens")
            }
            job_input["llm"]["api_key_configured"] = True
            app.state.job_llm_configs[job_id] = llm_payload
        elif request.llm_enabled is not False and isinstance(previous.get("llm"), dict):
            job_input["llm"] = previous["llm"]
        if request.e2e is not None:
            job_input["e2e"] = request.e2e.model_dump()
        if request.replace_fixtures:
            job_input["fixtures"] = [fixture.model_dump() for fixture in request.fixtures]
        elif request.fixtures:
            job_input["fixtures"] = [fixture.model_dump() for fixture in request.fixtures]
        elif isinstance(previous.get("fixtures"), list):
            job_input["fixtures"] = previous["fixtures"]
        preserved_llm_key = (
            previous_secrets.get("llm_api_key")
            if (
                request.llm is None
                and request.llm_enabled is not False
                and isinstance(previous.get("llm"), dict)
            )
            else None
        )
        previous_git_tokens = previous_secrets.get("git_tokens")
        git_tokens = dict(previous_git_tokens) if isinstance(previous_git_tokens, dict) else {}
        git_tokens.update(_repo_git_tokens(request.repos))
        _persist_job_secrets(
            app,
            job_id,
            auth=auth_payload,
            llm_api_key=(llm_payload or {}).get("api_key") or preserved_llm_key,
            git_tokens=git_tokens,
        )
        if git_tokens:
            app.state.job_repo_tokens[job_id] = git_tokens
        write_private_json(orch_store.job_dir(job_id) / "job_input.json", job_input)
        if app.state.database is not None:
            await asyncio.to_thread(
                app.state.database.save_job_config,
                job_id,
                job_input,
                created_by=owner_id,
            )
        if request.run_after_save:
            _spawn_run(app, job_id, force_node="req_parse")
        return {
            "job_id": job_id,
            "status": (
                "configuration-saved-and-rerun-queued"
                if request.run_after_save
                else "configuration-saved"
            ),
            "from_node": "req_parse" if request.run_after_save else None,
        }

    @app.get("/api/v1/jobs/{job_id}/logs")
    async def job_logs(job_id: str, http_request: Request) -> dict[str, Any]:
        """合并当前与历次归档的模型/节点留痕，并保留重跑轮次信息。"""
        _require_job_access(app, job_id, http_request)
        job_dir = orch_store.job_dir(job_id)
        entries: list[dict[str, Any]] = []
        for path in job_dir.rglob("*.jsonl"):
            if path.name not in {"llm_calls.jsonl", "review_log.jsonl", "runtime_log.jsonl"}:
                continue
            try:
                relative_path = path.relative_to(job_dir)
                relative_name = str(relative_path)
                run_scope = "archive" if "archive" in relative_path.parts else "current"
                for line in path.read_text(encoding="utf-8").splitlines():
                    item = redact_secrets(json.loads(line))
                    if isinstance(item, dict):
                        # 日志本身是追加文件，重跑不会覆盖旧记录；把文件来源
                        # 和 run_id 一并返回，前端可区分“本轮/历史轮次”，且
                        # 同名 call_id 不会把不同轮次的记录互相覆盖。
                        item["run_scope"] = run_scope
                        item["run_file"] = relative_name
                        entries.append(item)
            except (OSError, json.JSONDecodeError):
                continue
        # 数据库模式下补读运行事件：多实例或对象存储恢复场景可能看不到本地 JSONL。
        # 若本地文件同时可见，按事件关键字段去重，避免同一条事件重复展示。
        if app.state.database is not None:
            existing_runtime_keys = {
                _runtime_log_key(item)
                for item in entries
                if item.get("event") is not None
            }
            database_events = await asyncio.to_thread(
                app.state.database.list_events,
                job_id,
                limit=1000,
            )
            for record in database_events:
                if not record.event_type.startswith("runtime_"):
                    continue
                try:
                    item = json.loads(record.payload_json)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                item = redact_secrets(item)
                if _runtime_log_key(item) in existing_runtime_keys:
                    continue
                item["run_scope"] = "database"
                item["run_file"] = "job_events"
                entries.append(item)
                existing_runtime_keys.add(_runtime_log_key(item))
        unique = {
            f"{item.get('run_file', '')}:{item.get('call_id', '') or index}": item
            for index, item in enumerate(entries)
        }
        return {
            "logs": sorted(
                unique.values(),
                key=lambda item: (str(item.get("timestamp", "")), str(item.get("run_file", ""))),
            )
        }

    @app.get("/api/v1/jobs/{job_id}/export")
    async def export_job(job_id: str, http_request: Request) -> StreamingResponse:
        """导出用户可见结果产物；排除 job_input、原始文档、密钥与归档内部元数据。"""
        _require_job_access(app, job_id, http_request)
        root = orch_store.job_dir(job_id)
        allowed = set(_ARTIFACT_WHITELIST) | {"test_report.md", "metrics.json"}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(allowed):
                try:
                    path = orch_store.ensure_local(job_id, name)
                except (FileNotFoundError, OSError, ObjectStorageError, PipelineError):
                    continue
                archive.writestr(name, path.read_bytes())
            scripts = root / "scripts"
            script_paths = (
                sorted(scripts.rglob("*.py"))
                if scripts.is_dir()
                else []
            )
            for path in script_paths:
                relative_name = path.relative_to(root).as_posix()
                try:
                    local_path = orch_store.ensure_local(job_id, relative_name)
                except (FileNotFoundError, OSError, ObjectStorageError, PipelineError):
                    continue
                archive.writestr(relative_name, local_path.read_bytes())
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{job_id}-results.zip"'},
        )

    @app.get("/api/v1/jobs/{job_id}/artifacts/{name}")
    async def read_artifact(job_id: str, name: str, http_request: Request) -> Any:
        """读产物（白名单内；JSON 按对象返回，md/jsonl 按纯文本）。"""
        _require_job_access(app, job_id, http_request)
        if name not in _ARTIFACT_WHITELIST:
            raise HTTPException(status_code=404, detail=f"产物不在白名单：{name}")
        try:
            path = orch_store.ensure_local(job_id, name)
        except (FileNotFoundError, OSError, ObjectStorageError, PipelineError) as exc:
            raise HTTPException(status_code=404, detail=f"产物尚未生成：{name}") from exc
        text = path.read_text(encoding="utf-8")
        if name.endswith(".json"):
            return JSONResponse(content=json.loads(text))
        return PlainTextResponse(
            content=text,
            media_type="text/markdown" if name.endswith(".md") else "text/plain",
        )

    @app.get("/api/v1/jobs/{job_id}/report")
    async def job_report(job_id: str, http_request: Request) -> Any:
        """测试报告（Markdown，G5 证据物之一）。"""
        _require_job_access(app, job_id, http_request)
        try:
            path = orch_store.ensure_local(job_id, "test_report.md")
        except (FileNotFoundError, OSError, ObjectStorageError, PipelineError) as exc:
            raise HTTPException(status_code=404, detail="test_report.md 尚未生成") from exc
        report = path.read_text(encoding="utf-8")
        return PlainTextResponse(content=report, media_type="text/markdown")

    @app.get("/api/v1/jobs/{job_id}/audit")
    async def job_audit(
        job_id: str,
        http_request: Request,
        limit: int = 200,
        before_event_id: int | None = None,
    ) -> dict[str, Any]:
        """读取脱敏操作审计；数据库未启用时明确返回空的兼容结果。"""
        _require_job_access(app, job_id, http_request)
        database: Database | None = app.state.database
        if database is None:
            return {"job_id": job_id, "events": [], "storage": "database-disabled"}
        bounded_limit = max(1, min(limit, 200))
        records = await asyncio.to_thread(
            database.list_events,
            job_id,
            limit=bounded_limit,
            before_event_id=before_event_id,
        )
        return {
            "job_id": job_id,
            "events": [
                {
                    "event_id": record.event_id,
                    "event_type": record.event_type,
                    "node_name": record.node_name,
                    "payload": json.loads(record.payload_json),
                    "created_at": record.created_at.isoformat(),
                    "created_by": record.created_by,
                }
                for record in records
            ],
            "storage": "database",
            "next_before_event_id": records[-1].event_id if len(records) == bounded_limit else None,
        }

    # ---- 复测（F-DEF-02 全量 / 07 §3.5 单条）----

    @app.post("/api/v1/jobs/{job_id}/retest")
    async def retest(
        job_id: str, request: RetestRequest, http_request: Request
    ) -> dict[str, Any]:
        """复测：defect_id 空 → 后台全量（force_node，归档留痕）；指定 → 同步单条。"""
        _require_job_access(app, job_id, http_request)
        if await _task_alive_async(app, job_id):
            raise HTTPException(status_code=409, detail=f"job {job_id} 正在运行中")
        job_input = _load_job_input(orch_store, job_id)
        if request.defect_id is None:
            _spawn_run(app, job_id, force_node="retest_regression")
            return {"job_id": job_id, "mode": "full", "status": "queued"}
        result = await retest_defects(
            te_store,
            job_id,
            job_input["rest_url"],
            only_defect_ids={request.defect_id},
            auth=_job_auth_config(app, job_id, job_input),
            insecure_tls=bool(job_input.get("rest_insecure_tls", True)),
            fixtures=job_input.get("fixtures")
            if isinstance(job_input.get("fixtures"), list)
            else None,
        )
        return {
            "job_id": job_id,
            "mode": "single",
            "defect_id": request.defect_id,
            "retested": result["retested"],
            "archived": len(result["archived"]),
            "warnings": result["warnings"],
        }

    # ---- 重跑（C-14）----

    @app.post("/api/v1/jobs/{job_id}/rerun")
    async def rerun_job(
        job_id: str, request: RerunRequest, http_request: Request
    ) -> dict[str, Any]:
        """重跑（C-14）：from_node 缺省 = 全量（req_parse 级联失效）；

        级联失效：目标节点及其全部下游产物归档（带时间戳）并重置后重跑，
        避免新旧数据混用（F-ORCH-02 语义修正，docs/13 C-14）。
        """
        _require_job_access(app, job_id, http_request)
        if await _task_alive_async(app, job_id):
            raise HTTPException(status_code=409, detail=f"job {job_id} 正在运行中")
        force_node = request.from_node or "req_parse"
        if force_node not in NODE_ORDER:
            raise HTTPException(
                status_code=400,
                detail=f"from_node 须为 9 节点之一：{force_node}（可选：{'、'.join(NODE_ORDER)}）",
            )
        job_input = _load_job_input(orch_store, job_id)  # 非 Web 提交无原始输入 → 409 同复测口径
        if request.rest_url is not None:
            if not request.rest_url.startswith(("http://", "https://")):
                raise HTTPException(status_code=400, detail="rest_url 须为 http(s) 地址")
            job_input["rest_url"] = request.rest_url.rstrip("/")
        if request.e2e is not None:
            if not request.e2e.base_url.startswith(("http://", "https://")):
                raise HTTPException(status_code=400, detail="e2e.base_url 须为 http(s) 地址")
            job_input["e2e"] = request.e2e.model_dump()
        if request.llm is not None:
            app.state.job_llm_configs[job_id] = request.llm.model_dump()
            job_input["llm"] = {
                "model": request.llm.model,
                "base_url": request.llm.base_url,
                "max_tokens": request.llm.max_tokens,
                "api_key_configured": True,
            }
            if app.state.secret_store.enabled:
                _persist_job_secrets(
                    app,
                    job_id,
                    auth=_job_auth_config(app, job_id, job_input),
                    llm_api_key=request.llm.api_key,
                )
        if request.rest_url is not None or request.e2e is not None or request.llm is not None:
            # 仅更新执行目标和非敏感模型元数据，不读取/返回 API key。
            write_private_json(orch_store.job_dir(job_id) / "job_input.json", job_input)
            if app.state.database is not None:
                await asyncio.to_thread(
                    app.state.database.save_job_config,
                    job_id,
                    job_input,
                )
        _spawn_run(app, job_id, force_node=force_node)
        return {"job_id": job_id, "status": "rerun-queued", "from_node": force_node}

    @app.post("/api/v1/jobs/{job_id}/stop")
    async def stop_job(job_id: str, http_request: Request) -> dict[str, Any]:
        """停止正在运行的任务，保留已完成产物并使当前节点可续跑。"""
        _require_job_access(app, job_id, http_request)
        task: asyncio.Task[None] | None = app.state.job_tasks.get(job_id)
        if task is None or task.done():
            raise HTTPException(status_code=409, detail=f"job {job_id} 当前未在运行")

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # `_run` 会重新抛出取消信号；这表示协程确实已停止，而非运行失败。
            pass
        if app.state.database is not None:
            await asyncio.to_thread(app.state.database.stop_job, job_id)
        stopped_nodes = app.state.engine.stop_running_nodes(job_id)
        _remember_job_outcome(
            app,
            job_id,
            {
                "job_id": job_id,
                "status": "stopped",
                "stopped_nodes": stopped_nodes,
            },
        )
        logger.info("job %s 被用户停止（nodes=%s）", job_id, stopped_nodes)
        return {"job_id": job_id, "status": "stopped", "stopped_nodes": stopped_nodes}

    # ---- 人工审核（F-REVW-06）----

    @app.post("/api/v1/jobs/{job_id}/review-log")
    async def review_log(
        job_id: str, request: ReviewLogRequest, http_request: Request
    ) -> dict[str, Any]:
        """人工审核记录：approved 仅记录；rejected 回流至生成节点（产物归档留痕）。"""
        _require_job_access(app, job_id, http_request)
        if request.decision not in ("approved", "rejected"):
            raise HTTPException(status_code=400, detail="decision 须为 approved 或 rejected")
        if await _task_alive_async(app, job_id):
            raise HTTPException(status_code=409, detail=f"job {job_id} 正在运行中")
        entry = {
            "timestamp": _now_iso(),
            "decision": request.decision,
            "reviewer": request.reviewer,
            "note": request.note,
        }
        log_path = orch_store.job_dir(job_id) / "review_log.jsonl"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if request.decision == "rejected":
            # 驳回 → 回流至生成节点（F-REVW-06）：force_node 重跑，旧产物归档
            _spawn_run(app, job_id, force_node="testcase_generate")
            return {"job_id": job_id, **entry, "rerun": "testcase_generate"}
        return {"job_id": job_id, **entry, "rerun": None}

    # ---- 静态前端（可选：FRONTEND_DIST 指向构建产物则托管，零配置演示）----

    frontend_dist = os.environ.get("FRONTEND_DIST", "")
    if frontend_dist and Path(frontend_dist).is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app


# ---- 内部工具 ----


def _task_alive(app: FastAPI, job_id: str) -> bool:
    """job 后台任务是否仍在运行。"""
    task: asyncio.Task[None] | None = app.state.job_tasks.get(job_id)
    return task is not None and not task.done()


def _remember_job_outcome(
    app: FastAPI, job_id: str, outcome: PipelineOutcome | dict[str, Any]
) -> None:
    """只保留有限的进程内结果；完整状态由数据库/产物文件承载。"""
    app.state.job_outcomes[job_id] = outcome
    while len(app.state.job_outcomes) > MAX_IN_MEMORY_OUTCOMES:
        oldest = next(iter(app.state.job_outcomes))
        if oldest == job_id and len(app.state.job_outcomes) == 1:
            break
        app.state.job_outcomes.pop(oldest, None)


async def _task_alive_async(app: FastAPI, job_id: str) -> bool:
    """同时检查本进程任务和数据库中的跨实例排队/租约状态。"""
    if _task_alive(app, job_id):
        return True
    database: Database | None = app.state.database
    if database is None:
        return False
    return await asyncio.to_thread(database.is_job_locked, job_id)


def _recover_interrupted_jobs(app: FastAPI) -> dict[str, list[str]]:
    """服务启动时清理上次进程遗留的 running 状态。

    后台 asyncio 任务不能跨进程恢复：进程退出后不可能仍在执行。因此只要本进程没有
    对应任务，state.json 中的 running 必为遗留状态，必须回退为可重跑的 pending。
    """
    recovered: dict[str, list[str]] = {}
    root = Path(str(app.state.settings.artifacts_root))
    if not root.is_dir():
        return recovered
    for entry in root.iterdir():
        if not entry.is_dir() or not (entry / "state.json").is_file():
            continue
        try:
            stopped = app.state.engine.stop_running_nodes(
                entry.name,
                "服务重启导致任务中断；已完成产物保留，可从该节点继续重跑",
            )
        except PipelineError:
            logger.warning("跳过无法恢复的任务状态：%s", entry.name, exc_info=True)
            continue
        if stopped:
            recovered[entry.name] = stopped
    return recovered


def _pick_native_directory() -> str:
    """标准库 Tk 目录框；不在无图形环境中伪造路径。"""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return str(filedialog.askdirectory(title="选择被测源码仓库目录"))
    finally:
        root.destroy()


def _db_datetime_iso(value: datetime | None) -> str | None:
    """数据库时间统一输出带时区的 ISO 字符串（SQLite 可能返回 naive 值）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _runtime_log_key(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """运行事件去重键；不包含 job_id，避免把任务身份写入高基数标签。"""
    return (
        str(item.get("timestamp", "")),
        str(item.get("event", "")),
        str(item.get("node", "")),
        str(item.get("message", "")),
        str(item.get("attempt", "")),
    )


def _read_state_if_present(job_dir: Path) -> dict[str, Any]:
    """读取本地状态快照；数据库模式下本地卷不可见时返回空对象。"""
    path = job_dir / "state.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _db_node_payloads(records: list[Any]) -> list[dict[str, Any]]:
    """数据库节点记录转换为与 state.json 兼容的 API 结构。"""
    result: list[dict[str, Any]] = []
    for record in records:
        item: dict[str, Any] = {
            "name": record.node_name,
            "status": record.status,
            "updated_at": _db_datetime_iso(record.updated_at),
            "attempts": record.attempts,
        }
        if record.error_json:
            try:
                error = json.loads(record.error_json)
            except json.JSONDecodeError:
                error = {"message": "节点错误信息无法解析"}
            if isinstance(error, dict):
                item["error"] = error
        result.append(item)
    return result


def _merge_node_payloads(
    state_nodes: Any,
    db_records: list[Any],
) -> list[dict[str, Any]]:
    """按节点更新时间合并文件证据与数据库快照。

    数据库是跨实例查询索引；当共享文件卷可见且文件状态更新更晚时，不能让一次
    数据库写入失败把已完成节点倒退为 pending。反之，本地卷不可见时仍完整采用 DB。
    """
    file_nodes = {
        str(item.get("name")): item
        for item in state_nodes
        if isinstance(item, dict) and item.get("name")
    }
    db_nodes = {str(item["name"]): item for item in _db_node_payloads(db_records)}
    merged: list[dict[str, Any]] = []
    for name in NODE_ORDER:
        file_item = file_nodes.get(name)
        db_item = db_nodes.get(name)
        if file_item is None:
            chosen = db_item
        elif db_item is None:
            chosen = file_item
        else:
            file_time = str(file_item.get("updated_at") or "")
            db_time = str(db_item.get("updated_at") or "")
            chosen = db_item if db_time >= file_time else file_item
        if chosen is not None:
            merged.append(dict(chosen))
    # 兼容历史/测试产物中不在当前节点清单的附加记录，不丢失可诊断信息。
    known = {item["name"] for item in merged}
    for item in (*file_nodes.values(), *db_nodes.values()):
        if item["name"] not in known:
            merged.append(dict(item))
            known.add(item["name"])
    return merged


def _require_job(app: FastAPI, job_id: str) -> Path:
    """job 存在性校验；配置数据库时允许本地产物卷暂时不可见。"""
    job_dir: Path = app.state.orch_store.job_dir(job_id)
    database: Database | None = app.state.database
    db_exists = database is not None and database.get_job(job_id) is not None
    if not db_exists and not (job_dir / "state.json").is_file() and not _task_alive(app, job_id):
        raise HTTPException(status_code=404, detail=f"job 不存在：{job_id}")
    return job_dir


def _check_doc_name(doc_name: str) -> None:
    """C-12：文档名仅允许纯 basename（拒绝对路径/斜杠/反斜杠/..与控制字符）。"""
    problems: list[str] = []
    if not doc_name or len(doc_name) > 128:
        problems.append("非空且长度 ≤128")
    if doc_name != Path(doc_name).name or "/" in doc_name or "\\" in doc_name:
        problems.append("须为纯文件名（禁止路径分隔符/绝对路径）")
    if ".." in doc_name or doc_name in {".", ".."}:
        problems.append("禁止 .. 片段")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in doc_name):
        problems.append("禁止控制字符")
    if problems:
        raise HTTPException(
            status_code=400,
            detail=f"文档名非法（C-12 安全口径 {'；'.join(problems)}）：{doc_name!r}",
        )


def _request_docs(request: JobSubmitRequest) -> list[DocInput]:
    """归一提交文档：docs 优先；否则旧单文档字段；两组均空 → 400。"""
    if request.docs:
        return request.docs
    if request.doc_name is not None or request.doc_content_b64 is not None:
        if request.doc_name is None or request.doc_content_b64 is None:
            raise HTTPException(
                status_code=400, detail="旧单文档字段 doc_name/doc_content_b64 须同时提供"
            )
        return [DocInput(doc_name=request.doc_name, doc_content_b64=request.doc_content_b64)]
    raise HTTPException(status_code=400, detail="至少提交 1 份需求文档（docs 或旧单文档字段）")


def _validate_doc_inputs(docs: list[DocInput]) -> None:
    """只校验文档输入，不写盘；供配置预检复用提交口径。"""
    if not docs:
        raise HTTPException(status_code=400, detail="至少提交 1 份需求文档")
    if len(docs) > MAX_DOCS:
        raise HTTPException(status_code=400, detail=f"需求文档数超过上限 {MAX_DOCS} 份（C-12）")
    for doc in docs:
        _check_doc_name(doc.doc_name)
        if Path(doc.doc_name).suffix.lower() not in _DOC_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"文档格式不支持（F-REQ-01~03：md/docx/xlsx）：{doc.doc_name}",
            )
        try:
            content = base64.b64decode(doc.doc_content_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail=f"doc_content_b64 不是合法 base64：{doc.doc_name}"
            ) from exc
        if not content:
            raise HTTPException(status_code=400, detail=f"文档内容为空：{doc.doc_name}")
        if len(content) > MAX_DOC_BYTES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"文档 {doc.doc_name} 解码后超过上限 "
                    f"{MAX_DOC_BYTES // (1024 * 1024)} MB（C-12）"
                ),
            )


def _public_document_pairs(job_input: dict[str, Any]) -> list[dict[str, str | None]]:
    """配置读取仅展示文件名，不泄露任务工作目录绝对路径。"""
    pairs = job_input.get("document_pairs")
    if not isinstance(pairs, list):
        return []
    result: list[dict[str, str | None]] = []
    for pair in pairs:
        if not isinstance(pair, dict) or not pair.get("target_path"):
            continue
        target = Path(str(pair["target_path"])).name
        baseline = pair.get("baseline_path")
        result.append(
            {"target_name": target, "baseline_name": Path(str(baseline)).name if baseline else None}
        )
    return result


def _save_document_pairs(
    orch_store: ArtifactStore,
    job_id: str,
    pairs: list[RequirementDocumentPair],
    target_dir: Path | None = None,
) -> list[dict[str, str | None]]:
    """落盘多行版本文档，并将每行的基线/被测关系保存为显式元数据。"""
    if not pairs:
        return []
    flattened: list[DocInput] = []
    indexes: list[tuple[int, int | None]] = []
    for pair in pairs:
        target_index = len(flattened)
        flattened.append(pair.target)
        baseline_index: int | None = None
        if pair.baseline is not None:
            baseline_index = len(flattened)
            flattened.append(pair.baseline)
        indexes.append((target_index, baseline_index))
    paths = _save_docs(orch_store, job_id, flattened, target_dir=target_dir)
    return [
        {
            "target_path": str(paths[target_index]),
            "baseline_path": str(paths[baseline_index]) if baseline_index is not None else None,
        }
        for target_index, baseline_index in indexes
    ]


def _save_docs(
    orch_store: ArtifactStore, job_id: str, docs: list[DocInput], target_dir: Path | None = None
) -> list[Path]:
    """逐份校验并落盘上传文档（input/ 子目录），返回路径列表（req_parse 按后缀解析）。

    C-12 安全：文档名仅允许纯 basename（拒绝绝对路径/斜杠/..）；限额：
    文档数 ≤ MAX_DOCS、单文档解码后 ≤ MAX_DOC_BYTES。
    重名文档自动加序号后缀（不影响后缀识别）；同一请求内两格式同内容时
    以落盘顺序为准（LLM 单次解析全部拼接）。
    """
    if len(docs) > MAX_DOCS:
        raise HTTPException(status_code=400, detail=f"需求文档数超过上限 {MAX_DOCS} 份（C-12）")
    paths: list[Path] = []
    used_names: set[str] = set()
    for doc in docs:
        _check_doc_name(doc.doc_name)
        suffix = Path(doc.doc_name).suffix.lower()
        if suffix not in _DOC_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"文档格式不支持（F-REQ-01~03：md/docx/xlsx）：{doc.doc_name}",
            )
        try:
            content = base64.b64decode(doc.doc_content_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail=f"doc_content_b64 不是合法 base64：{doc.doc_name}"
            ) from exc
        if not content:
            raise HTTPException(status_code=400, detail=f"文档内容为空：{doc.doc_name}")
        if len(content) > MAX_DOC_BYTES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"文档 {doc.doc_name} 解码后超过上限 "
                    f"{MAX_DOC_BYTES // (1024 * 1024)} MB（C-12）"
                ),
            )
        # 重名去重：a.md + a.md → a.md + a-2.md（逐份落盘不覆盖）
        name = doc.doc_name
        if name in used_names:
            stem = Path(doc.doc_name).stem
            serial = 2
            while f"{stem}-{serial}{suffix}" in used_names:
                serial += 1
            name = f"{stem}-{serial}{suffix}"
        used_names.add(name)
        doc_path = (target_dir or orch_store.job_dir(job_id) / "input") / name
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_bytes(content)
        if target_dir is None:
            # 文档属于可复用输入证据；job_input/密钥不走镜像，避免把敏感配置
            # 误送到对象存储。
            relative_doc = doc_path.relative_to(orch_store.job_dir(job_id)).as_posix()
            orch_store.mirror_file(job_id, relative_doc)
        paths.append(doc_path)
    return paths


def _replace_docs(orch_store: ArtifactStore, job_id: str, docs: list[DocInput]) -> list[Path]:
    """原子替换任务输入文档：新文件全部校验成功后才删除旧文件。"""
    job_dir = orch_store.job_dir(job_id)
    staging = job_dir / f".input-next-{uuid.uuid4().hex}"
    try:
        staged_paths = _save_docs(orch_store, job_id, docs, target_dir=staging)
        input_dir = job_dir / "input"
        if input_dir.exists():
            shutil.rmtree(input_dir)
        staging.rename(input_dir)
        for path in staged_paths:
            orch_store.mirror_file(job_id, f"input/{path.name}")
        return [input_dir / path.name for path in staged_paths]
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _replace_document_pairs(
    orch_store: ArtifactStore, job_id: str, pairs: list[RequirementDocumentPair]
) -> list[dict[str, str | None]]:
    """原子替换多行版本文档，并回写重命名后的基线/被测路径。"""
    job_dir = orch_store.job_dir(job_id)
    staging = job_dir / f".input-next-{uuid.uuid4().hex}"
    try:
        saved = _save_document_pairs(orch_store, job_id, pairs, target_dir=staging)
        input_dir = job_dir / "input"
        if input_dir.exists():
            shutil.rmtree(input_dir)
        staging.rename(input_dir)
        for pair in saved:
            orch_store.mirror_file(job_id, f"input/{Path(str(pair['target_path'])).name}")
            if pair.get("baseline_path"):
                orch_store.mirror_file(job_id, f"input/{Path(str(pair['baseline_path'])).name}")
        return [
            {
                "target_path": str(input_dir / Path(str(pair["target_path"])).name),
                "baseline_path": (
                    str(input_dir / Path(str(pair["baseline_path"])).name)
                    if pair.get("baseline_path")
                    else None
                ),
            }
            for pair in saved
        ]
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _load_job_input(orch_store: ArtifactStore, job_id: str) -> dict[str, Any]:
    """读取任务配置；本地文件缺失时回退数据库最新快照。"""
    path = orch_store.job_dir(job_id) / "job_input.json"
    if not path.is_file():
        database = orch_store.database
        if database is not None:
            snapshot = database.latest_job_config(job_id)
            if snapshot is not None:
                return _materialize_job_inputs(orch_store, job_id, snapshot)
        raise HTTPException(status_code=409, detail=f"job {job_id} 缺少任务配置（非 Web 提交）")
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return _materialize_job_inputs(orch_store, job_id, payload)


def _materialize_job_inputs(
    orch_store: ArtifactStore,
    job_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """把配置快照中的文档路径归一到当前任务目录，并按对象存储尝试恢复。"""
    result = dict(payload)
    job_dir = orch_store.job_dir(job_id)

    def materialize(value: Any) -> str:
        name = Path(str(value)).name
        target = job_dir / "input" / name
        if not target.is_file():
            try:
                target = orch_store.ensure_local(job_id, f"input/{name}")
            except (FileNotFoundError, OSError, ObjectStorageError, PipelineError):
                # 后续节点会给出“需求文档不存在”的可诊断错误；这里不伪造内容。
                pass
        return str(target)

    if isinstance(result.get("doc_paths"), list):
        result["doc_paths"] = [materialize(value) for value in result["doc_paths"]]
    elif result.get("doc_path"):
        result["doc_path"] = materialize(result["doc_path"])
    pairs = result.get("document_pairs")
    if isinstance(pairs, list):
        normalized_pairs: list[dict[str, Any]] = []
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            normalized = dict(pair)
            if normalized.get("target_path"):
                normalized["target_path"] = materialize(normalized["target_path"])
            if normalized.get("baseline_path"):
                normalized["baseline_path"] = materialize(normalized["baseline_path"])
            normalized_pairs.append(normalized)
        result["document_pairs"] = normalized_pairs
    return result


def _request_scope(app: FastAPI, request: Request) -> tuple[str, str]:
    """解析当前请求的服务端身份与项目，不接受客户端伪造 user_id。"""
    principal = getattr(request.state, "access_principal", None)
    if principal is None:
        principal = app.state.access_policy.authenticate(request.headers)
    if principal is None:
        raise HTTPException(status_code=401, detail="访问令牌无效或缺失")
    request.state.access_principal = principal
    requested_project = request.headers.get("x-project-id", "").strip()
    if not requested_project and len(principal.projects) == 1:
        requested_project = next(iter(principal.projects))
    if not requested_project:
        requested_project = "default"
    if len(requested_project) > 128 or not app.state.access_policy.allows_project(
        principal, requested_project
    ):
        raise HTTPException(status_code=403, detail="当前身份无权访问该项目")
    return principal.user_id, requested_project


def _require_job_access(app: FastAPI, job_id: str, request: Request) -> None:
    """校验任务存在且属于当前用户/项目；数据库模式不依赖本地输入文件。"""
    _require_job(app, job_id)
    if not app.state.access_policy.enabled:
        return
    user_id, project_id = _request_scope(app, request)
    record = (
        app.state.database.get_job(job_id) if app.state.database is not None else None
    )
    if record is not None:
        owner_matches = record.created_by == user_id and record.project_id == project_id
    else:
        payload = _load_job_input(app.state.orch_store, job_id)
        owner_matches = (
            payload.get("owner_id") == user_id and payload.get("project_id") == project_id
        )
    if not owner_matches:
        raise HTTPException(status_code=404, detail=f"任务不存在：{job_id}")


async def _enforce_submission_quota(
    app: FastAPI,
    request: Request,
    owner_id: str,
    project_id: str,
) -> None:
    """执行令牌级并发/日配额；未启用部门访问策略时保持开发兼容。"""
    if not app.state.access_policy.enabled:
        return
    principal = getattr(request.state, "access_principal", None)
    if not isinstance(principal, AccessPrincipal):
        return
    quota = principal.quota_for(project_id)
    database: Database | None = app.state.database
    active_statuses = {"queued", "running"}
    if database is not None:
        if quota.max_concurrent_jobs is not None:
            active = await asyncio.to_thread(
                database.count_jobs,
                created_by=owner_id,
                project_id=project_id,
                statuses=active_statuses,
            )
            if active >= quota.max_concurrent_jobs:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"当前用户/项目已有 {active} 个任务运行或排队，"
                        f"并发上限为 {quota.max_concurrent_jobs}"
                    ),
                )
        if quota.daily_job_limit is not None:
            now = datetime.now(UTC)
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            submitted = await asyncio.to_thread(
                database.count_jobs,
                created_by=owner_id,
                project_id=project_id,
                created_since=day_start,
            )
            if submitted >= quota.daily_job_limit:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"今日已提交 {submitted} 个任务，"
                        f"日配额为 {quota.daily_job_limit}"
                    ),
                )


async def _append_audit_event(
    app: FastAPI,
    request: Request,
    job_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """写入脱敏审计事件；数据库故障只记录日志，不改变业务响应。"""
    database: Database | None = app.state.database
    if database is None:
        return
    principal = getattr(request.state, "access_principal", None)
    created_by = principal.user_id if isinstance(principal, AccessPrincipal) else None
    try:
        await asyncio.to_thread(
            database.append_event,
            job_id,
            event_type,
            payload=payload or {},
            created_by=created_by,
        )
    except Exception:  # pragma: no cover - 审计故障不阻断业务主流程
        logger.exception("操作审计写入失败：%s/%s", job_id, event_type)


def _load_job_secrets(app: FastAPI, job_id: str) -> dict[str, Any]:
    """读取任务密钥；解密失败时阻止执行，不回退到猜测或静默空凭据。"""
    try:
        secrets = app.state.secret_store.load(job_id)
        return dict(secrets) if isinstance(secrets, dict) else {}
    except SecretStoreError as exc:
        raise HTTPException(status_code=500, detail="任务密钥不可用，请联系管理员") from exc


def _job_auth_config(app: FastAPI, job_id: str, job_input: dict[str, Any]) -> dict[str, Any] | None:
    """合并非敏感任务配置与加密凭据，供执行/复测使用。"""
    auth = job_input.get("auth")
    secrets = _load_job_secrets(app, job_id)
    stored_auth = secrets.get("auth")
    if isinstance(stored_auth, dict):
        merged = dict(auth) if isinstance(auth, dict) else {}
        merged.update(stored_auth)
        return merged
    return dict(auth) if isinstance(auth, dict) else None


def _persist_job_secrets(
    app: FastAPI,
    job_id: str,
    *,
    auth: dict[str, Any] | None,
    llm_api_key: str | None,
    git_tokens: dict[str, str] | None = None,
) -> None:
    """保存/清理任务密钥；未启用加密时由兼容路径继续使用旧 job_input。"""
    store: SecretStore = app.state.secret_store
    if not store.enabled:
        return
    secrets: dict[str, Any] = {}
    if auth is not None:
        secrets["auth"] = auth
    if llm_api_key:
        secrets["llm_api_key"] = llm_api_key
    if git_tokens:
        secrets["git_tokens"] = git_tokens
    try:
        if secrets:
            store.save(job_id, secrets)
        else:
            store.clear(job_id)
    except SecretStoreError as exc:
        raise HTTPException(status_code=500, detail="任务密钥无法安全保存，请联系管理员") from exc


def _job_e2e_config(orch_store: ArtifactStore, job_id: str) -> dict[str, Any] | None:
    """读取任务的非敏感 E2E 开关；历史/CLI 任务缺输入时按未启用处理。"""
    try:
        payload = _load_job_input(orch_store, job_id).get("e2e")
    except HTTPException:
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "base_url": str(payload.get("base_url", "")),
        "generate_cases": bool(payload.get("generate_cases")),
        "execute_cases": bool(payload.get("execute_cases")),
        "insecure_tls": bool(payload.get("insecure_tls", True)),
        # 历史任务维持原浏览器登录行为；新任务可以选择复用验证过的同源会话。
        "auth_mode": str(payload.get("auth_mode") or "browser"),
    }


def _job_rest_url(orch_store: ArtifactStore, job_id: str) -> str | None:
    """供重跑表单预填的非敏感接口目标地址。"""
    try:
        value = _load_job_input(orch_store, job_id).get("rest_url")
    except HTTPException:
        return None
    return str(value) if value else None


def _spawn_run(app: FastAPI, job_id: str, force_node: str | None = None) -> None:
    """后台驱动 PipelineRunner（同一入口 F-ORCH-01；force_node 归档留痕）。"""
    job_input = _load_job_input(app.state.orch_store, job_id)
    # C-10：新格式 doc_paths 列表；旧 job 的单值 doc_path 兼容归一为列表
    doc_paths = [Path(p) for p in job_input.get("doc_paths", [])]
    if not doc_paths and job_input.get("doc_path"):
        doc_paths = [Path(job_input["doc_path"])]
    # C-15：鉴权配置随原始输入复用（rerun/retest 全量同样生效）
    auth = _job_auth_config(app, job_id, job_input)
    auth_headers = app.state.job_auth_headers.get(job_id)
    # 重跑优先复用进程态密钥；服务重启后从 SecretStore 恢复，绝不从 job_input 读取明文。
    llm_config = app.state.job_llm_configs.get(job_id)
    if llm_config is None and isinstance(job_input.get("llm"), dict):
        secrets = _load_job_secrets(app, job_id)
        api_key = secrets.get("llm_api_key")
        if isinstance(api_key, str) and api_key:
            llm_config = {**job_input["llm"], "api_key": api_key}

    async def _run() -> None:
        runner: PipelineRunner = app.state.runner
        try:
            secrets = _load_job_secrets(app, job_id)
            git_tokens = secrets.get("git_tokens")
            if isinstance(app.state.job_repo_tokens.get(job_id), dict):
                git_tokens = {
                    **(git_tokens if isinstance(git_tokens, dict) else {}),
                    **app.state.job_repo_tokens[job_id],
                }
            repositories = await asyncio.to_thread(
                resolve_repository_inputs,
                job_input["repos"],
                cache_root=Path(app.state.settings.artifacts_root),
                git_tokens=git_tokens if isinstance(git_tokens, dict) else None,
                database=app.state.database,
                job_id=job_id,
            )
            run_args: dict[str, Any] = {
                "job_id": job_id,
                "doc_paths": doc_paths,
                "repositories": repositories,
                "rest_base_url": job_input["rest_url"],
                "rest_insecure_tls": bool(job_input.get("rest_insecure_tls", True)),
                "force_node": force_node,
                "auth": auth,
            }
            if auth_headers:
                run_args["auth_headers"] = auth_headers
            if job_input.get("e2e") is not None:
                run_args["e2e"] = job_input["e2e"]
            if isinstance(job_input.get("fixtures"), list):
                run_args["fixtures"] = job_input["fixtures"]
            if isinstance(job_input.get("document_pairs"), list):
                run_args["document_pairs"] = job_input["document_pairs"]
            if llm_config is not None:
                run_args["llm_config"] = llm_config
            outcome = await runner.run(**run_args)
            _remember_job_outcome(app, job_id, outcome)
            logger.info("job %s 完成（force_node=%s）", job_id, force_node)
        except asyncio.CancelledError:
            logger.info("job %s 收到停止请求（force_node=%s）", job_id, force_node)
            raise
        except PipelineError as exc:
            logger.error("job %s 仓库准备失败：%s", job_id, exc)
            _remember_job_outcome(app, job_id, {"job_id": job_id, "error": exc.to_dict()})
        except Exception:
            logger.exception("job %s 后台运行异常（force_node=%s）", job_id, force_node)
            _remember_job_outcome(
                app, job_id, {"job_id": job_id, "error": "后台运行异常，见服务日志"}
            )
        finally:
            # 运行期映射只服务当前实例/当前轮次；生产重跑从 SecretStore 恢复，
            # 避免 API Key、Cookie、Git Token 随任务数量无限滞留在进程内存。
            app.state.job_auth_headers.pop(job_id, None)
            app.state.job_llm_configs.pop(job_id, None)
            app.state.job_repo_tokens.pop(job_id, None)

    try:
        app.state.job_queue.submit(job_id, _run)
    except QueueConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（横切约定）。"""
    return datetime.now(UTC).isoformat()


def _auth_fingerprint(auth: dict[str, Any]) -> str:
    """会话只可复用于完全相同的登录配置；摘要不出服务端。"""
    material = json.dumps(
        {
            "login_url": auth.get("login_url", ""),
            "username": auth.get("username", ""),
            "password": auth.get("password", ""),
            "insecure_tls": bool(auth.get("insecure_tls", True)),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _take_verified_auth_headers(
    app: FastAPI, auth: dict[str, Any], session_id: str | None
) -> dict[str, str] | None:
    """接管同配置预检会话；未点预检则保持原有执行前登录行为。"""
    pending = app.state.pending_auth_sessions.pop(session_id, None) if session_id else None
    if pending:
        created_at = float(pending.get("created_at", 0.0))
        if time.monotonic() - created_at >= AUTH_SESSION_TTL_SECONDS:
            pending = None
    if pending and pending.get("fingerprint") == _auth_fingerprint(auth):
        headers = pending.get("headers")
        if isinstance(headers, dict) and all(isinstance(value, str) for value in headers.values()):
            return dict(headers)
    return None
