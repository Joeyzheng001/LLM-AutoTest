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
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from orchestrator.artifacts import ArtifactStore, ensure_job_id
from orchestrator.config import Settings
from orchestrator.engine import NODE_ARTIFACTS, Engine
from orchestrator.errors import PipelineError
from orchestrator.javaparser_client import JavaParserClient
from orchestrator.nodes.auto_execute import login_for_headers
from orchestrator.nodes.retest_regression import retest_defects
from orchestrator.pipeline import NODE_ORDER, PipelineState
from orchestrator.runner import PipelineOutcome, PipelineRunner, repository_inputs_from_config

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


class RepoConfig(BaseModel):
    """仓库配置（与 java-parser RepositoryInput 对齐；path 须为绝对路径）。"""

    name: str
    path: str
    branch: str = "main"
    baseline_branch: str | None = None  # C-09：基线分支（可选，指定则产出变更清单）
    is_fallback: bool = False
    include_paths: list[str] = Field(default_factory=list)
    # C-13：仓库语言（java → java-parser；python → 内置 AST 扫描器）
    language: str = "java"


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
    """本次任务的 MiniMax 运行时配置；密钥只驻留后台任务内存。"""

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


class EditableAuthConfig(BaseModel):
    """编辑任务时的鉴权配置；空密码表示保留原任务的密码，绝不回传。"""

    login_url: str
    username: str
    password: str | None = Field(default=None, max_length=256)
    insecure_tls: bool = True


class EditableLlmConfig(BaseModel):
    """编辑任务时的模型配置；API Key 可省略以复用仍在内存中的密钥。"""

    api_key: str | None = Field(default=None, min_length=8, max_length=512)
    model: str = Field(default="MiniMax-M3", min_length=1, max_length=128)
    base_url: str = Field(default="https://api.minimaxi.com/v1", min_length=8, max_length=256)
    max_tokens: int = Field(default=16384, ge=1024, le=16384)


class JobConfigurationUpdateRequest(BaseModel):
    """完整任务配置编辑请求。

    未上传 docs 时保留当前需求文档；鉴权密码与模型 Key 为空时仅复用服务仍在
    内存中的值，接口从不读取或返回它们。
    """

    docs: list[DocInput] = Field(default_factory=list)
    document_pairs: list[RequirementDocumentPair] = Field(default_factory=list)
    keep_existing_docs: bool = True
    repos: list[RepoConfig] = Field(min_length=1)
    rest_url: str
    rest_insecure_tls: bool = True
    resume: bool = True
    run_after_save: bool = True
    auth: EditableAuthConfig | None = None
    auth_session_id: str | None = Field(default=None, max_length=64)
    llm: EditableLlmConfig | None = None
    e2e: E2eConfig | None = None


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


def create_app(settings: Settings, runner: PipelineRunner | None = None) -> FastAPI:
    """构建 FastAPI 应用实例（runner 可注入：测试替身）。"""
    app = FastAPI(title="LLM-AutoTest orchestrator", version="0.2.0")
    app.state.settings = settings
    orch_store = ArtifactStore(
        Path(settings.artifacts_root), Path(settings.schemas_dir), "orchestrator"
    )
    te_store = ArtifactStore(
        Path(settings.artifacts_root), Path(settings.schemas_dir), "test-executor"
    )
    app.state.orch_store = orch_store
    app.state.te_store = te_store
    app.state.engine = Engine(orch_store)
    app.state.job_tasks: dict[str, asyncio.Task[None]] = {}  # type: ignore[misc]
    app.state.job_outcomes: dict[str, PipelineOutcome | dict[str, Any]] = {}  # type: ignore[misc]
    app.state.job_llm_configs = {}  # 仅运行期，禁止落盘 API key
    app.state.job_auth_headers: dict[str, dict[str, str]] = {}
    app.state.pending_auth_sessions: dict[str, dict[str, Any]] = {}
    if runner is None:
        java_client = JavaParserClient(base_url=settings.java_parser_url)
        runner = PipelineRunner(settings, app.state.engine, orch_store, te_store, java_client)
    app.state.runner = runner

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

    # ---- 基础端点 ----

    @app.get("/health")
    async def health() -> dict[str, str]:
        """健康检查（docker-compose 编排依赖）。"""
        return {"status": "ok"}

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

    @app.post("/api/v1/auth/verify")
    async def verify_authentication(request: AuthVerificationRequest) -> dict[str, Any]:
        """配置阶段真实登录；凭据头仅留在当前服务进程，绝不返回浏览器。"""
        auth_payload = request.model_dump(exclude={"job_id"})
        try:
            headers = await login_for_headers(auth_payload)
        except PipelineError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        session_id = uuid.uuid4().hex
        app.state.pending_auth_sessions[session_id] = {
            "headers": headers,
            "fingerprint": _auth_fingerprint(auth_payload),
        }
        if request.job_id:
            _require_job(app, request.job_id)
            app.state.job_auth_headers[request.job_id] = headers
        return {
            "authenticated": True,
            "session_id": session_id,
            "credential_type": "token" if "Authorization" in headers else "cookie",
        }

    @app.get("/api/v1/pipeline/nodes")
    async def list_nodes() -> dict[str, object]:
        """9 节点清单与当前状态（R-MTR-01 九节点固定口径）。"""
        state = PipelineState.initial()
        return {"nodes": [state.node_summary(name) for name in NODE_ORDER]}

    # ---- job 管理 ----

    @app.post("/api/v1/jobs", status_code=202)
    async def submit_job(request: JobSubmitRequest) -> dict[str, Any]:
        """提交全流程 job：校验 → 落盘输入 → 后台驱动 PipelineRunner。"""
        job_id = request.job_id or f"job-web-{uuid.uuid4().hex[:12]}"
        # C-12：完整正则校验（ensure_job_id 同源；不合规 400，免产物校验级联降级）
        try:
            ensure_job_id(job_id)
        except PipelineError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        if _task_alive(app, job_id):
            raise HTTPException(status_code=409, detail=f"job {job_id} 正在运行中")
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
            if repo.language not in ("java", "python"):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"仓库 {repo.name} 的 language 仅支持 java/python（C-13）：{repo.language}"
                    ),
                )
            repo_path = Path(repo.path)
            if not repo_path.is_absolute():
                raise HTTPException(
                    status_code=400,
                    detail=f"仓库 {repo.name} 的 path 须为绝对路径（Web 提交口径）：{repo.path}",
                )
            if not repo_path.is_dir():
                raise HTTPException(
                    status_code=400,
                    detail=f"仓库 {repo.name} 路径不存在或不是目录（X-CODE-01 预防）：{repo.path}",
                )
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
            # 凭据长度上限（防滥用；非安全边界——job_input.json 为本机明文落盘）
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
            "repos": [repo.model_dump() for repo in request.repos],
            "rest_url": request.rest_url,
            "rest_insecure_tls": request.rest_insecure_tls,
            "submitted_at": _now_iso(),
        }
        if document_pairs:
            job_input["document_pairs"] = document_pairs
        if auth_payload is not None:
            # C-15：凭据随 job 落盘（供 rerun/retest 复用原始输入）；
            # job_input.json 属服务内部元数据，不在产物读取白名单内（不外露）
            job_input["auth"] = auth_payload
        if llm_payload is not None:
            # 仅保存可审计的非敏感模型选择；api_key 绝不写入 job_input/产物/调用留痕。
            job_input["llm"] = {
                "model": llm_payload["model"],
                "base_url": llm_payload["base_url"],
                "max_tokens": llm_payload["max_tokens"],
            }
        if request.e2e is not None:
            job_input["e2e"] = request.e2e.model_dump()
        orch_store.job_dir(job_id).mkdir(parents=True, exist_ok=True)
        (orch_store.job_dir(job_id) / "job_input.json").write_text(
            json.dumps(job_input, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if llm_payload is not None:
            app.state.job_llm_configs[job_id] = llm_payload
        _spawn_run(app, job_id)
        return {"job_id": job_id, "status": "queued"}

    @app.get("/api/v1/jobs")
    async def list_jobs() -> dict[str, Any]:
        """job 清单（按产物目录扫描；运行中标记来自内存任务表）。"""
        jobs: list[dict[str, Any]] = []
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
    async def job_status(job_id: str) -> dict[str, Any]:
        """job 状态：9 节点状态 + 降级清单 + 产物清单（断点诊断，F-ORCH-02）。"""
        _require_job(app, job_id)
        state = json.loads((orch_store.job_dir(job_id) / "state.json").read_text(encoding="utf-8"))
        artifacts = [
            f.name
            for f in sorted(orch_store.job_dir(job_id).iterdir())
            if f.is_file() and f.name in _ARTIFACT_WHITELIST
        ]
        return {
            "job_id": job_id,
            "running": _task_alive(app, job_id),
            "updated_at": state.get("updated_at"),
            "nodes": state.get("nodes", []),
            "degraded": state.get("degraded", []),
            "artifacts": artifacts,
            # 任务输入仅暴露 E2E 开关和地址，不暴露鉴权/模型密钥；供前端动态呈现可选子流程。
            "rest_url": _job_rest_url(orch_store, job_id),
            "e2e": _job_e2e_config(orch_store, job_id),
        }

    @app.get("/api/v1/jobs/{job_id}/configuration")
    async def get_job_configuration(job_id: str) -> dict[str, Any]:
        """读取可编辑的完整任务配置，敏感字段只返回“是否已配置”。"""
        _require_job(app, job_id)
        job_input = _load_job_input(orch_store, job_id)
        auth = job_input.get("auth")
        llm = job_input.get("llm")
        doc_paths = job_input.get("doc_paths", [])
        if not doc_paths and job_input.get("doc_path"):
            doc_paths = [job_input["doc_path"]]
        return {
            "job_id": job_id,
            "documents": [Path(str(path)).name for path in doc_paths],
            "document_pairs": _public_document_pairs(job_input),
            "repos": job_input.get("repos", []),
            "rest_url": job_input.get("rest_url", ""),
            "rest_insecure_tls": bool(job_input.get("rest_insecure_tls", True)),
            "resume": bool(job_input.get("resume", True)),
            "auth": (
                {
                    "login_url": auth.get("login_url", ""),
                    "username": auth.get("username", ""),
                    "insecure_tls": bool(auth.get("insecure_tls", True)),
                    "password_configured": bool(auth.get("password")),
                }
                if isinstance(auth, dict)
                else None
            ),
            "llm": (
                {
                    "model": llm.get("model", "MiniMax-M3"),
                    "base_url": llm.get("base_url", "https://api.minimaxi.com/v1"),
                    "max_tokens": llm.get("max_tokens", 16384),
                    "api_key_configured": job_id in app.state.job_llm_configs,
                }
                if isinstance(llm, dict)
                else None
            ),
            "e2e": job_input.get("e2e") if isinstance(job_input.get("e2e"), dict) else None,
        }

    @app.put("/api/v1/jobs/{job_id}/configuration", status_code=202)
    async def update_job_configuration(
        job_id: str, request: JobConfigurationUpdateRequest
    ) -> dict[str, Any]:
        """保存整份任务配置，并从需求解析起归档旧产物后全量重新执行。"""
        _require_job(app, job_id)
        if _task_alive(app, job_id):
            raise HTTPException(
                status_code=409, detail=f"job {job_id} 正在运行中，请先停止后再修改配置"
            )
        previous = _load_job_input(orch_store, job_id)
        for repo in request.repos:
            if repo.language not in ("java", "python"):
                raise HTTPException(
                    status_code=400,
                    detail=f"仓库 {repo.name} 的 language 仅支持 java/python：{repo.language}",
                )
            repo_path = Path(repo.path)
            if not repo_path.is_absolute() or not repo_path.is_dir():
                raise HTTPException(
                    status_code=400,
                    detail=f"仓库 {repo.name} 路径不存在或不是绝对目录：{repo.path}",
                )
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
            old_auth = previous.get("auth") if isinstance(previous.get("auth"), dict) else {}
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
            api_key = request.llm.api_key or old_runtime.get("api_key")
            if not api_key:
                raise HTTPException(
                    status_code=400,
                    detail="请填写 MiniMax API Key（服务重启后无法复用内存中的密钥）",
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
            "repos": [repo.model_dump() for repo in request.repos],
            "rest_url": request.rest_url.rstrip("/"),
            "rest_insecure_tls": request.rest_insecure_tls,
            "resume": request.resume,
            "submitted_at": previous.get("submitted_at", _now_iso()),
            "updated_at": _now_iso(),
        }
        if isinstance(document_pairs, list):
            job_input["document_pairs"] = document_pairs
        if auth_payload is not None:
            job_input["auth"] = auth_payload
        if llm_payload is not None:
            job_input["llm"] = {
                key: llm_payload[key] for key in ("model", "base_url", "max_tokens")
            }
            app.state.job_llm_configs[job_id] = llm_payload
        elif isinstance(previous.get("llm"), dict):
            job_input["llm"] = previous["llm"]
        if request.e2e is not None:
            job_input["e2e"] = request.e2e.model_dump()
        (orch_store.job_dir(job_id) / "job_input.json").write_text(
            json.dumps(job_input, ensure_ascii=False, indent=2), encoding="utf-8"
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
    async def job_logs(job_id: str) -> dict[str, Any]:
        """合并当前与历次归档的 LLM 留痕，重跑后仍可在总览查看运行历史。"""
        _require_job(app, job_id)
        entries: list[dict[str, Any]] = []
        for path in orch_store.job_dir(job_id).rglob("*.jsonl"):
            if path.name not in {"llm_calls.jsonl", "review_log.jsonl", "runtime_log.jsonl"}:
                continue
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    item = json.loads(line)
                    if isinstance(item, dict):
                        entries.append(item)
            except (OSError, json.JSONDecodeError):
                continue
        unique = {str(item.get("call_id", f"{index}")): item for index, item in enumerate(entries)}
        return {"logs": sorted(unique.values(), key=lambda item: str(item.get("timestamp", "")))}

    @app.get("/api/v1/jobs/{job_id}/export")
    async def export_job(job_id: str) -> StreamingResponse:
        """导出用户可见结果产物；排除 job_input、原始文档、密钥与归档内部元数据。"""
        _require_job(app, job_id)
        root = orch_store.job_dir(job_id)
        allowed = set(_ARTIFACT_WHITELIST) | {"test_report.md", "metrics.json"}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.iterdir()):
                if path.is_file() and path.name in allowed:
                    archive.writestr(path.name, path.read_bytes())
            scripts = root / "scripts"
            if scripts.is_dir():
                for path in sorted(scripts.rglob("*.py")):
                    archive.writestr(str(path.relative_to(root)), path.read_bytes())
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{job_id}-results.zip"'},
        )

    @app.get("/api/v1/jobs/{job_id}/artifacts/{name}")
    async def read_artifact(job_id: str, name: str) -> Any:
        """读产物（白名单内；JSON 按对象返回，md/jsonl 按纯文本）。"""
        _require_job(app, job_id)
        if name not in _ARTIFACT_WHITELIST:
            raise HTTPException(status_code=404, detail=f"产物不在白名单：{name}")
        path = orch_store.job_dir(job_id) / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"产物尚未生成：{name}")
        text = path.read_text(encoding="utf-8")
        if name.endswith(".json"):
            return JSONResponse(content=json.loads(text))
        return PlainTextResponse(
            content=text,
            media_type="text/markdown" if name.endswith(".md") else "text/plain",
        )

    @app.get("/api/v1/jobs/{job_id}/report")
    async def job_report(job_id: str) -> Any:
        """测试报告（Markdown，G5 证据物之一）。"""
        _require_job(app, job_id)
        path = orch_store.job_dir(job_id) / "test_report.md"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="test_report.md 尚未生成")
        report = path.read_text(encoding="utf-8")
        return PlainTextResponse(content=report, media_type="text/markdown")

    # ---- 复测（F-DEF-02 全量 / 07 §3.5 单条）----

    @app.post("/api/v1/jobs/{job_id}/retest")
    async def retest(job_id: str, request: RetestRequest) -> dict[str, Any]:
        """复测：defect_id 空 → 后台全量（force_node，归档留痕）；指定 → 同步单条。"""
        _require_job(app, job_id)
        if _task_alive(app, job_id):
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
            auth=job_input.get("auth"),
            insecure_tls=bool(job_input.get("rest_insecure_tls", True)),
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
    async def rerun_job(job_id: str, request: RerunRequest) -> dict[str, Any]:
        """重跑（C-14）：from_node 缺省 = 全量（req_parse 级联失效）；

        级联失效：目标节点及其全部下游产物归档（带时间戳）并重置后重跑，
        避免新旧数据混用（F-ORCH-02 语义修正，docs/13 C-14）。
        """
        _require_job(app, job_id)
        if _task_alive(app, job_id):
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
        if request.rest_url is not None or request.e2e is not None:
            # 仅更新执行目标，不读取/返回 auth 或 API key。重跑的级联归档仍由 force_node 负责。
            (orch_store.job_dir(job_id) / "job_input.json").write_text(
                json.dumps(job_input, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if request.llm is not None:
            app.state.job_llm_configs[job_id] = request.llm.model_dump()
        _spawn_run(app, job_id, force_node=force_node)
        return {"job_id": job_id, "status": "rerun-queued", "from_node": force_node}

    # ---- 人工审核（F-REVW-06）----

    @app.post("/api/v1/jobs/{job_id}/review-log")
    async def review_log(job_id: str, request: ReviewLogRequest) -> dict[str, Any]:
        """人工审核记录：approved 仅记录；rejected 回流至生成节点（产物归档留痕）。"""
        _require_job(app, job_id)
        if request.decision not in ("approved", "rejected"):
            raise HTTPException(status_code=400, detail="decision 须为 approved 或 rejected")
        if _task_alive(app, job_id):
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


def _require_job(app: FastAPI, job_id: str) -> Path:
    """job 存在性校验（state.json 为准——产物为唯一事实）。"""
    job_dir: Path = app.state.orch_store.job_dir(job_id)
    if not (job_dir / "state.json").is_file() and not _task_alive(app, job_id):
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
    """读 job_input.json（提交时落盘；复测/回流重跑复用原始输入）。"""
    path = orch_store.job_dir(job_id) / "job_input.json"
    if not path.is_file():
        raise HTTPException(
            status_code=409, detail=f"job {job_id} 缺少 job_input.json（非 Web 提交）"
        )
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


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
    repositories = repository_inputs_from_config(job_input["repos"])
    # C-10：新格式 doc_paths 列表；旧 job 的单值 doc_path 兼容归一为列表
    doc_paths = [Path(p) for p in job_input.get("doc_paths", [])]
    if not doc_paths and job_input.get("doc_path"):
        doc_paths = [Path(job_input["doc_path"])]
    # C-15：鉴权配置随原始输入复用（rerun/retest 全量同样生效）
    auth = job_input.get("auth")
    auth_headers = app.state.job_auth_headers.get(job_id)
    # 重跑优先复用仍在内存中的密钥；服务重启后回退部署环境变量，绝不从磁盘读密钥。
    llm_config = app.state.job_llm_configs.get(job_id)

    async def _run() -> None:
        runner: PipelineRunner = app.state.runner
        try:
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
            if isinstance(job_input.get("document_pairs"), list):
                run_args["document_pairs"] = job_input["document_pairs"]
            if llm_config is not None:
                run_args["llm_config"] = llm_config
            outcome = await runner.run(**run_args)
            app.state.job_outcomes[job_id] = outcome
            logger.info("job %s 完成（force_node=%s）", job_id, force_node)
        except Exception:
            logger.exception("job %s 后台运行异常（force_node=%s）", job_id, force_node)
            app.state.job_outcomes[job_id] = {"job_id": job_id, "error": "后台运行异常，见服务日志"}

    app.state.job_tasks[job_id] = asyncio.create_task(_run())


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
    if pending and pending.get("fingerprint") == _auth_fingerprint(auth):
        headers = pending.get("headers")
        if isinstance(headers, dict) and all(isinstance(value, str) for value in headers.values()):
            return dict(headers)
    return None
