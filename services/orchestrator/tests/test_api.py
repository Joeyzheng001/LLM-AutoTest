"""REST API 层测试（Web 并存入口，V2.1）：提交/状态/产物/复测/审核回流。

FakeRunner 注入（create_app(runner=...)）：不经 LLM/java-parser，只产真实
state.json + 少量产物——API 协议与留痕行为是测点，节点语义已有专项测试。
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from helpers import requirement_review_payload
from orchestrator.api import create_app
from orchestrator.config import Settings
from orchestrator.persistence import Database, JobRecord
from orchestrator.pipeline import NODE_ORDER, NodeStatus
from orchestrator.runner import PipelineOutcome
from orchestrator.security import SecretStore

_DOC = "# 需求\n\n1. 查询用户\n2. 创建用户\n"
_GIT_BINARY = shutil.which("git") or "git"


class FakeRunner:
    """测试替身：记录调用 + 用真实 Engine 产 state.json + 落两份产物。"""

    def __init__(self, settings: Settings, engine: Any, orch_store: Any, te_store: Any) -> None:
        self.settings = settings
        self.engine = engine
        self.orch_store = orch_store
        self.te_store = te_store
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        job_id: str,
        doc_paths: list[Path],
        repositories: list[Any],
        rest_base_url: str,
        rest_insecure_tls: bool = True,
        force_node: str | None = None,
        auth: dict[str, Any] | None = None,
        auth_headers: dict[str, str] | None = None,
        llm_config: dict[str, Any] | None = None,
        document_pairs: list[dict[str, str | None]] | None = None,
        fixtures: list[dict[str, Any]] | None = None,
    ) -> PipelineOutcome:
        self.calls.append(
            {
                "job_id": job_id,
                "doc_paths": [str(path) for path in doc_paths],
                "repos": [(r.name, r.path) for r in repositories],
                "rest_base_url": rest_base_url,
                "rest_insecure_tls": rest_insecure_tls,
                "force_node": force_node,
                "auth": auth,
                "auth_headers": auth_headers,
                "llm_config": llm_config,
                "document_pairs": document_pairs,
                "fixtures": fixtures,
            }
        )
        job_dir = self.orch_store.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        # 产物先落盘（引擎产物判据：存在且过 Schema；双身份写权与流水线一致）
        self.te_store.write(job_id, "testcases.json", self._payload("testcases", job_id))
        self.te_store.write(job_id, "execution.json", self._payload("execution", job_id))
        self.te_store.write(job_id, "defects.json", self._payload("defects", job_id))
        self.te_store.write(
            job_id, "regression_index.json", self._payload("regression_index", job_id)
        )
        self.orch_store.write(
            job_id, "requirement_items.json", self._payload("requirement_items", job_id)
        )
        self.orch_store.write(job_id, "requirement_review.json", requirement_review_payload(job_id))
        self.orch_store.write(
            job_id, "code_inventory.json", self._payload("code_inventory", job_id)
        )
        self.orch_store.write(
            job_id, "interface_candidates.json", self._payload("interface_candidates", job_id)
        )
        self.orch_store.write(job_id, "interfaces.json", self._payload("interfaces", job_id))
        self.orch_store.write(
            job_id, "review_mapping.json", self._payload("review_mapping", job_id)
        )
        self.orch_store.write(job_id, "metrics.json", self._payload("metrics", job_id))
        job_dir.joinpath("test_report.md").write_text("# 测试报告（fake）\n", encoding="utf-8")
        state_path = job_dir / "state.json"
        if state_path.is_file():
            state = self.engine.open_job(job_id)
        else:
            state = self.engine.init_job(job_id)
        for name in NODE_ORDER:
            if state.node(name).status is NodeStatus.COMPLETED:
                continue
            self.engine.mark_completed(state, name)
        return PipelineOutcome(job_id=job_id, completed=list(NODE_ORDER), artifacts_dir=None)

    @staticmethod
    def _payload(kind: str, job_id: str) -> dict[str, Any]:
        """各产物最小合法形状（过 Schema 校验）。"""
        base = {"schema_version": "1.0", "job_id": job_id}
        code_location = {"repo": "repo-a", "file": "A.java", "start_line": 1, "end_line": 2}
        if kind == "requirement_items":
            base["items"] = [{"id": "REQ-001", "title": "查询用户", "description": "按 ID 查询"}]
        elif kind == "code_inventory":
            base["repositories"] = [
                {
                    "name": "repo-a",
                    "path": "/var/repos/repo-a",
                    "branch": "main",
                    "is_fallback": False,
                    "status": "ok",
                    "file_count": 1,
                },
                {
                    "name": "repo-b",
                    "path": "/var/repos/repo-b",
                    "branch": "main",
                    "is_fallback": False,
                    "status": "ok",
                    "file_count": 1,
                },
            ]
        elif kind == "interface_candidates":
            base["candidates"] = [
                {
                    "candidate_id": "CAND-001",
                    "protocol": "rest",
                    "signature": "GET /api/v1/users/{id}",
                    "code_location": code_location,
                    "is_fallback": False,
                }
            ]
        elif kind == "interfaces":
            base["interfaces"] = [
                {
                    "id": "IF-REST-001",
                    "protocol": "rest",
                    "signature": {
                        "method": "GET",
                        "path": "/api/v1/users/{id}",
                        "summary": "按 ID 查询用户",
                    },
                    "code_location": code_location,
                    "is_fallback": False,
                }
            ]
        elif kind == "review_mapping":
            base["mappings"] = [
                {"requirement_id": "REQ-001", "status": "implemented", "confidence": 0.9}
            ]
            base["risks"] = []
            base["missing_features"] = []
        elif kind == "testcases":
            base["cases"] = [
                {
                    "id": "TC-001",
                    "interface_id": "IF-REST-001",
                    "category": "normal",
                    "title": "查询",
                    "expected": "2xx",
                    "script_path": "scripts/test_TC_001.py",
                    "loadable": True,
                }
            ]
        elif kind == "execution":
            base["results"] = [
                {
                    "case_id": "TC-001",
                    "interface_id": "IF-REST-001",
                    "passed": True,
                    "timestamp": "2026-09-15T00:00:00+00:00",
                }
            ]
            base["summary"] = {
                "total": 1,
                "passed": 1,
                "failed_assertion": 0,
                "failed_environment": 0,
            }
        elif kind == "metrics":
            base["automation_rate"] = {
                "value": 1.0,
                "numerator": 9,
                "denominator": 9,
                "data_complete": True,
            }
            base["nodes"] = [{"name": n, "status": "completed"} for n in NODE_ORDER]
        elif kind == "defects":
            base["defects"] = []
        elif kind == "regression_index":
            base["suites"] = []
        return base


class BlockingRunner:
    """可取消 Runner：用于验证 API 停止会等待协程收敛再写回节点状态。"""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.started = asyncio.Event()
        self.cancelled = False

    async def run(self, *, job_id: str, **_kwargs: Any) -> PipelineOutcome:
        state = self.engine.init_job(job_id)
        self.engine.mark_running(state, "req_parse")
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("BlockingRunner 不应自然结束")


@pytest.fixture()
async def env(tmp_path: Path) -> tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path]:
    """构建注入 FakeRunner 的 app + 异步客户端。"""
    repo_dir = tmp_path / "repo-a"
    repo_dir.mkdir()
    schemas = Path(__file__).resolve().parents[3] / "schemas"
    settings = Settings(
        port=8000,
        java_parser_url="http://mock:8080",
        artifacts_root=str(tmp_path / "artifacts"),
        llm_base_url="http://mock:8090/v1",
        llm_model_name="glm-5.3",
        llm_api_key="",
        llm_temperature=0.2,
        llm_top_p=0.8,
        llm_seed=None,
        llm_timeout_s=5.0,
        schemas_dir=str(schemas),
        repositories_root=str(repo_dir.parent),
    )
    app = create_app(settings)
    fake = FakeRunner(settings, app.state.engine, app.state.orch_store, app.state.te_store)
    app.state.runner = fake
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    yield app, fake, client, repo_dir
    await client.aclose()


@pytest.mark.asyncio
async def test_access_mode_endpoint_defaults_to_single_user(env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path]) -> None:
    _app, _runner, client, _repo_dir = env
    response = await client.get("/api/v1/system/access-mode")
    assert response.status_code == 200
    assert response.json() == {"mode": "single_user", "user": ""}


def _submit_body(repo_dir: Path, job_id: str | None = None, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "job_id": job_id,
        "doc_name": "requirements.md",
        "doc_content_b64": base64.b64encode(_DOC.encode()).decode(),
        "repos": [{"name": "repo-a", "path": str(repo_dir), "branch": "main"}],
        "rest_url": "http://127.0.0.1:9000",
    }
    body.update(overrides)
    return body


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - tests invoke a fixed local git executable
        [_GIT_BINARY, *args], cwd=cwd, check=True, capture_output=True
    )


async def _await_task(app: FastAPI, job_id: str) -> None:
    """等后台任务收敛（FakeRunner 立即完成，yield 即可）。"""
    task = app.state.job_tasks.get(job_id)
    if task is not None:
        await task


async def test_submit_job_happy_path(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """提交（旧单文档字段兼容路径）→ 202；doc/job_input 落盘；FakeRunner 拿到正确入参。"""
    app, fake, client, repo_dir = env
    response = await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-0001"))
    assert response.status_code == 202
    assert response.json() == {"job_id": "job-api-0001", "status": "queued"}
    await _await_task(app, "job-api-0001")
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["job_id"] == "job-api-0001"
    job_dir = Path(str(app.state.settings.artifacts_root)) / "job-api-0001"
    assert call["doc_paths"] == [str(job_dir / "input" / "requirements.md")]
    assert call["repos"] == [("repo-a", str(repo_dir))]
    assert call["rest_base_url"] == "http://127.0.0.1:9000"
    assert call["rest_insecure_tls"] is True
    assert (job_dir / "input" / "requirements.md").read_text(encoding="utf-8") == _DOC
    saved = json.loads((job_dir / "job_input.json").read_text(encoding="utf-8"))
    assert saved["doc_paths"] == [str(job_dir / "input" / "requirements.md")]
    assert saved["rest_url"] == "http://127.0.0.1:9000"
    assert saved["rest_insecure_tls"] is True


async def test_job_logs_keeps_run_scope_and_same_call_id_from_reruns(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """重跑追加日志时，同名 call_id 也不能覆盖历史轮次。"""
    app, _fake, client, repo_dir = env
    response = await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-log1"))
    assert response.status_code == 202
    await _await_task(app, "job-api-log1")
    job_dir = Path(str(app.state.settings.artifacts_root)) / "job-api-log1"
    current = {
        "call_id": "LLM-same-001",
        "run_id": "run-new",
        "timestamp": "2026-09-21T01:00:00+00:00",
        "node": "code_review",
        "ok": True,
    }
    archived = {
        "call_id": "LLM-same-001",
        "run_id": "run-old",
        "timestamp": "2026-09-20T01:00:00+00:00",
        "node": "code_review",
        "ok": True,
    }
    (job_dir / "llm_calls.jsonl").write_text(json.dumps(current) + "\n", encoding="utf-8")
    archive = job_dir / "archive" / "code_review"
    archive.mkdir(parents=True)
    (archive / "llm_calls.jsonl").write_text(json.dumps(archived) + "\n", encoding="utf-8")

    logs_response = await client.get("/api/v1/jobs/job-api-log1/logs")
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]
    assert len(logs) == 2
    assert {item["run_scope"] for item in logs} == {"current", "archive"}
    assert {item["run_id"] for item in logs} == {"run-new", "run-old"}


async def test_preflight_checks_configuration_without_creating_job(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    app, _fake, client, repo_dir = env
    response = await client.post("/api/v1/preflight", json=_submit_body(repo_dir, "job-api-pf1"))
    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert {item["name"] for item in payload["checks"]} == {
        "documents",
        "repositories",
        "rest",
        "auth",
        "llm",
    }
    assert not (Path(str(app.state.settings.artifacts_root)) / "job-api-pf1").exists()


async def test_repository_root_endpoint_and_relative_path_are_server_resolved(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    app, fake, client, repo_dir = env
    root_response = await client.get("/api/v1/system/repository-root")
    assert root_response.status_code == 200
    assert root_response.json() == {"root": str(repo_dir.parent), "exists": True}

    body = _submit_body(
        repo_dir,
        "job-api-relative-repo",
        repos=[{"name": "repo-a", "path": "repo-a", "branch": "main"}],
    )
    response = await client.post("/api/v1/jobs", json=body)
    assert response.status_code == 202
    await _await_task(app, "job-api-relative-repo")
    assert fake.calls[-1]["repos"] == [("repo-a", str(repo_dir))]


async def test_submit_job_remote_git_keeps_token_out_of_job_input(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """内部 Git 配置可提交，令牌只进入运行期/SecretStore，不写普通任务配置。"""
    app, fake, client, repo_dir = env
    source = repo_dir.parent / "remote-source"
    source.mkdir()
    _run_git(source, "init", "-b", "main")
    _run_git(source, "config", "user.email", "test@example.com")
    _run_git(source, "config", "user.name", "test")
    (source / "README.md").write_text("remote\n", encoding="utf-8")
    _run_git(source, "add", ".")
    _run_git(source, "commit", "-m", "init")
    bare = repo_dir.parent / "remote.git"
    _run_git(repo_dir.parent, "clone", "--bare", str(source), str(bare))
    body = _submit_body(
        repo_dir,
        "job-api-remote1",
        repos=[
            {
                "name": "remote-service",
                "source": "git",
                "remote_url": bare.as_uri(),
                "branch": "main",
                "access_token": "token-should-not-be-persisted",
            }
        ],
    )
    response = await client.post("/api/v1/jobs", json=body)
    assert response.status_code == 202
    await _await_task(app, "job-api-remote1")
    saved = json.loads(
        (
            Path(str(app.state.settings.artifacts_root))
            / "job-api-remote1"
            / "job_input.json"
        ).read_text(encoding="utf-8")
    )
    assert "access_token" not in saved["repos"][0]
    # 运行完成后，Git token 不应继续留在进程级运行期映射中；执行期间已由
    # resolver 使用，重跑/服务重启场景由 SecretStore 恢复。
    assert "job-api-remote1" not in app.state.job_repo_tokens
    assert fake.calls[-1]["repos"][0][0] == "remote-service"


async def test_submit_job_persists_fixture_protocol_and_passes_to_runner(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """P0-05：夹具配置随任务保存并传入流水线，不进入 LLM。"""
    app, fake, client, repo_dir = env
    body = _submit_body(repo_dir, "job-api-fixture1")
    body["fixtures"] = [
        {
            "name": "seed-user",
            "phase": "setup",
            "method": "POST",
            "path": "/fixtures/users",
            "body": {"id": 1},
            "expected": "status=201",
            "required": True,
        }
    ]
    response = await client.post("/api/v1/jobs", json=body)
    assert response.status_code == 202
    await _await_task(app, "job-api-fixture1")
    saved = json.loads(
        (
            Path(str(app.state.settings.artifacts_root))
            / "job-api-fixture1"
            / "job_input.json"
        ).read_text(encoding="utf-8")
    )
    assert saved["fixtures"][0]["name"] == "seed-user"
    assert fake.calls[-1]["fixtures"] == saved["fixtures"]


async def test_database_backed_queue_claims_and_releases_job(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """配置数据库后，API 队列实际写入任务租约并在执行后释放。"""
    base_app, _, _, repo_dir = env
    db_path = repo_dir.parent / "queue-state.db"
    artifacts_root = repo_dir.parent / "database-artifacts"
    settings = replace(
        base_app.state.settings,
        artifacts_root=str(artifacts_root),
        database_url=f"sqlite:///{db_path}",
        database_auto_create=True,
    )
    app = create_app(settings)
    fake = FakeRunner(settings, app.state.engine, app.state.orch_store, app.state.te_store)
    app.state.runner = fake
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        response = await client.post(
            "/api/v1/jobs", json=_submit_body(repo_dir, "job-api-db01")
        )
        assert response.status_code == 202
        await _await_task(app, "job-api-db01")
        database = Database(f"sqlite:///{db_path}")
        with database.session() as session:
            record = session.get(JobRecord, "job-api-db01")
            assert record is not None
            assert record.status == "completed"
            assert record.worker_id is None
    finally:
        await client.aclose()
        await app.state.job_queue.shutdown()


async def test_database_backed_job_views_survive_missing_state_file(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """多实例部署中本地卷暂不可见时，任务列表/详情仍由数据库索引返回。"""
    base_app, _, _, repo_dir = env
    db_path = repo_dir.parent / "authoritative-state.db"
    artifacts_root = repo_dir.parent / "authoritative-artifacts"
    settings = replace(
        base_app.state.settings,
        artifacts_root=str(artifacts_root),
        database_url=f"sqlite:///{db_path}",
        database_auto_create=True,
    )
    app = create_app(settings)
    fake = FakeRunner(settings, app.state.engine, app.state.orch_store, app.state.te_store)
    app.state.runner = fake
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        response = await client.post(
            "/api/v1/jobs", json=_submit_body(repo_dir, "job-api-db-index")
        )
        assert response.status_code == 202
        await _await_task(app, "job-api-db-index")
        database = Database(f"sqlite:///{db_path}")
        for node_name in NODE_ORDER:
            database.record_node("job-api-db-index", node_name, status="completed", attempts=1)
        state_path = artifacts_root / "job-api-db-index" / "state.json"

        # 文件快照若比数据库更晚（例如数据库短暂不可用），应保留文件侧的新状态。
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["nodes"][0]["status"] = "pending"
        state["nodes"][0]["updated_at"] = "2099-01-01T00:00:00+00:00"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        merged = await client.get("/api/v1/jobs")
        assert merged.json()["jobs"][0]["completed"] == len(NODE_ORDER) - 1

        state_path.unlink()

        listed = await client.get("/api/v1/jobs")
        assert listed.status_code == 200
        assert listed.json()["jobs"][0]["job_id"] == "job-api-db-index"
        assert listed.json()["jobs"][0]["completed"] == len(NODE_ORDER)

        detail = await client.get("/api/v1/jobs/job-api-db-index")
        assert detail.status_code == 200
        assert len(detail.json()["nodes"]) == len(NODE_ORDER)
        assert detail.json()["artifacts"]
    finally:
        await client.aclose()
        await app.state.job_queue.shutdown()


async def test_database_backed_configuration_survives_missing_job_input(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """任务目录中的配置文件不可见时，配置页从数据库快照恢复。"""
    base_app, _, _, repo_dir = env
    db_path = repo_dir.parent / "configuration-state.db"
    artifacts_root = repo_dir.parent / "configuration-artifacts"
    settings = replace(
        base_app.state.settings,
        artifacts_root=str(artifacts_root),
        database_url=f"sqlite:///{db_path}",
        database_auto_create=True,
    )
    app = create_app(settings)
    fake = FakeRunner(settings, app.state.engine, app.state.orch_store, app.state.te_store)
    app.state.runner = fake
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        response = await client.post(
            "/api/v1/jobs", json=_submit_body(repo_dir, "job-api-db-config")
        )
        assert response.status_code == 202
        await _await_task(app, "job-api-db-config")
        (artifacts_root / "job-api-db-config" / "job_input.json").unlink()

        configuration = await client.get("/api/v1/jobs/job-api-db-config/configuration")
        assert configuration.status_code == 200
        assert configuration.json()["rest_url"] == "http://127.0.0.1:9000"
        assert configuration.json()["documents"] == ["requirements.md"]
        database = Database(f"sqlite:///{db_path}")
        database.append_event(
            "job-api-db-config",
            "runtime_node_started",
            node_name="req_parse",
            payload={
                "timestamp": "2026-09-21T00:00:00+00:00",
                "run_id": "run-db",
                "event": "node_started",
                "node": "req_parse",
                "message": "数据库事件回退",
            },
        )
        logs = await client.get("/api/v1/jobs/job-api-db-config/logs")
        assert logs.status_code == 200
        assert any(item["run_scope"] == "database" for item in logs.json()["logs"])
    finally:
        await client.aclose()
        await app.state.job_queue.shutdown()


async def test_database_queued_job_is_reenqueued_on_startup(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """服务启动时，数据库中的 queued 任务会自动恢复到本实例队列。"""
    base_app, _, _, repo_dir = env
    db_path = repo_dir.parent / "startup-recovery.db"
    artifacts_root = repo_dir.parent / "startup-artifacts"
    settings = replace(
        base_app.state.settings,
        artifacts_root=str(artifacts_root),
        database_url=f"sqlite:///{db_path}",
        database_auto_create=True,
    )
    app = create_app(settings)
    fake = FakeRunner(settings, app.state.engine, app.state.orch_store, app.state.te_store)
    app.state.runner = fake
    job_id = "job-api-recover01"
    job_dir = app.state.orch_store.job_dir(job_id)
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True)
    doc_path = input_dir / "requirements.md"
    doc_path.write_text(_DOC, encoding="utf-8")
    (job_dir / "job_input.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "doc_paths": [str(doc_path)],
                "repos": [{"name": "repo-a", "path": str(repo_dir), "branch": "main"}],
                "rest_url": "http://127.0.0.1:9000",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app.state.database.ensure_job(job_id)
    try:
        for handler in app.router.on_startup:
            await handler()
        await _await_task(app, job_id)
        assert len(fake.calls) == 1
        with app.state.database.session() as session:
            record = session.get(JobRecord, job_id)
            assert record is not None
            assert record.status == "completed"
    finally:
        for handler in app.router.on_shutdown:
            await handler()


async def test_submit_job_multi_docs(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """C-10：docs[] 多文档提交——两份落盘、doc_paths 入参列表、FakeRunner 收到两份。"""
    app, fake, client, repo_dir = env
    doc_b = "# 补充需求\n\n3. 删除用户\n"
    doc_b64 = base64.b64encode(_DOC.encode()).decode()
    extra_b64 = base64.b64encode(doc_b.encode()).decode()
    body = _submit_body(repo_dir, "job-api-md01")
    body["docs"] = [
        {"doc_name": "requirements.md", "doc_content_b64": doc_b64},
        {"doc_name": "extra.md", "doc_content_b64": extra_b64},
    ]
    del body["doc_name"], body["doc_content_b64"]
    response = await client.post("/api/v1/jobs", json=body)
    assert response.status_code == 202
    await _await_task(app, "job-api-md01")
    job_dir = Path(str(app.state.settings.artifacts_root)) / "job-api-md01"
    assert (job_dir / "input" / "requirements.md").read_text(encoding="utf-8") == _DOC
    assert (job_dir / "input" / "extra.md").read_text(encoding="utf-8") == doc_b
    assert len(fake.calls) == 1
    assert len(fake.calls[0]["doc_paths"]) == 2


async def test_submit_job_version_document_pairs(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """每行被测/基线文档保存成显式配对元数据，并仅把被测版本传给旧 runner 输入。"""
    app, fake, client, repo_dir = env
    body = _submit_body(repo_dir, "job-api-pair1")
    body.pop("doc_name")
    body.pop("doc_content_b64")
    body["document_pairs"] = [
        {
            "target": {
                "doc_name": "target.md",
                "doc_content_b64": base64.b64encode(b"new").decode(),
            },
            "baseline": {
                "doc_name": "baseline.md",
                "doc_content_b64": base64.b64encode(b"old").decode(),
            },
        },
        {"target": {"doc_name": "single.md", "doc_content_b64": base64.b64encode(b"one").decode()}},
    ]
    assert (await client.post("/api/v1/jobs", json=body)).status_code == 202
    await _await_task(app, "job-api-pair1")
    saved = json.loads(
        (
            Path(str(app.state.settings.artifacts_root)) / "job-api-pair1" / "job_input.json"
        ).read_text()
    )
    assert len(saved["document_pairs"]) == 2
    assert saved["document_pairs"][0]["baseline_path"].endswith("baseline.md")
    assert fake.calls[-1]["document_pairs"] == saved["document_pairs"]
    config = (await client.get("/api/v1/jobs/job-api-pair1/configuration")).json()
    assert config["document_pairs"] == [
        {"target_name": "target.md", "baseline_name": "baseline.md"},
        {"target_name": "single.md", "baseline_name": None},
    ]


async def test_submit_job_docs_and_legacy_validations(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """C-10 文档字段拒绝路径：两组均空 / 旧字段只填一半 / docs 与旧字段同时给（docs 优先）。"""
    app, _fake, client, repo_dir = env
    empty = _submit_body(repo_dir, "job-api-d0")
    del empty["doc_name"], empty["doc_content_b64"]
    assert (await client.post("/api/v1/jobs", json=empty)).status_code == 400
    half = _submit_body(repo_dir, "job-api-d1")
    del half["doc_content_b64"]
    assert (await client.post("/api/v1/jobs", json=half)).status_code == 400
    # 两组同时给：docs 优先（不报错，旧字段忽略）
    both = _submit_body(repo_dir, "job-api-d2")
    both["docs"] = [
        {"doc_name": "only.md", "doc_content_b64": base64.b64encode(b"# only").decode()}
    ]
    assert (await client.post("/api/v1/jobs", json=both)).status_code == 202
    await _await_task(app, "job-api-d2")
    job_dir = Path(str(app.state.settings.artifacts_root)) / "job-api-d2"
    assert (job_dir / "input" / "only.md").is_file()
    assert not (job_dir / "input" / "requirements.md").exists()


async def test_submit_job_empty_repos_rejected(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """C-10：空仓库列表拒绝（≥1）；单仓库合法通过。"""
    app, fake, client, repo_dir = env
    empty = _submit_body(repo_dir, "job-api-r0")
    empty["repos"] = []
    assert (await client.post("/api/v1/jobs", json=empty)).status_code == 422  # Pydantic min_length
    # 单仓库（C-10 放宽后合法）
    response = await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-r1"))
    assert response.status_code == 202
    await _await_task(app, "job-api-r1")
    assert fake.calls[-1]["repos"] == [("repo-a", str(repo_dir))]


async def test_submit_job_invalid_job_id_rejected(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """非 job- 前缀 job_id 提交入口拒绝（横切 ID 约定），不建目录不跑流水线。

    实例：中文任务名提交曾致 9 节点全部因产物 schema ^job- 校验级联降级。
    """
    app, fake, client, repo_dir = env
    body = _submit_body(repo_dir, "第一次全局测试")
    response = await client.post("/api/v1/jobs", json=body)
    assert response.status_code == 400
    assert "job-" in response.json()["detail"]
    assert fake.calls == []
    assert not (Path(str(app.state.settings.artifacts_root)) / "第一次全局测试").exists()


async def test_submit_job_traversal_job_id_rejected(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """C-12：带 ../ 或路径分隔符的 job_id → 400，不建目录不跑流水线（防穿越 rmtree）。"""
    app, fake, client, repo_dir = env
    artifacts_root = Path(str(app.state.settings.artifacts_root))
    for bad_id in ("job-../../evil", "job-a/b", "job-a\\b", "job-.."):
        response = await client.post("/api/v1/jobs", json=_submit_body(repo_dir, bad_id))
        assert response.status_code == 400, f"{bad_id} 应被完整正则拒绝"
        assert "C-12" in response.json()["detail"]
    assert fake.calls == []
    # 产物卷根之外无任何新建目录（穿越未得逞）
    assert not (artifacts_root.parent / "evil").exists()
    assert list(artifacts_root.glob("job-a*")) == []


async def test_submit_job_doc_name_traversal_rejected(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """C-12：文档名绝对路径 / ../ / 子路径 / 控制字符 → 400，不落盘任务目录之外。"""
    app, _fake, client, repo_dir = env
    artifacts_root = Path(str(app.state.settings.artifacts_root))
    for bad_name in ("../evil.md", "/abs/evil.md", "sub/x.md", "a..b.md\n"):
        body = _submit_body(repo_dir, "job-api-sec1")
        body["doc_name"] = bad_name
        response = await client.post("/api/v1/jobs", json=body)
        assert response.status_code == 400, f"{bad_name!r} 应被纯 basename 口径拒绝"
        assert "文档名非法" in response.json()["detail"]
    # 任务目录未建立、卷外无文件（任意写入未得逞）
    assert not (artifacts_root / "job-api-sec1").exists()
    assert not (artifacts_root.parent / "evil.md").exists()


async def test_submit_job_docs_count_and_size_limits(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """C-12：文档数超 MAX_DOCS（10 份）→ 400；单文档超 10MB → 400。"""
    app, _fake, client, repo_dir = env
    artifacts_root = Path(str(app.state.settings.artifacts_root))
    doc_b64 = base64.b64encode(_DOC.encode()).decode()
    overflow = {"docs": [{"doc_name": f"d{i}.md", "doc_content_b64": doc_b64} for i in range(11)]}
    body = _submit_body(repo_dir, "job-api-sec2", **overflow)
    del body["doc_name"], body["doc_content_b64"]
    response = await client.post("/api/v1/jobs", json=body)
    assert response.status_code == 400
    assert "上限 10 份" in response.json()["detail"]

    huge = base64.b64encode(b"x" * (10 * 1024 * 1024 + 1)).decode()
    body = _submit_body(repo_dir, "job-api-sec3", doc_content_b64=huge)
    response = await client.post("/api/v1/jobs", json=body)
    assert response.status_code == 400
    assert "超过上限" in response.json()["detail"]
    assert not (artifacts_root / "job-api-sec3").exists()


async def test_request_body_too_large_413(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """C-12：content-length 超 MAX_REQUEST_BYTES（64MB）→ 413（解析前拒绝）。"""
    _app, fake, client, _repo_dir = env
    request = client.build_request("POST", "/api/v1/jobs", json={})
    request.headers["Content-Length"] = str(65 * 1024 * 1024)
    response = await client.send(request)
    assert response.status_code == 413
    assert fake.calls == []


async def test_submit_job_repo_language_validation(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """C-13：仓库 language 仅支持 java/python；python 合法提交入参透传。"""
    app, fake, client, repo_dir = env
    body = _submit_body(repo_dir, "job-api-lang0")
    body["repos"][0]["language"] = "go"
    response = await client.post("/api/v1/jobs", json=body)
    assert response.status_code == 400
    assert "language 仅支持 java/python" in response.json()["detail"]
    assert fake.calls == []

    body = _submit_body(repo_dir, "job-api-lang1")
    body["repos"][0]["language"] = "python"
    response = await client.post("/api/v1/jobs", json=body)
    assert response.status_code == 202
    await _await_task(app, "job-api-lang1")
    saved = json.loads(
        (
            Path(str(app.state.settings.artifacts_root)) / "job-api-lang1" / "job_input.json"
        ).read_text(encoding="utf-8")
    )
    assert saved["repos"][0]["language"] == "python"


async def test_submit_job_repo_service_base_url_validation(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """C-07：仓库级接口服务地址仅允许 http(s)，避免把路由配置静默吞掉。"""
    _app, fake, client, repo_dir = env
    body = _submit_body(repo_dir, "job-api-route0")
    body["repos"][0]["service_base_url"] = "ftp://not-an-http-service"
    response = await client.post("/api/v1/jobs", json=body)
    assert response.status_code == 400
    assert "service_base_url 须为 http(s) 地址" in response.json()["detail"]
    assert fake.calls == []

    valid = _submit_body(repo_dir, "job-api-route1")
    valid["repos"][0]["service_base_url"] = "http://user-service.internal:9000/"
    response = await client.post("/api/v1/jobs", json=valid)
    assert response.status_code == 202
    await _await_task(_app, "job-api-route1")
    saved = json.loads(
        (
            Path(str(_app.state.settings.artifacts_root)) / "job-api-route1" / "job_input.json"
        ).read_text(encoding="utf-8")
    )
    assert saved["repos"][0]["service_base_url"] == "http://user-service.internal:9000/"


async def test_submit_job_validations(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """非法提交的四种拒绝路径：格式/相对路径/坏 base64/坏 rest_url。"""
    _app, _fake, client, repo_dir = env
    bad_doc = _submit_body(repo_dir, "v1")
    bad_doc["doc_name"] = "requirements.pdf"
    assert (await client.post("/api/v1/jobs", json=bad_doc)).status_code == 400
    bad_rel = _submit_body(repo_dir, "v2")
    bad_rel["repos"] = [{"name": "r", "path": "relative/path"}]
    assert (await client.post("/api/v1/jobs", json=bad_rel)).status_code == 400
    bad_b64 = _submit_body(repo_dir, "v3")
    bad_b64["doc_content_b64"] = "!!not-base64!!"
    assert (await client.post("/api/v1/jobs", json=bad_b64)).status_code == 400
    bad_url = _submit_body(repo_dir, "v4")
    bad_url["rest_url"] = "ftp://x"
    assert (await client.post("/api/v1/jobs", json=bad_url)).status_code == 400


async def test_submit_job_auth_validation(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """C-15：鉴权配置三种拒绝路径：坏 login_url/空密码/超长凭据。"""
    _app, fake, client, repo_dir = env
    bad_url = _submit_body(repo_dir, "job-auth-v1")
    bad_url["auth"] = {
        "login_url": "ftp://x/login",
        "username": "admin",
        "password": "admin123456",
    }
    response = await client.post("/api/v1/jobs", json=bad_url)
    assert response.status_code == 400
    assert "login_url" in response.json()["detail"]
    empty_pw = _submit_body(repo_dir, "job-auth-v2")
    empty_pw["auth"] = {
        "login_url": "http://127.0.0.1:9000/api/auth/login/",
        "username": "admin",
        "password": "",
    }
    response = await client.post("/api/v1/jobs", json=empty_pw)
    assert response.status_code == 400
    assert "username/password 不能为空" in response.json()["detail"]
    too_long = _submit_body(repo_dir, "job-auth-v3")
    too_long["auth"] = {
        "login_url": "http://127.0.0.1:9000/api/auth/login/",
        "username": "admin",
        "password": "x" * 257,
    }
    response = await client.post("/api/v1/jobs", json=too_long)
    assert response.status_code == 400
    assert "超长" in response.json()["detail"]
    assert fake.calls == []


async def test_submit_job_auth_persisted_and_passed(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """C-15：合法 auth → job_input.json 落盘 + FakeRunner 收到入参；未填 auth 时为 None。"""
    app, fake, client, repo_dir = env
    body = _submit_body(repo_dir, "job-api-auth1")
    body["auth"] = {
        "login_url": "https://10.50.16.19:8443/api/auth/login/",
        "username": "admin",
        "password": "admin123456",
        "insecure_tls": True,
    }
    response = await client.post("/api/v1/jobs", json=body)
    assert response.status_code == 202
    await _await_task(app, "job-api-auth1")
    saved = json.loads(
        (
            Path(str(app.state.settings.artifacts_root)) / "job-api-auth1" / "job_input.json"
        ).read_text(encoding="utf-8")
    )
    assert saved["auth"] == {
        "login_url": "https://10.50.16.19:8443/api/auth/login/",
        "username": "admin",
        "password": "admin123456",
        "insecure_tls": True,
    }
    assert fake.calls[-1]["auth"] == saved["auth"]
    # 未填 auth 的提交 → runner 收到 None（鉴权可选，不影响既有链路）
    response = await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-auth2"))
    assert response.status_code == 202
    await _await_task(app, "job-api-auth2")
    assert fake.calls[-1]["auth"] is None


async def test_submit_job_uses_encrypted_secret_store_when_configured(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """配置主密钥后，job_input 只留元数据，重跑仍可恢复完整凭据。"""
    app, _fake, _client, repo_dir = env
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("ascii")
    secure_settings = replace(app.state.settings, secrets_encryption_key=key)
    secure_app = create_app(secure_settings)
    secure_fake = FakeRunner(
        secure_settings,
        secure_app.state.engine,
        secure_app.state.orch_store,
        secure_app.state.te_store,
    )
    secure_app.state.runner = secure_fake
    secure_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=secure_app), base_url="http://test"
    )
    try:
        body = _submit_body(repo_dir, "job-api-secure1")
        body["auth"] = {
            "login_url": "https://example.test/login/",
            "username": "admin",
            "password": "secret-password",
        }
        body["llm"] = {
            "api_key": "sk-secure-test-key",
            "model": "MiniMax-M3",
            "base_url": "https://api.minimaxi.com/v1",
            "max_tokens": 8192,
        }
        response = await secure_client.post("/api/v1/jobs", json=body)
        assert response.status_code == 202
        await _await_task(secure_app, "job-api-secure1")

        root = Path(str(secure_app.state.settings.artifacts_root)) / "job-api-secure1"
        saved = json.loads((root / "job_input.json").read_text(encoding="utf-8"))
        assert saved["auth"]["password_configured"] is True
        assert "password" not in saved["auth"]
        assert "api_key" not in saved.get("llm", {})
        blob = root / ".job-credentials.enc"
        assert blob.is_file()
        assert body["auth"]["password"].encode() not in blob.read_bytes()
        assert secure_fake.calls[-1]["auth"]["password"] == body["auth"]["password"]
        assert secure_fake.calls[-1]["llm_config"]["api_key"] == "sk-secure-test-key"
        assert isinstance(secure_app.state.secret_store, SecretStore)

        config_response = await secure_client.get("/api/v1/jobs/job-api-secure1/configuration")
        assert config_response.status_code == 200
        config = config_response.json()
        assert config["auth"]["password_configured"] is True
        assert config["llm"]["api_key_configured"] is True

        restarted_app = create_app(secure_settings)
        restarted_fake = FakeRunner(
            secure_settings,
            restarted_app.state.engine,
            restarted_app.state.orch_store,
            restarted_app.state.te_store,
        )
        restarted_app.state.runner = restarted_fake
        restarted_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted_app), base_url="http://test"
        )
        try:
            rerun = await restarted_client.post(
                "/api/v1/jobs/job-api-secure1/rerun",
                json={"from_node": "auto_execute"},
            )
            assert rerun.status_code == 200
            await _await_task(restarted_app, "job-api-secure1")
            assert restarted_fake.calls[-1]["auth"]["password"] == body["auth"]["password"]
            assert restarted_fake.calls[-1]["llm_config"]["api_key"] == body["llm"]["api_key"]
        finally:
            await restarted_client.aclose()
    finally:
        await secure_client.aclose()


async def test_access_policy_scopes_jobs_by_token_and_project(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """启用部门令牌后，任务 owner/project 由服务端确定并隔离读写。"""
    app, _fake, _client, repo_dir = env
    access_settings = replace(
        app.state.settings,
        access_tokens_json=json.dumps(
            {
                "token-alice": {"user_id": "alice", "projects": ["fund"]},
                "token-bob": {"user_id": "bob", "projects": ["fund"]},
            }
        ),
    )
    access_app = create_app(access_settings)
    access_fake = FakeRunner(
        access_settings,
        access_app.state.engine,
        access_app.state.orch_store,
        access_app.state.te_store,
    )
    access_app.state.runner = access_fake
    access_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=access_app), base_url="http://test"
    )
    try:
        body = _submit_body(repo_dir, "job-api-scope1")
        no_token = await access_client.post("/api/v1/jobs", json=body)
        assert no_token.status_code == 401
        alice_headers = {
            "Authorization": "Bearer token-alice",
            "X-Project-Id": "fund",
        }
        created = await access_client.post("/api/v1/jobs", json=body, headers=alice_headers)
        assert created.status_code == 202
        await _await_task(access_app, "job-api-scope1")
        saved = json.loads(
            (
                Path(str(access_app.state.settings.artifacts_root))
                / "job-api-scope1"
                / "job_input.json"
            ).read_text(encoding="utf-8")
        )
        assert saved["owner_id"] == "alice"
        assert saved["project_id"] == "fund"

        bob_headers = {"Authorization": "Bearer token-bob", "X-Project-Id": "fund"}
        hidden = await access_client.get("/api/v1/jobs/job-api-scope1", headers=bob_headers)
        assert hidden.status_code == 404
        visible = await access_client.get("/api/v1/jobs/job-api-scope1", headers=alice_headers)
        assert visible.status_code == 200
        listed = await access_client.get("/api/v1/jobs", headers=bob_headers)
        assert listed.status_code == 200
        assert listed.json()["jobs"] == []
    finally:
        await access_client.aclose()


async def test_department_quota_and_audit_are_enforced(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
    tmp_path: Path,
) -> None:
    """部门令牌可限制日配额，并能读取脱敏操作审计。"""
    app, _fake, _client, repo_dir = env
    access_settings = replace(
        app.state.settings,
        database_url=f"sqlite:///{tmp_path / 'quota-api.db'}",
        database_auto_create=True,
        access_tokens_json=json.dumps(
            {
                "token-alice": {
                    "user_id": "alice",
                    "projects": ["fund"],
                    "daily_job_limit": 1,
                }
            }
        ),
    )
    quota_app = create_app(access_settings)
    quota_fake = FakeRunner(
        access_settings,
        quota_app.state.engine,
        quota_app.state.orch_store,
        quota_app.state.te_store,
    )
    quota_app.state.runner = quota_fake
    quota_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=quota_app), base_url="http://test"
    )
    headers = {"Authorization": "Bearer token-alice", "X-Project-Id": "fund"}
    try:
        first = await quota_client.post(
            "/api/v1/jobs", json=_submit_body(repo_dir, "job-api-quota1"), headers=headers
        )
        assert first.status_code == 202
        await _await_task(quota_app, "job-api-quota1")

        second = await quota_client.post(
            "/api/v1/jobs", json=_submit_body(repo_dir, "job-api-quota2"), headers=headers
        )
        assert second.status_code == 429
        assert "日配额" in second.json()["detail"]

        rerun = await quota_client.post(
            "/api/v1/jobs/job-api-quota1/rerun",
            json={"from_node": "auto_execute"},
            headers=headers,
        )
        assert rerun.status_code == 200
        await _await_task(quota_app, "job-api-quota1")

        audit = await quota_client.get("/api/v1/jobs/job-api-quota1/audit", headers=headers)
        assert audit.status_code == 200
        assert audit.json()["storage"] == "database"
        event_types = [item["event_type"] for item in audit.json()["events"]]
        assert "job_submitted" in event_types
        assert any(item.startswith("audit_") for item in event_types)
    finally:
        await quota_client.aclose()


async def test_verify_authentication_returns_opaque_session_only(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置预检会话不返回 Token；仅服务内存持有，可在保存任务时接管。"""
    app, _fake, client, _repo_dir = env

    async def fake_login(auth: dict[str, Any]) -> dict[str, str]:
        assert auth["password"]
        return {"Authorization": "Token never-return-this-value"}

    monkeypatch.setattr("orchestrator.api.login_for_headers", fake_login)
    response = await client.post(
        "/api/v1/auth/verify",
        json={
            "login_url": "https://example.test/login/",
            "username": "admin",
            "password": "secret-password",
            "insecure_tls": True,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["authenticated"] is True
    assert payload["credential_type"] == "token"
    assert "never-return-this-value" not in response.text
    assert payload["session_id"] in app.state.pending_auth_sessions
    assert "created_at" in app.state.pending_auth_sessions[payload["session_id"]]


async def test_submit_job_llm_key_is_runtime_only(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """MiniMax 配置传给本次 Runner，但 API Key 不得进入 job_input.json。"""
    app, fake, client, repo_dir = env
    body = _submit_body(repo_dir, "job-api-llm1")
    body["llm"] = {
        "api_key": "sk-test-key-123456",
        "model": "MiniMax-M2",
        "base_url": "https://api.minimaxi.com/v1",
        "max_tokens": 8192,
    }
    response = await client.post("/api/v1/jobs", json=body)
    assert response.status_code == 202
    await _await_task(app, "job-api-llm1")
    saved = json.loads(
        (
            Path(str(app.state.settings.artifacts_root)) / "job-api-llm1" / "job_input.json"
        ).read_text(encoding="utf-8")
    )
    assert saved["llm"] == {
        "model": "MiniMax-M2",
        "base_url": "https://api.minimaxi.com/v1",
        "protocol": "openai-chat",
        "max_tokens": 8192,
        "api_key_configured": True,
    }
    assert "api_key" not in saved["llm"]
    assert fake.calls[-1]["llm_config"]["api_key"] == "sk-test-key-123456"
    assert "job-api-llm1" not in app.state.job_llm_configs


async def test_submit_job_persists_selected_llm_protocol(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """任务级协议是非敏感运行配置，保存、重跑与 Runner 均须保持一致。"""
    app, fake, client, repo_dir = env
    body = _submit_body(repo_dir, "job-api-llm-protocol")
    body["llm"] = {
        "api_key": "sk-test-key-123456",
        "model": "deepseek-chat",
        "base_url": "https://llm.htffund.com/v1",
        "protocol": "anthropic-messages",
        "max_tokens": 4096,
    }
    assert (await client.post("/api/v1/jobs", json=body)).status_code == 202
    await _await_task(app, "job-api-llm-protocol")
    saved = json.loads(
        (
            Path(str(app.state.settings.artifacts_root))
            / "job-api-llm-protocol"
            / "job_input.json"
        ).read_text(encoding="utf-8")
    )
    assert saved["llm"]["protocol"] == "anthropic-messages"
    assert fake.calls[-1]["llm_config"]["protocol"] == "anthropic-messages"


async def test_edit_full_job_configuration_preserves_secrets_and_reruns(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """完整配置读取脱敏；编辑可保留文档/密码并从 req_parse 全量重跑。"""
    app, fake, client, repo_dir = env
    body = _submit_body(repo_dir, "job-api-edit1")
    body["auth"] = {
        "login_url": "https://example.test/login/",
        "username": "admin",
        "password": "secret-password",
    }
    body["llm"] = {
        "api_key": "sk-test-key-123456",
        "model": "MiniMax-M3",
        "base_url": "https://api.minimaxi.com/v1",
        "max_tokens": 8192,
    }
    assert (await client.post("/api/v1/jobs", json=body)).status_code == 202
    await _await_task(app, "job-api-edit1")

    config = (await client.get("/api/v1/jobs/job-api-edit1/configuration")).json()
    assert config["documents"] == ["requirements.md"]
    assert config["rest_insecure_tls"] is True
    assert config["auth"]["password_configured"] is True
    assert "password" not in config["auth"]
    assert config["llm"]["api_key_configured"] is True
    assert "api_key" not in config["llm"]

    update = {
        "docs": [],
        "keep_existing_docs": True,
        "repos": [{"name": "repo-updated", "path": str(repo_dir), "branch": "feature/x"}],
        "rest_url": "https://example.test:8443",
        "rest_insecure_tls": False,
        "resume": False,
        "auth": {"login_url": "https://example.test/login/", "username": "admin"},
        "llm": {
            "api_key": "sk-test-key-123456",
            "model": "MiniMax-M2.5",
            "base_url": "https://api.minimaxi.com/v1",
            "max_tokens": 4096,
        },
    }
    response = await client.put("/api/v1/jobs/job-api-edit1/configuration", json=update)
    assert response.status_code == 202
    assert response.json()["from_node"] == "req_parse"
    await _await_task(app, "job-api-edit1")
    assert fake.calls[-1]["force_node"] == "req_parse"
    assert fake.calls[-1]["rest_insecure_tls"] is False
    saved = json.loads(
        (
            Path(str(app.state.settings.artifacts_root)) / "job-api-edit1" / "job_input.json"
        ).read_text()
    )
    assert saved["auth"]["password"] == body["auth"]["password"]
    assert saved["llm"]["model"] == "MiniMax-M2.5"
    assert saved["llm"]["protocol"] == "openai-chat"
    assert "api_key" not in saved["llm"]
    assert saved["doc_paths"][-1].endswith("input/requirements.md")


async def test_edit_configuration_can_switch_to_service_level_llm(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """编辑任务时关闭任务级模型覆盖，不能继续偷偷沿用旧 LLM 配置。"""
    app, _fake, client, repo_dir = env
    body = _submit_body(repo_dir, "job-api-service-llm")
    body["llm"] = {
        "api_key": "sk-test-key-123456",
        "model": "MiniMax-M3",
        "base_url": "https://api.minimaxi.com/v1",
        "max_tokens": 8192,
    }
    assert (await client.post("/api/v1/jobs", json=body)).status_code == 202
    await _await_task(app, "job-api-service-llm")

    response = await client.put(
        "/api/v1/jobs/job-api-service-llm/configuration",
        json={
            "docs": [],
            "keep_existing_docs": True,
            "repos": [{"name": "repo-a", "path": str(repo_dir), "branch": "main"}],
            "rest_url": "http://127.0.0.1:9000",
            "resume": True,
            "run_after_save": False,
            "llm_enabled": False,
        },
    )
    assert response.status_code == 202
    saved = json.loads(
        (
            Path(str(app.state.settings.artifacts_root))
            / "job-api-service-llm"
            / "job_input.json"
        ).read_text()
    )
    assert "llm" not in saved
    configuration = (
        await client.get("/api/v1/jobs/job-api-service-llm/configuration")
    ).json()
    assert configuration["llm"] is None


async def test_edit_full_job_configuration_replaces_documents(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """重新上传文档时替换 input 目录，不混入旧需求文档。"""
    app, _fake, client, repo_dir = env
    assert (
        await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-edit2"))
    ).status_code == 202
    await _await_task(app, "job-api-edit2")
    update = {
        "docs": [{"doc_name": "new.md", "doc_content_b64": base64.b64encode(b"# new").decode()}],
        "keep_existing_docs": False,
        "repos": [{"name": "repo-a", "path": str(repo_dir), "branch": "main"}],
        "rest_url": "http://127.0.0.1:9001",
        "resume": True,
    }
    assert (
        await client.put("/api/v1/jobs/job-api-edit2/configuration", json=update)
    ).status_code == 202
    input_dir = Path(str(app.state.settings.artifacts_root)) / "job-api-edit2" / "input"
    assert (input_dir / "new.md").is_file()
    assert not (input_dir / "requirements.md").exists()


async def test_edit_configuration_can_save_without_rerun(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """仅保存配置不得创建后台任务、归档产物或消耗模型调用。"""
    app, fake, client, repo_dir = env
    body = _submit_body(repo_dir, "job-api-saveonly")
    body["fixtures"] = [
        {
            "name": "seed-user",
            "phase": "setup",
            "method": "POST",
            "path": "/fixtures/users",
            "expected": "status=201",
            "required": True,
        }
    ]
    assert (
        await client.post("/api/v1/jobs", json=body)
    ).status_code == 202
    await _await_task(app, "job-api-saveonly")
    calls_before = len(fake.calls)
    response = await client.put(
        "/api/v1/jobs/job-api-saveonly/configuration",
        json={
            "docs": [],
            "keep_existing_docs": True,
            "repos": [{"name": "repo-a", "path": str(repo_dir), "branch": "main"}],
            "rest_url": "http://127.0.0.1:9002",
            "resume": True,
            "run_after_save": False,
            "fixtures": [],
            "replace_fixtures": True,
        },
    )
    assert response.status_code == 202
    assert response.json() == {
        "job_id": "job-api-saveonly",
        "status": "configuration-saved",
        "from_node": None,
    }
    assert len(fake.calls) == calls_before
    saved = json.loads(
        (
            Path(str(app.state.settings.artifacts_root))
            / "job-api-saveonly"
            / "job_input.json"
        ).read_text()
    )
    assert saved["rest_url"] == "http://127.0.0.1:9002"
    assert saved["fixtures"] == []


async def test_job_status_and_artifacts(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """状态端点（9 节点 completed）+ 产物白名单读取 + 越权 404。"""
    app, _fake, client, repo_dir = env
    await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-0002"))
    await _await_task(app, "job-api-0002")
    status = (await client.get("/api/v1/jobs/job-api-0002")).json()
    assert status["running"] is False
    assert len(status["nodes"]) == 9
    assert all(n["status"] == "completed" for n in status["nodes"])
    assert "test_report.md" in status["artifacts"]
    assert "job_input.json" not in status["artifacts"]  # 内部元数据不外露
    report = await client.get("/api/v1/jobs/job-api-0002/report")
    assert report.status_code == 200
    assert report.headers["content-type"].startswith("text/markdown")
    assert "# 测试报告" in report.text
    leaked = await client.get("/api/v1/jobs/job-api-0002/artifacts/state.json")
    assert leaked.status_code == 404
    absent = await client.get("/api/v1/jobs/job-api-0002/artifacts/llm_calls.jsonl")
    assert absent.status_code == 404  # 白名单内但尚未生成（FakeRunner 不产留痕）


async def test_list_jobs(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """清单端点：按产物目录扫描 + completed/degraded 计数 + 创建时间倒排。"""
    app, _fake, client, repo_dir = env
    await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-0003"))
    await _await_task(app, "job-api-0003")
    jobs = (await client.get("/api/v1/jobs")).json()["jobs"]
    assert [j["job_id"] for j in jobs] == ["job-api-0003"]
    assert jobs[0]["completed"] == 9
    assert jobs[0]["running"] is False


async def test_list_jobs_created_desc(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """任务列表按创建时间倒排（最新提交在前）：改写旧 job 的 created_at 验证顺序。"""
    app, _fake, client, repo_dir = env
    root = Path(str(app.state.settings.artifacts_root))
    await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-old"))
    await _await_task(app, "job-api-old")
    await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-new"))
    await _await_task(app, "job-api-new")
    # 两份 state.json 的 created_at 可能同秒——手工改写确保可比：旧 job 回拨 1 小时
    old_state = json.loads((root / "job-api-old" / "state.json").read_text(encoding="utf-8"))
    old_state["created_at"] = "2026-01-01T00:00:00+00:00"
    (root / "job-api-old" / "state.json").write_text(
        json.dumps(old_state, ensure_ascii=False), encoding="utf-8"
    )
    jobs = (await client.get("/api/v1/jobs")).json()["jobs"]
    assert [j["job_id"] for j in jobs] == ["job-api-new", "job-api-old"]
    assert jobs[0]["created_at"] is not None


async def test_retest_full_and_single(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全量复测（force_node 队列化）与单条复测（同源核心，环境不可达如实告警）。"""
    app, fake, client, repo_dir = env

    # rest_url 用封闭端口；单条复测的环境探测直接桩为不可达——测点是「不可达 →
    # 跳过 + 告警」行为本身，不依赖真实网络拒绝（透明代理环境下 502 响应会被
    # probe_rest 判为存活，真实端口拒绝不可移植）
    async def _probe_unreachable(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr("orchestrator.nodes.retest_regression.probe_rest", _probe_unreachable)
    body = _submit_body(repo_dir, "job-api-0004")
    body["rest_url"] = "http://127.0.0.1:1"
    await client.post("/api/v1/jobs", json=body)
    await _await_task(app, "job-api-0004")
    full = await client.post("/api/v1/jobs/job-api-0004/retest", json={})
    assert full.status_code == 200
    assert full.json()["mode"] == "full"
    await _await_task(app, "job-api-0004")
    assert fake.calls[-1]["force_node"] == "retest_regression"
    # 单条：缺陷存在但 rest 服务不可达（封闭端口）→ 跳过 + 告警（X-EXEC-01 同源口径）
    job_dir = Path(str(app.state.settings.artifacts_root)) / "job-api-0004"
    defects = json.loads((job_dir / "defects.json").read_text(encoding="utf-8"))
    defects["defects"] = [
        {
            "id": "DEF-001",
            "case_id": "TC-001",
            "interface_id": "IF-REST-001",
            "failure_reason": "HTTP 500",
            "reproduce": {"script_path": "scripts/test_TC_001.py", "params": {}},
            "status": "retained",
            "history": [{"event": "discovered", "timestamp": "2026-09-15T00:00:00+00:00"}],
            "is_fallback": False,
        }
    ]
    (job_dir / "defects.json").write_text(json.dumps(defects), encoding="utf-8")
    single = await client.post("/api/v1/jobs/job-api-0004/retest", json={"defect_id": "DEF-001"})
    assert single.status_code == 200
    payload = single.json()
    assert payload["mode"] == "single"
    assert payload["retested"] == []
    assert any("复测环境不可达" in w for w in payload["warnings"])


async def test_review_log_and_reject_rerun(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """审核留痕（F-REVW-06）：approved 仅记录；rejected 回流生成节点。"""
    app, fake, client, repo_dir = env
    await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-0005"))
    await _await_task(app, "job-api-0005")
    bad = await client.post("/api/v1/jobs/job-api-0005/review-log", json={"decision": "maybe"})
    assert bad.status_code == 400
    approved = await client.post(
        "/api/v1/jobs/job-api-0005/review-log",
        json={"decision": "approved", "reviewer": "reviewer-a", "note": "口径一致"},
    )
    assert approved.status_code == 200
    assert approved.json()["rerun"] is None
    rejected = await client.post(
        "/api/v1/jobs/job-api-0005/review-log",
        json={"decision": "rejected", "note": "参数语义有误"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["rerun"] == "testcase_generate"
    await _await_task(app, "job-api-0005")
    assert fake.calls[-1]["force_node"] == "testcase_generate"
    log = Path(str(app.state.settings.artifacts_root)) / "job-api-0005" / "review_log.jsonl"
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [e["decision"] for e in entries] == ["approved", "rejected"]


async def test_rerun_from_node(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """C-14：POST /rerun 指定 from_node → 200 入队，force_node 透传给 Runner。"""
    app, fake, client, repo_dir = env
    await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-rr01"))
    await _await_task(app, "job-api-rr01")

    response = await client.post("/api/v1/jobs/job-api-rr01/rerun", json={"from_node": "code_scan"})
    assert response.status_code == 200
    assert response.json() == {
        "job_id": "job-api-rr01",
        "status": "rerun-queued",
        "from_node": "code_scan",
    }
    await _await_task(app, "job-api-rr01")
    assert fake.calls[-1]["force_node"] == "code_scan"


async def test_rerun_default_full(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """C-14：from_node 缺省 → 全量重跑（req_parse 级联失效）。"""
    app, fake, client, repo_dir = env
    await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-rr02"))
    await _await_task(app, "job-api-rr02")

    response = await client.post("/api/v1/jobs/job-api-rr02/rerun", json={})
    assert response.status_code == 200
    assert response.json()["from_node"] == "req_parse"
    await _await_task(app, "job-api-rr02")
    assert fake.calls[-1]["force_node"] == "req_parse"


async def test_rerun_rejections(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """C-14：非法节点 400 / 不存在 job 404 / 运行中 409。"""
    app, fake, client, repo_dir = env
    await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-rr03"))
    await _await_task(app, "job-api-rr03")

    bad_node = await client.post("/api/v1/jobs/job-api-rr03/rerun", json={"from_node": "bogus"})
    assert bad_node.status_code == 400
    assert "9 节点之一" in bad_node.json()["detail"]

    unknown = await client.post("/api/v1/jobs/job-nope/rerun", json={})
    assert unknown.status_code == 404

    # 运行中冲突：注入一个未完成的后台任务模拟在跑
    task = asyncio.create_task(asyncio.sleep(30))
    app.state.job_tasks["job-api-rr03"] = task
    try:
        conflict = await client.post("/api/v1/jobs/job-api-rr03/rerun", json={})
        assert conflict.status_code == 409
    finally:
        task.cancel()
    assert len(fake.calls) == 1  # 拒绝路径均未触发重跑


async def test_stop_running_job_preserves_completed_artifacts_and_releases_current_node(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """停止须取消协程、把 running 节点回退 pending，且不伪造完成或降级。"""
    app, _fake, client, repo_dir = env
    runner = BlockingRunner(app.state.engine)
    app.state.runner = runner

    response = await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-stop01"))
    assert response.status_code == 202
    await asyncio.wait_for(runner.started.wait(), timeout=1)
    assert (await client.get("/api/v1/jobs/job-api-stop01")).json()["running"] is True

    stopped = await client.post("/api/v1/jobs/job-api-stop01/stop", json={})
    assert stopped.status_code == 200
    assert stopped.json() == {
        "job_id": "job-api-stop01",
        "status": "stopped",
        "stopped_nodes": ["req_parse"],
    }
    assert runner.cancelled is True

    status = (await client.get("/api/v1/jobs/job-api-stop01")).json()
    req_parse = next(node for node in status["nodes"] if node["name"] == "req_parse")
    assert status["running"] is False
    assert req_parse["status"] == "pending"
    assert req_parse["error"]["code"] == "X-ORCH-04"
    assert (await client.post("/api/v1/jobs/job-api-stop01/stop", json={})).status_code == 409


def test_app_startup_recovers_stale_running_node(tmp_path: Path) -> None:
    """进程已退出时，内存任务不可能存活；遗留 running 必须回退，不能让页面永久卡住。"""
    schemas = Path(__file__).resolve().parents[3] / "schemas"
    settings = Settings(
        port=8000,
        java_parser_url="http://mock:8080",
        artifacts_root=str(tmp_path / "artifacts"),
        llm_base_url="http://mock:8090/v1",
        llm_model_name="glm-5.3",
        llm_api_key="",
        llm_temperature=0.2,
        llm_top_p=0.8,
        llm_seed=None,
        llm_timeout_s=5.0,
        schemas_dir=str(schemas),
    )
    first_app = create_app(settings)
    state = first_app.state.engine.init_job("job-api-stale01")
    first_app.state.engine.mark_running(state, "req_parse")

    restarted_app = create_app(settings)
    recovered = restarted_app.state.engine.open_job("job-api-stale01")
    node = recovered.node("req_parse")
    assert node.status is NodeStatus.PENDING
    assert node.error is not None
    assert node.error["code"] == "X-ORCH-04"
    assert "服务重启" in node.error["message"]


async def test_unknown_job_404(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """不存在的 job：统一 404（C-12 后 ID 须合法格式，故用合规前缀的不存在 ID）。"""
    _app, _fake, client, _repo_dir = env
    assert (await client.get("/api/v1/jobs/job-nope")).status_code == 404
    assert (await client.get("/api/v1/jobs/job-nope/report")).status_code == 404
    assert (await client.post("/api/v1/jobs/job-nope/retest", json={})).status_code == 404


async def test_resume_false_wipes_job_dir(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """resume=false → 清空既有产物重跑（F-ORCH-02 反向口径）。"""
    app, _fake, client, repo_dir = env
    await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-0006"))
    await _await_task(app, "job-api-0006")
    job_dir = Path(str(app.state.settings.artifacts_root)) / "job-api-0006"
    stale = job_dir / "stale-marker.txt"
    stale.write_text("旧产物", encoding="utf-8")
    body = _submit_body(repo_dir, "job-api-0006")
    body["resume"] = False
    await client.post("/api/v1/jobs", json=body)
    await _await_task(app, "job-api-0006")
    assert not stale.exists()
    # P-11 回归：清空不得误删本次提交的输入文档（req_parse 依赖）
    doc = next((job_dir / "input").glob("*.md"), None)
    assert doc is not None and doc.read_text(encoding="utf-8") == _DOC


async def test_submit_existing_job_with_resume_true_is_idempotency_conflict(
    env: tuple[FastAPI, FakeRunner, httpx.AsyncClient, Path],
) -> None:
    """已有 job 不接受第二次普通提交，避免误覆盖；重跑走专用端点。"""
    app, _, client, repo_dir = env
    await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-idem01"))
    await _await_task(app, "job-api-idem01")
    response = await client.post("/api/v1/jobs", json=_submit_body(repo_dir, "job-api-idem01"))
    assert response.status_code == 409
    assert "重跑" in response.json()["detail"]
