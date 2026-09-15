"""REST API 层（Web 并存入口，V2.1）：job 提交/状态/产物/复测/审核回流。

只做协议转换与校验，业务下沉 service（.rules/python-code-style）。
口径：
- POST /api/v1/jobs：提交全流程 job（文档 base64 + 仓库配置 + 被测服务地址），
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

from orchestrator.artifacts import ArtifactStore
from orchestrator.config import Settings
from orchestrator.engine import NODE_ARTIFACTS, Engine
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


class RepoConfig(BaseModel):
    """仓库配置（与 java-parser RepositoryInput 对齐；path 须为绝对路径）。"""

    name: str
    path: str
    branch: str = "main"
    is_fallback: bool = False
    include_paths: list[str] = Field(default_factory=list)


class JobSubmitRequest(BaseModel):
    """job 提交请求（文档以 base64 传递，免 python-multipart 依赖）。"""

    job_id: str | None = None
    doc_name: str
    doc_content_b64: str
    repos: list[RepoConfig]
    rest_url: str
    resume: bool = True


class RetestRequest(BaseModel):
    """复测请求：defect_id 为空 → 全量（F-DEF-02）；指定 → 单条（07 §3.5）。"""

    defect_id: str | None = None


class ReviewLogRequest(BaseModel):
    """人工审核（F-REVW-06）：approved 仅记录；rejected 回流至生成节点。"""

    decision: str  # approved | rejected
    reviewer: str = "web-user"
    note: str = ""


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
        if _task_alive(app, job_id):
            raise HTTPException(status_code=409, detail=f"job {job_id} 正在运行中")
        if not request.resume:
            # 断点续跑开关关闭 → 清空既有 job 目录重跑（F-ORCH-02 反向口径）。
            # 须在 _save_doc 之前：否则会把刚落盘的文档连同目录一起删掉（P-11）。
            existing = orch_store.job_dir(job_id)
            if existing.is_dir():
                shutil.rmtree(existing)
        doc_path = _save_doc(orch_store, job_id, request)
        for repo in request.repos:
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

        job_input = {
            "job_id": job_id,
            "doc_path": str(doc_path),
            "doc_name": request.doc_name,
            "repos": [repo.model_dump() for repo in request.repos],
            "rest_url": request.rest_url,
            "submitted_at": _now_iso(),
        }
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
                        "updated_at": state.get("updated_at"),
                        "running": _task_alive(app, str(state.get("job_id", entry.name))),
                        "completed": sum(1 for n in nodes if n.get("status") == "completed"),
                        "degraded": sum(
                            1 for n in nodes if n.get("status") == "manual_intervention"
                        ),
                        "has_report": (entry / "test_report.md").is_file(),
                    }
                )
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
            te_store, job_id, job_input["rest_url"], only_defect_ids={request.defect_id}
        )
        return {
            "job_id": job_id,
            "mode": "single",
            "defect_id": request.defect_id,
            "retested": result["retested"],
            "archived": len(result["archived"]),
            "warnings": result["warnings"],
        }

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


def _save_doc(orch_store: ArtifactStore, job_id: str, request: JobSubmitRequest) -> Path:
    """落盘上传文档（input/ 子目录），返回路径（req_parse 按后缀解析）。"""
    suffix = Path(request.doc_name).suffix.lower()
    if suffix not in _DOC_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"文档格式不支持（F-REQ-01~03：md/docx/xlsx）：{request.doc_name}",
        )
    try:
        content = base64.b64decode(request.doc_content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="doc_content_b64 不是合法 base64") from exc
    if not content:
        raise HTTPException(status_code=400, detail="文档内容为空")
    doc_path = orch_store.job_dir(job_id) / "input" / request.doc_name
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_bytes(content)
    return doc_path


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

    async def _run() -> None:
        runner: PipelineRunner = app.state.runner
        try:
            outcome = await runner.run(
                job_id=job_id,
                doc_path=Path(job_input["doc_path"]),
                repositories=repositories,
                rest_base_url=job_input["rest_url"],
                force_node=force_node,
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
