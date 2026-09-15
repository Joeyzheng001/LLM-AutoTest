"""auto_execute 节点测试：失败二态 + 环境探测 + 降级演示 + 汇总口径。

MockTransport 模拟被测 REST 服务（/health 探活 + 业务端点）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from orchestrator.artifacts import ArtifactStore
from orchestrator.engine import Engine
from orchestrator.errors import X_ORCH_02, PipelineError
from orchestrator.nodes import run_auto_execute
from orchestrator.pipeline import NodeStatus

_SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"
_JOB = "job-3f2a1b8c-0000-4000-8000-000000000001"

_INTERFACES: list[dict[str, Any]] = [
    {
        "id": "IF-REST-001",
        "protocol": "rest",
        "signature": {"method": "GET", "path": "/api/v1/users", "summary": "查用户"},
        "params": [{"name": "id", "type": "long", "required": True, "in": "query"}],
        "code_location": {
            "repo": "user-service",
            "file": "UserController.java",
            "start_line": 30,
            "end_line": 45,
        },
        "is_fallback": False,
    },
    {
        "id": "IF-GRPC-001",
        "protocol": "grpc",
        "signature": {"service_method": "GetOrder", "summary": "查询订单"},
        "params": [
            {"name": "order_id", "type": "unknown", "required": True, "default": "ORDER-000"}
        ],
        "code_location": {
            "repo": "order-center",
            "file": "OrderGrpc.java",
            "start_line": 10,
            "end_line": 20,
        },
        "is_fallback": False,
    },
]

_TESTCASES: list[dict[str, Any]] = [
    {
        "id": "TC-001",
        "interface_id": "IF-REST-001",
        "category": "normal",
        "title": "正常查询",
        "expected": "返回 UserDTO",
        "params": {"id": 1},
        "script_path": "scripts/test_TC_001.py",
        "loadable": True,
    },
    {
        "id": "TC-002",
        "interface_id": "IF-REST-001",
        "category": "exception",
        "title": "非法 id",
        "expected": "400",
        "params": {"id": -1},
        "script_path": "scripts/test_TC_002.py",
        "loadable": True,
    },
    {
        "id": "TC-003",
        "interface_id": "IF-GRPC-001",
        "category": "normal",
        "title": "正常查询订单",
        "expected": "订单详情",
        "params": {"order_id": "ORDER-001"},
        "script_path": "scripts/test_TC_003.py",
        "loadable": True,
    },
]


def _ok_service(request: httpx.Request) -> httpx.Response:
    """模拟被测 REST 服务：/health 探活 + 业务端点正常。"""
    if request.url.path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if request.url.path == "/api/v1/users":
        return httpx.Response(200, json={"id": 1, "name": "u"})
    return httpx.Response(404, json={"error": "not found"})


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Engine, ArtifactStore, ArtifactStore]:
    """引擎 + 双身份产物卷（甲前置 orchestrator 写；testcases/execution 丙写）+ 前五节点已完成。"""
    orch_store = ArtifactStore(tmp_path, _SCHEMAS, "orchestrator")
    te_store = ArtifactStore(tmp_path, _SCHEMAS, "test-executor")
    engine = Engine(orch_store)
    state = engine.init_job(_JOB)
    # （节点名，产物文件，写入卷，载荷）
    orch_artifacts: list[tuple[str, str, dict[str, Any]]] = [
        (
            "req_parse",
            "requirement_items.json",
            {
                "schema_version": "1.0",
                "job_id": _JOB,
                "items": [{"id": "REQ-001", "title": "用户查询", "description": "按 ID 查询用户"}],
            },
        ),
        (
            "code_scan",
            "code_inventory.json",
            {
                "schema_version": "1.0",
                "job_id": _JOB,
                "repositories": [
                    {
                        "name": "user-service",
                        "path": "/data/repos/user-service",
                        "branch": "release/v2.3",
                        "is_fallback": False,
                        "status": "ok",
                        "file_count": 1,
                    },
                    {
                        "name": "order-center",
                        "path": "/data/repos/order-center",
                        "branch": "master",
                        "is_fallback": False,
                        "status": "ok",
                        "file_count": 1,
                    },
                ],
            },
        ),
        (
            "interface_reconstruct",
            "interface_candidates.json",
            {
                "schema_version": "1.0",
                "job_id": _JOB,
                "candidates": [
                    {
                        "candidate_id": "CAND-001",
                        "protocol": "rest",
                        "signature": "GET /api/v1/users",
                        "code_location": _INTERFACES[0]["code_location"],
                        "is_fallback": False,
                    },
                    {
                        "candidate_id": "CAND-002",
                        "protocol": "grpc",
                        "signature": "OrderService/GetOrder",
                        "code_location": _INTERFACES[1]["code_location"],
                        "is_fallback": False,
                    },
                ],
            },
        ),
        (
            "interface_reconstruct",
            "interfaces.json",
            {"schema_version": "1.0", "job_id": _JOB, "interfaces": _INTERFACES},
        ),
        (
            "code_review",
            "review_mapping.json",
            {
                "schema_version": "1.0",
                "job_id": _JOB,
                "mappings": [
                    {
                        "requirement_id": "REQ-001",
                        "interface_id": "IF-REST-001",
                        "status": "implemented",
                        "confidence": 0.9,
                    }
                ],
                "risks": [],
                "missing_features": [],
            },
        ),
    ]
    for node_name, _filename, _payload in orch_artifacts:
        engine.mark_running(state, node_name)
    engine.mark_running(state, "testcase_generate")
    for _node, filename, payload in orch_artifacts:
        orch_store.write(_JOB, filename, payload)
    te_store.write(
        _JOB, "testcases.json", {"schema_version": "1.0", "job_id": _JOB, "cases": _TESTCASES}
    )
    engine.mark_completed(state, "req_parse")
    engine.mark_completed(state, "code_scan")
    engine.mark_completed(state, "interface_reconstruct")
    engine.mark_completed(state, "code_review")
    engine.mark_completed(state, "testcase_generate")
    return engine, te_store, orch_store


async def test_auto_execute_success_with_degraded_grpc(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """REST 真实执行（探活+调用）通过；gRPC 降级 Mock 演示并如实记录。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)
    transport = httpx.MockTransport(_ok_service)

    path = await run_auto_execute(engine, state, te_store, "http://mock:9000", transport)

    assert path.is_file()
    payload = te_store.read_json(_JOB, "execution.json")
    results = payload["results"]
    # REST 两条通过（真实 HTTP 200）
    rest = [r for r in results if r["interface_id"] == "IF-REST-001"]
    assert all(r["passed"] is True for r in rest)
    assert all(r["duration_ms"] >= 0 for r in rest)
    # gRPC 一条：Mock 演示 passed + 降级清单如实记录（X-EXEC-03）
    grpc = [r for r in results if r["interface_id"] == "IF-GRPC-001"]
    assert grpc[0]["passed"] is True
    assert payload["degraded_protocols"] == [
        {
            "protocol": "grpc",
            "mode": "脚本生成成功+Mock执行演示",
            "reason": "Runner 未就绪（X-EXEC-03），降级为 Mock 执行演示",
        }
    ]
    # 汇总口径（程序化）
    assert payload["summary"] == {
        "total": 3,
        "passed": 3,
        "failed_assertion": 0,
        "failed_environment": 0,
    }
    # D4：允许失败用例——失败二态明确即完成
    assert state.node("auto_execute").status is NodeStatus.COMPLETED


async def test_auto_execute_assertion_failure(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """REST 返回 500：断言失败（failure_kind=assertion，进缺陷清单口径）。"""

    def server_500(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(500, json={"error": "boom"})

    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)
    transport = httpx.MockTransport(server_500)

    await run_auto_execute(engine, state, te_store, "http://mock:9000", transport)

    payload = te_store.read_json(_JOB, "execution.json")
    rest = [r for r in payload["results"] if r["interface_id"] == "IF-REST-001"]
    assert all(r["passed"] is False and r["failure_kind"] == "assertion" for r in rest)
    assert "HTTP 500" in rest[0]["failure_reason"]
    assert payload["summary"]["failed_assertion"] == 2


async def test_auto_execute_environment_failure(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """探活失败：REST 用例全记环境失败（X-EXEC-01，不判缺陷）。"""

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)
    transport = httpx.MockTransport(unreachable)

    await run_auto_execute(engine, state, te_store, "http://mock:9000", transport)

    payload = te_store.read_json(_JOB, "execution.json")
    rest = [r for r in payload["results"] if r["interface_id"] == "IF-REST-001"]
    assert all(r["passed"] is False and r["failure_kind"] == "environment" for r in rest)
    assert payload["summary"]["failed_environment"] == 2
    # gRPC 不受 REST 环境影响（走 Mock 演示降级）
    grpc = [r for r in payload["results"] if r["interface_id"] == "IF-GRPC-001"]
    assert grpc[0]["passed"] is True


async def test_auto_execute_timeout(env: tuple[Engine, ArtifactStore, ArtifactStore]) -> None:
    """执行超时：timeout 标记 + 环境类失败（X-EXEC-02）。"""

    def slow(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        raise httpx.ReadTimeout("timed out")

    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)
    transport = httpx.MockTransport(slow)

    await run_auto_execute(engine, state, te_store, "http://mock:9000", transport)

    payload = te_store.read_json(_JOB, "execution.json")
    rest = [r for r in payload["results"] if r["interface_id"] == "IF-REST-001"]
    assert all(r["timeout"] is True and r["failure_kind"] == "environment" for r in rest)


async def test_auto_execute_unloadable_skipped(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """loadable=false 用例：跳过执行、skipped 计数、不静默（R-GEN-03 特例）。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)
    # 把 TC-002 改为不可加载
    testcases = te_store.read_json(_JOB, "testcases.json")
    testcases["cases"][1]["loadable"] = False
    te_store.write(_JOB, "testcases.json", testcases)

    transport = httpx.MockTransport(_ok_service)
    await run_auto_execute(engine, state, te_store, "http://mock:9000", transport)

    payload = te_store.read_json(_JOB, "execution.json")
    skipped = [r for r in payload["results"] if r["case_id"] == "TC-002"]
    assert skipped[0]["passed"] is False
    assert skipped[0]["failure_kind"] is None
    assert "loadable=false" in skipped[0]["failure_reason"]
    assert payload["summary"]["skipped"] == 1
    assert payload["summary"]["total"] == 3


async def test_auto_execute_skip_order_guard(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """testcase_generate 未完成：X-ORCH-02。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)
    state.node("testcase_generate").status = NodeStatus.PENDING

    with pytest.raises(PipelineError) as err:
        await run_auto_execute(
            engine, state, te_store, "http://mock:9000", httpx.MockTransport(_ok_service)
        )

    assert err.value.code == X_ORCH_02
