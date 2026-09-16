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
import json
import logging
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from orchestrator.artifacts import ArtifactStore, ensure_job_id
from orchestrator.config import Settings
from orchestrator.engine import NODE_ARTIFACTS, Engine
from orchestrator.errors import PipelineError
from orchestrator.javaparser_client import JavaParserClient
from orchestrator.nodes.retest_regression import retest_defects
from orchestrator.pipeline import NODE_ORDER, PipelineState
from orchestrator.runner import PipelineOutcome, PipelineRunner, repository_inputs_from_config

logger = logging.getLogger(__name__)

# 产物读取白名单（9 节点产物 + 留痕文件；job_input.json 属服务内部元数据不外露）
_ARTIFACT_WHITELIST: frozenset[str] = frozenset(
    name for names in NODE_ARTIFACTS.values() for name in names
) | {"llm_calls.jsonl", "review_log.jsonl"}

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


class JobSubmitRequest(BaseModel):
    """job 提交请求（文档以 base64 传递，免 python-multipart 依赖）。

    C-10：docs 列表支持多份；旧单文档字段（doc_name/doc_content_b64）
    仍接受（兼容旧客户端），两组二选一、docs 优先。
    """

    job_id: str | None = None
    docs: list[DocInput] = Field(default_factory=list)
    doc_name: str | None = None  # C-10 前旧字段（兼容保留）
    doc_content_b64: str | None = None  # C-10 前旧字段（兼容保留）
    repos: list[RepoConfig] = Field(min_length=1)  # C-10：≥1 个仓库（原 ≥2 放宽）
    rest_url: str
    resume: bool = True
    # C-15：鉴权配置（可选；填写则执行前登录被测服务注入凭据头）
    auth: AuthConfig | None = None


class RetestRequest(BaseModel):
    """复测请求：defect_id 为空 → 全量（F-DEF-02）；指定 → 单条（07 §3.5）。"""

    defect_id: str | None = None


class ReviewLogRequest(BaseModel):
    """人工审核（F-REVW-06）：approved 仅记录；rejected 回流至生成节点。"""

    decision: str  # approved | rejected
    reviewer: str = "web-user"
    note: str = ""


class RerunRequest(BaseModel):
    """重跑请求（C-14）：from_node 缺省 = 全量（从 req_parse 级联失效）。"""

    from_node: str | None = None


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
    if runner is None:
        java_client = JavaParserClient(base_url=settings.java_parser_url)
        runner = PipelineRunner(settings, app.state.engine, orch_store, te_store, java_client)
    app.state.runner = runner

    # C-12：ArtifactStore/节点层拍出的 PipelineError 统一映射 400（协议层不炸 500）
    @app.exception_handler(PipelineError)
    async def _pipeline_error_handler(
        _request: Any, exc: PipelineError
    ) -> JSONResponse:
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
        doc_paths = _save_docs(orch_store, job_id, _request_docs(request))
        for repo in request.repos:
            if repo.language not in ("java", "python"):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"仓库 {repo.name} 的 language 仅支持 java/python（C-13）："
                        f"{repo.language}"
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

        job_input: dict[str, Any] = {
            "job_id": job_id,
            "doc_paths": [str(path) for path in doc_paths],
            "repos": [repo.model_dump() for repo in request.repos],
            "rest_url": request.rest_url,
            "submitted_at": _now_iso(),
        }
        if auth_payload is not None:
            # C-15：凭据随 job 落盘（供 rerun/retest 复用原始输入）；
            # job_input.json 属服务内部元数据，不在产物读取白名单内（不外露）
            job_input["auth"] = auth_payload
        orch_store.job_dir(job_id).mkdir(parents=True, exist_ok=True)
        (orch_store.job_dir(job_id) / "job_input.json").write_text(
            json.dumps(job_input, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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
        state = json.loads(
            (orch_store.job_dir(job_id) / "state.json").read_text(encoding="utf-8")
        )
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
        }

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
        _load_job_input(orch_store, job_id)  # 非 Web 提交无原始输入 → 409 同复测口径
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


def _save_docs(orch_store: ArtifactStore, job_id: str, docs: list[DocInput]) -> list[Path]:
    """逐份校验并落盘上传文档（input/ 子目录），返回路径列表（req_parse 按后缀解析）。

    C-12 安全：文档名仅允许纯 basename（拒绝绝对路径/斜杠/..）；限额：
    文档数 ≤ MAX_DOCS、单文档解码后 ≤ MAX_DOC_BYTES。
    重名文档自动加序号后缀（不影响后缀识别）；同一请求内两格式同内容时
    以落盘顺序为准（LLM 单次解析全部拼接）。
    """
    if len(docs) > MAX_DOCS:
        raise HTTPException(
            status_code=400, detail=f"需求文档数超过上限 {MAX_DOCS} 份（C-12）"
        )
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
        doc_path = orch_store.job_dir(job_id) / "input" / name
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_bytes(content)
        paths.append(doc_path)
    return paths


def _load_job_input(orch_store: ArtifactStore, job_id: str) -> dict[str, Any]:
    """读 job_input.json（提交时落盘；复测/回流重跑复用原始输入）。"""
    path = orch_store.job_dir(job_id) / "job_input.json"
    if not path.is_file():
        raise HTTPException(
            status_code=409, detail=f"job {job_id} 缺少 job_input.json（非 Web 提交）"
        )
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


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

    async def _run() -> None:
        runner: PipelineRunner = app.state.runner
        try:
            outcome = await runner.run(
                job_id=job_id,
                doc_paths=doc_paths,
                repositories=repositories,
                rest_base_url=job_input["rest_url"],
                force_node=force_node,
                auth=auth,
            )
            app.state.job_outcomes[job_id] = outcome
            logger.info("job %s 完成（force_node=%s）", job_id, force_node)
        except Exception:
            logger.exception("job %s 后台运行异常（force_node=%s）", job_id, force_node)
            app.state.job_outcomes[job_id] = {"job_id": job_id, "error": "后台运行异常，见服务日志"}

    app.state.job_tasks[job_id] = asyncio.create_task(_run())


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（横切约定）。"""
    return datetime.now(UTC).isoformat()
