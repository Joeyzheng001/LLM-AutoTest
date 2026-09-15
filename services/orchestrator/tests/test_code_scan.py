"""code_scan 节点测试：契约对接全链路（MockTransport 模拟 java-parser）。

覆盖：轮询成功落盘、仓库 error（X-CODE-01）、job failed 透传契约错误、
网络失败/轮询超时（X-ORCH-03）、响应缺产物（X-ORCH-01）、跳序防护（X-ORCH-02）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from orchestrator.artifacts import ArtifactStore
from orchestrator.engine import Engine
from orchestrator.errors import X_ORCH_02, X_ORCH_03, PipelineError
from orchestrator.javaparser_client import JavaParserClient, RepositoryInput
from orchestrator.nodes import run_code_scan
from orchestrator.pipeline import NodeStatus

_SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"
_JOB = "job-3f2a1b8c-0000-4000-8000-000000000001"

_REPOS = [
    RepositoryInput(name="user-service", path="/data/repos/user-service", branch="release/v2.3"),
    RepositoryInput(
        name="fallback-demo",
        path="/data/repos/fallback-demo",
        branch="master",
        is_fallback=True,
    ),
]

_SCAN_JOB_ID = "scan-0001"

_GOOD_INVENTORY: dict[str, Any] = {
    "schema_version": "1.0",
    "job_id": _JOB,
    "repositories": [
        {
            "name": "user-service",
            "path": "/data/repos/user-service",
            "branch": "release/v2.3",
            "is_fallback": False,
            "status": "ok",
            "file_count": 2,
            "files": [
                {
                    "repo": "user-service",
                    "module": "user-service",
                    "path": "src/main/java/User.java",
                },
                {
                    "repo": "user-service",
                    "module": "user-service",
                    "path": "src/main/java/Order.java",
                },
            ],
        },
        {
            "name": "fallback-demo",
            "path": "/data/repos/fallback-demo",
            "branch": "master",
            "is_fallback": True,
            "status": "empty",
            "file_count": 0,
        },
    ],
}


def _client(handler: Any, **kwargs: Any) -> JavaParserClient:
    """构造注入 MockTransport 的客户端（测试专用）。"""
    return JavaParserClient(
        "http://java-parser:8000",
        poll_interval_s=0.01,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Engine, ArtifactStore]:
    """引擎 + 产物卷 + req_parse 已完成的前置状态。"""
    store = ArtifactStore(tmp_path, _SCHEMAS, "orchestrator")
    engine = Engine(store)
    state = engine.init_job(_JOB)
    # 前置节点 req_parse 完成（写合法产物 + 状态推进）
    engine.mark_running(state, "req_parse")
    store.write(
        _JOB,
        "requirement_items.json",
        {
            "schema_version": "1.0",
            "job_id": _JOB,
            "source_file": "/data/docs/需求说明.md",
            "items": [{"id": "REQ-001", "title": "用户查询", "description": "按 ID 查询用户"}],
        },
    )
    engine.mark_completed(state, "req_parse")
    return engine, store


def _scan_handler(inventory: dict[str, Any] | None = _GOOD_INVENTORY) -> Any:
    """模拟 java-parser：POST 受理 → 首次 GET queued → 次 GET completed（或 failed）。"""
    polls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/scan-jobs":
            return httpx.Response(202, json={"job_id": _SCAN_JOB_ID, "status": "queued"})
        if request.method == "GET" and request.url.path == f"/api/v1/scan-jobs/{_SCAN_JOB_ID}":
            polls["count"] += 1
            if polls["count"] == 1:
                return httpx.Response(200, json={"job_id": _SCAN_JOB_ID, "status": "queued"})
            if inventory is None:
                return httpx.Response(
                    200,
                    json={
                        "job_id": _SCAN_JOB_ID,
                        "status": "failed",
                        "code": "X-CODE-01",
                        "message": "REPO_PATH_NOT_FOUND",
                        "details": {"paths": ["/data/repos/fallback-demo"]},
                    },
                )
            return httpx.Response(
                200,
                json={"job_id": _SCAN_JOB_ID, "status": "completed", "code_inventory": inventory},
            )
        raise AssertionError(f"非预期请求：{request.method} {request.url.path}")

    return handler


async def test_code_scan_success(env: tuple[Engine, ArtifactStore]) -> None:
    """全流程成功：轮询两次至 completed、产物落盘过校验、节点置完成。"""
    engine, store = env
    state = engine.open_job(_JOB)
    client = _client(_scan_handler())

    path = await run_code_scan(engine, state, store, client, _REPOS)

    assert path.is_file()
    payload = store.read_json(_JOB, "code_inventory.json")
    assert payload["job_id"] == _JOB
    assert [repo["name"] for repo in payload["repositories"]] == ["user-service", "fallback-demo"]
    # empty（X-CODE-02）与兜底仓库如实分列、流程继续
    assert payload["repositories"][1]["status"] == "empty"
    assert payload["repositories"][1]["is_fallback"] is True
    assert state.node("code_scan").status is NodeStatus.COMPLETED


async def test_code_scan_repo_error_fails_node(env: tuple[Engine, ArtifactStore]) -> None:
    """仓库 status=error（X-CODE-01）：节点失败、不落产物。"""
    engine, store = env
    state = engine.open_job(_JOB)
    bad = {
        "schema_version": "1.0",
        "job_id": _JOB,
        "repositories": [
            {**_GOOD_INVENTORY["repositories"][0]},
            {
                "name": "fallback-demo",
                "path": "/data/repos/fallback-demo",
                "branch": "master",
                "is_fallback": True,
                "status": "error",
                "file_count": 0,
                "error": {"code": "X-CODE-01", "message": "路径不存在"},
            },
        ],
    }
    client = _client(_scan_handler(bad))

    with pytest.raises(PipelineError) as err:
        await run_code_scan(engine, state, store, client, _REPOS)

    assert err.value.code == "X-CODE-01"
    # mark_failed 语义：回 pending + error 留痕（重试耗尽后由编排决定降级）
    node = state.node("code_scan")
    assert node.status is NodeStatus.PENDING
    assert node.error is not None and node.error["code"] == "X-CODE-01"
    assert not (store.job_dir(_JOB) / "code_inventory.json").exists()


async def test_code_scan_job_failed_propagates_contract_error(
    env: tuple[Engine, ArtifactStore],
) -> None:
    """job failed 终态：契约错误码原样透传（X-CODE-01）。"""
    engine, store = env
    state = engine.open_job(_JOB)
    client = _client(_scan_handler(inventory=None))

    with pytest.raises(PipelineError) as err:
        await run_code_scan(engine, state, store, client, _REPOS)

    assert err.value.code == "X-CODE-01"
    assert state.node("code_scan").status is NodeStatus.PENDING
    assert state.node("code_scan").error is not None


async def test_code_scan_network_error(env: tuple[Engine, ArtifactStore]) -> None:
    """网络不可达：X-ORCH-03、节点失败。"""

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    engine, store = env
    state = engine.open_job(_JOB)
    client = _client(broken)

    with pytest.raises(PipelineError) as err:
        await run_code_scan(engine, state, store, client, _REPOS)

    assert err.value.code == X_ORCH_03
    assert state.node("code_scan").status is NodeStatus.PENDING


async def test_code_scan_poll_timeout(env: tuple[Engine, ArtifactStore]) -> None:
    """轮询耗尽（一直 queued）：X-ORCH-03。"""

    def always_queued(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": _SCAN_JOB_ID, "status": "queued"})
        return httpx.Response(200, json={"job_id": _SCAN_JOB_ID, "status": "queued"})

    engine, store = env
    state = engine.open_job(_JOB)
    client = _client(always_queued, max_wait_s=0.03)

    with pytest.raises(PipelineError) as err:
        await run_code_scan(engine, state, store, client, _REPOS)

    assert err.value.code == X_ORCH_03


async def test_code_scan_missing_inventory(env: tuple[Engine, ArtifactStore]) -> None:
    """completed 但缺 code_inventory：X-ORCH-01 宁停不猜。"""

    def no_inventory(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": _SCAN_JOB_ID, "status": "queued"})
        return httpx.Response(200, json={"job_id": _SCAN_JOB_ID, "status": "completed"})

    engine, store = env
    state = engine.open_job(_JOB)
    client = _client(no_inventory)

    with pytest.raises(PipelineError) as err:
        await run_code_scan(engine, state, store, client, _REPOS)

    assert err.value.code == "X-ORCH-01"


async def test_code_scan_skip_order_guard(env: tuple[Engine, ArtifactStore]) -> None:
    """req_parse 未完成时直接跑 code_scan：X-ORCH-02 跳序拒绝。"""
    engine, store = env
    state = engine.open_job(_JOB)
    # 把 req_parse 状态回退为 pending（模拟对账前直接调节点）
    state.node("req_parse").status = NodeStatus.PENDING

    client = _client(_scan_handler())
    with pytest.raises(PipelineError) as err:
        await run_code_scan(engine, state, store, client, _REPOS)

    assert err.value.code == X_ORCH_02
    # 未发起远端调用即被拒（code_scan 仍 pending）
    assert state.node("code_scan").status is NodeStatus.PENDING
