"""auto_execute 节点测试：失败二态 + 环境探测 + 降级演示 + 汇总口径。

MockTransport 模拟被测 REST 服务（/health 探活 + 业务端点）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from helpers import requirement_review_payload
from orchestrator.artifacts import ArtifactStore
from orchestrator.engine import Engine
from orchestrator.errors import X_ORCH_02, PipelineError
from orchestrator.nodes import run_auto_execute
from orchestrator.nodes.auto_execute import (
    InvokeRequest,
    RestRunner,
    _expects_404,
    _judge_assertion,
    _summarize,
    login_for_headers,
    mutates_auth_session,
    resolved_auth_mode,
)
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
        ("req_parse", "requirement_review.json", requirement_review_payload(_JOB)),
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


def test_summary_contains_api_and_e2e_breakdown_for_mixed_execution() -> None:
    summary = _summarize(
        [
            {"case_id": "TC-001", "case_type": "api", "passed": True},
            {
                "case_id": "E2E-001",
                "case_type": "e2e",
                "passed": False,
                "failure_kind": "environment",
            },
        ]
    )
    assert summary["total"] == 2
    assert summary["by_type"]["api"]["passed"] == 1
    assert summary["by_type"]["e2e"]["failed_environment"] == 1


async def test_auto_execute_auth_failure_is_environment_not_defect(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """P0-05：成功预期收到 401/403 时按鉴权前置失败隔离，不进入 defects。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)

    def unauthorized(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(401, json={"detail": "认证凭据无效"})

    testcases = te_store.read_json(_JOB, "testcases.json")
    testcases["cases"] = [
        {**testcases["cases"][0], "expected": "status=200"},
    ]
    te_store.write(_JOB, "testcases.json", testcases)
    await run_auto_execute(
        engine, state, te_store, "http://mock:9000", httpx.MockTransport(unauthorized)
    )

    result = te_store.read_json(_JOB, "execution.json")["results"][0]
    assert result["failure_kind"] == "environment"
    assert "X-EXEC-05" in result["failure_reason"]
    assert "不计入产品缺陷" in result["failure_reason"]


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


async def test_auto_execute_skips_missing_required_before_http_call(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """P0-05：成功场景缺少必填参数时不访问业务接口，不伪造 400/500 缺陷。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)
    testcases = te_store.read_json(_JOB, "testcases.json")
    testcases["cases"][0]["params"] = {}
    testcases["cases"][0]["expected"] = "status=200"
    te_store.write(_JOB, "testcases.json", testcases)
    business_calls = 0

    def service(request: httpx.Request) -> httpx.Response:
        nonlocal business_calls
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        business_calls += 1
        return httpx.Response(400, json={"detail": "missing"})

    await run_auto_execute(
        engine, state, te_store, "http://mock:9000", httpx.MockTransport(service)
    )

    payload = te_store.read_json(_JOB, "execution.json")
    result = next(item for item in payload["results"] if item["case_id"] == "TC-001")
    assert result["passed"] is False
    assert result["failure_kind"] is None
    assert result["skipped"] is True
    assert result["skipped_reason"] == "前置缺失"
    assert "fund_code" not in result["failure_reason"]
    assert "id" in result["failure_reason"]
    assert business_calls == 1  # 仅探活请求


async def test_auto_execute_allows_missing_required_for_negative_case(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """P0-05：明确期待 4xx 的异常用例允许缺参并验证服务端校验。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)
    testcases = te_store.read_json(_JOB, "testcases.json")
    testcases["cases"] = [
        {
            **testcases["cases"][0],
            "category": "exception",
            "expected": "status=400",
            "params": {},
        }
    ]
    te_store.write(_JOB, "testcases.json", testcases)

    def service(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(400, json={"detail": "fund_code required"})

    await run_auto_execute(
        engine, state, te_store, "http://mock:9000", httpx.MockTransport(service)
    )

    result = te_store.read_json(_JOB, "execution.json")["results"][0]
    assert result["passed"] is True
    assert result.get("skipped") is not True


async def test_auto_execute_runs_fixtures_before_cases_and_teardown_after(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """P0-05：夹具按 setup→用例→teardown 顺序执行并记录，不把响应体落盘。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)
    testcases = te_store.read_json(_JOB, "testcases.json")
    testcases["cases"] = [testcases["cases"][0]]
    te_store.write(_JOB, "testcases.json", testcases)
    paths: list[str] = []

    def service(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/fixtures/users":
            return httpx.Response(201, json={"token": "secret-token", "created": True})
        if request.url.path == "/api/v1/users":
            return httpx.Response(200, json={"id": 1, "name": "u"})
        if request.url.path == "/fixtures/cleanup":
            return httpx.Response(204)
        return httpx.Response(404)

    fixtures = [
        {
            "name": "create-user",
            "phase": "setup",
            "method": "POST",
            "path": "/fixtures/users",
            "body": {"id": 1},
            "expected": "status=201",
            "required": True,
        },
        {
            "name": "cleanup-user",
            "phase": "teardown",
            "method": "DELETE",
            "path": "/fixtures/cleanup",
            "expected": "status=204",
            "required": False,
        },
    ]
    await run_auto_execute(
        engine,
        state,
        te_store,
        "http://mock:9000",
        httpx.MockTransport(service),
        fixtures=fixtures,
    )

    payload = te_store.read_json(_JOB, "execution.json")
    assert paths == ["/health", "/fixtures/users", "/api/v1/users", "/fixtures/cleanup"]
    assert [item["name"] for item in payload["fixtures"]] == [
        "create-user",
        "cleanup-user",
    ]
    assert payload["fixtures"][0]["passed"] is True
    assert "token" not in json.dumps(payload["fixtures"], ensure_ascii=False)
    assert payload["results"][0]["passed"] is True


async def test_auto_execute_required_fixture_failure_skips_cases(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """P0-05：必需夹具失败时不执行业务用例，统一记前置缺失。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)
    business_calls = 0

    def service(request: httpx.Request) -> httpx.Response:
        nonlocal business_calls
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/fixtures/users":
            return httpx.Response(500, json={"error": "seed failed"})
        business_calls += 1
        return httpx.Response(200, json={"id": 1})

    await run_auto_execute(
        engine,
        state,
        te_store,
        "http://mock:9000",
        httpx.MockTransport(service),
        fixtures=[
            {
                "name": "create-user",
                "phase": "setup",
                "method": "POST",
                "path": "/fixtures/users",
                "expected": "status=201",
                "required": True,
            }
        ],
    )

    payload = te_store.read_json(_JOB, "execution.json")
    assert payload["fixtures"][0]["passed"] is False
    assert all(item["skipped_reason"] == "前置缺失" for item in payload["results"])
    assert all("create-user" in item["failure_reason"] for item in payload["results"])
    assert business_calls == 0


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


async def test_auto_execute_path_param_substitution(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """回归锁定：path 参数须替换路径模板（联调发现——字面 {id} 进 URL 真实服务必 404）。"""
    engine, te_store, orch_store = env
    state = engine.open_job(_JOB)
    seen_paths: list[str] = []

    def path_service(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/v1/users/42":
            return httpx.Response(200, json={"id": 42, "name": "u"})
        return httpx.Response(404, json={"error": "not found"})

    # 前置：testcases 含 path 参数用例，interface 声明 in: "path"
    te_store.write(
        _JOB,
        "testcases.json",
        {
            "schema_version": "1.0",
            "job_id": _JOB,
            "cases": [
                {
                    "id": "TC-001",
                    "interface_id": "IF-REST-001",
                    "requirement_id": "REQ-001",
                    "category": "normal",
                    "title": "按 ID 查询",
                    "expected": "返回用户",
                    "params": {"id": 42},
                    "script_path": "scripts/test_TC_001.py",
                    "loadable": True,
                }
            ],
            "scripts": [
                {
                    "case_id": "TC-001",
                    "script_path": "scripts/test_TC_001.py",
                    "loadable": True,
                }
            ],
        },
    )
    orch_store.write(
        _JOB,
        "interfaces.json",
        {
            "schema_version": "1.0",
            "job_id": _JOB,
            "interfaces": [
                {
                    "id": "IF-REST-001",
                    "candidate_id": "CAND-001",
                    "protocol": "rest",
                    "signature": {
                        "method": "GET",
                        "path": "/api/v1/users/{id}",
                        "summary": "按 ID 查询用户",
                    },
                    "params": [
                        {"name": "id", "type": "long", "required": True, "in": "path"}
                    ],
                    "code_location": {
                        "repo": "user-service",
                        "file": "UserController.java",
                        "start_line": 28,
                        "end_line": 31,
                    },
                    "is_fallback": False,
                }
            ],
        },
    )

    await run_auto_execute(
        engine, state, te_store, "http://mock:9000", httpx.MockTransport(path_service)
    )

    # 请求命中替换后的真实路径（非字面 {id}）
    assert "/api/v1/users/42" in seen_paths
    assert not any("{id}" in p for p in seen_paths)
    payload = te_store.read_json(_JOB, "execution.json")
    assert payload["results"][0]["passed"] is True


async def test_auto_execute_semantic_assertion(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """最小语义断言（docs/13 C-05）：expected「字段=值」等值对失配 → assertion 失败。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)

    def user_service(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"id": 1, "name": "alice", "age": 29})

    _write_case(te_store, expected="id=1;age=28")  # 期望 28，实际 29 → 语义失配
    await run_auto_execute(
        engine, state, te_store, "http://mock:9000", httpx.MockTransport(user_service)
    )
    payload = te_store.read_json(_JOB, "execution.json")
    failed = [r for r in payload["results"] if r["passed"] is False]
    assert failed and failed[0]["failure_kind"] == "assertion"
    assert "age=29" in failed[0]["failure_reason"]
    assert "期望 28" in failed[0]["failure_reason"]


async def test_auto_execute_semantic_assertion_passes_on_match(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """语义等值对全部命中 + 描述型 expected（无等值对）不受影响 → 通过。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)

    def user_service(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"id": 1, "name": "alice", "age": 28})

    _write_case(te_store, expected="id=1;age=28；描述文字不影响判定")
    await run_auto_execute(
        engine, state, te_store, "http://mock:9000", httpx.MockTransport(user_service)
    )
    payload = te_store.read_json(_JOB, "execution.json")
    assert all(r["passed"] is True for r in payload["results"])


def _write_case(te_store: ArtifactStore, expected: str) -> None:
    """写单用例前置（接口与用例，供语义断言测试复用）。"""
    te_store.write(
        _JOB,
        "testcases.json",
        {
            "schema_version": "1.0",
            "job_id": _JOB,
            "cases": [
                {
                    "id": "TC-001",
                    "interface_id": "IF-REST-001",
                    "requirement_id": "REQ-001",
                    "category": "normal",
                    "title": "按 ID 查询",
                    "expected": expected,
                    "params": {"id": 1},
                    "script_path": "scripts/test_TC_001.py",
                    "loadable": True,
                }
            ],
        },
    )


async def test_auto_execute_404_is_environment_not_defect(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """回归锁定：HTTP 404（路由不存在）归 environment，不进缺陷清单（R-EXEC-03/R-DEF-01）。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)

    def routed_service(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404, text="Not Found")

    _write_case(te_store, expected="2xx 响应结构符合接口定义")
    await run_auto_execute(
        engine, state, te_store, "http://mock:9000", httpx.MockTransport(routed_service)
    )
    payload = te_store.read_json(_JOB, "execution.json")
    result = payload["results"][0]
    assert result["passed"] is False
    assert result["failure_kind"] == "environment"
    assert "404" in str(result["failure_reason"])


async def test_auto_execute_expected_status_token(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """回归锁定：expected 含 status=400 token 时，非 2xx 预期状态码判通过（C-05 扩展）。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)

    def biz_service(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(400, json={"error": "用户不存在"})

    _write_case(te_store, expected="status=400；不存在资源返回业务错误码")
    await run_auto_execute(
        engine, state, te_store, "http://mock:9000", httpx.MockTransport(biz_service)
    )
    payload = te_store.read_json(_JOB, "execution.json")
    result = payload["results"][0]
    assert result["passed"] is True
    assert result.get("failure_kind") is None


def test_judge_assertion_accepts_status_class_and_legacy_http_class() -> None:
    """边界/异常用例的 4xx 区间既支持新 token，也兼容既有自然语言产物。"""
    for expected in ("status_class=4xx；密码应被拒绝", "HTTP 4xx 或返回校验失败提示"):
        is_pass, semantic_failures = _judge_assertion(expected, 401, {})
        assert is_pass is True
        assert semantic_failures == []


def test_judge_assertion_supports_multiple_legacy_statuses_and_ignores_prose_equals() -> None:
    """旧用例的 HTTP 200/204 和自然语言示例不能制造伪断言失败。"""
    is_pass, semantic_failures = _judge_assertion(
        "HTTP 200/204；响应体为对象（含 fund_code='000330'）", 204, {}
    )
    assert is_pass is True
    assert semantic_failures == []


def test_expects_404_recognizes_exact_and_class_status() -> None:
    assert _expects_404("status=404；资源不存在")
    assert _expects_404("HTTP 4xx；资源不存在")
    assert not _expects_404("status=200")


# ---- C-15 鉴权：登录提取凭据头（数值/凭据不经 LLM）----

_AUTH: dict[str, Any] = {
    "login_url": "http://mock:9000/api/auth/login/",
    "username": "admin",
    "password": "admin123456",
}


async def test_login_for_headers_token() -> None:
    """token 字段 → Authorization: Token（实测被测服务口径）。"""
    seen: list[dict[str, Any]] = []

    def login_service(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "path": request.url.path,
                "body": json.loads(request.content.decode("utf-8")),
            }
        )
        return httpx.Response(200, json={"token": "tok-abc123", "username": "admin"})

    headers = await login_for_headers(_AUTH, httpx.MockTransport(login_service))
    assert headers == {"Authorization": "Token tok-abc123"}
    assert seen == [
        {
            "path": "/api/auth/login/",
            "body": {"username": "admin", "password": "admin123456"},
        }
    ]


async def test_login_for_headers_supports_custom_fields_nested_token_and_bearer() -> None:
    """C-15：兼容前端常见 userName 入参及 data.accessToken 嵌套返回。"""
    seen: list[dict[str, Any]] = []

    def login_service(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "nested-token"}})

    headers = await login_for_headers(
        {
            **_AUTH,
            "username_field": "userName",
            "token_path": "data.accessToken",
            "authorization_scheme": "Bearer",
        },
        httpx.MockTransport(login_service),
    )
    assert headers == {"Authorization": "Bearer nested-token"}
    assert seen == [{"userName": "admin", "password": "admin123456"}]


async def test_login_for_headers_auto_recognizes_nested_token() -> None:
    """默认 auto 模式也覆盖主流 data.token 结构，避免每次手工适配。"""
    def login_service(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"token": "nested-token"}})

    headers = await login_for_headers(_AUTH, httpx.MockTransport(login_service))
    assert headers == {"Authorization": "Token nested-token"}


async def test_login_for_headers_cookie_fallback() -> None:
    """无 token 字段 → Set-Cookie 回带（session 会话口径，属性剔除）。"""
    def login_service(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True},
            headers=[
                ("set-cookie", "sessionid=s123; Path=/; HttpOnly"),
                ("set-cookie", "csrftoken=xyz; Path=/"),
            ],
        )

    headers = await login_for_headers(_AUTH, httpx.MockTransport(login_service))
    assert headers == {"Cookie": "sessionid=s123; csrftoken=xyz"}


async def test_login_for_headers_rejects_bad_credentials() -> None:
    """登录 401 → X-EXEC-01（环境类，节点重试口径）。"""
    def login_service(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "凭据错误"})

    with pytest.raises(PipelineError) as exc_info:
        await login_for_headers(_AUTH, httpx.MockTransport(login_service))
    assert exc_info.value.code == "X-EXEC-01"
    assert "401" in exc_info.value.message


async def test_login_for_headers_unreachable() -> None:
    """登录接口不可达 → X-EXEC-01。"""
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(PipelineError) as exc_info:
        await login_for_headers(_AUTH, httpx.MockTransport(unreachable))
    assert exc_info.value.code == "X-EXEC-01"


async def test_login_for_headers_no_credentials_in_response() -> None:
    """响应无 token 且无 Set-Cookie → X-EXEC-01（无凭据可提取）。"""
    def login_service(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    with pytest.raises(PipelineError) as exc_info:
        await login_for_headers(_AUTH, httpx.MockTransport(login_service))
    assert exc_info.value.code == "X-EXEC-01"
    assert "无凭据" in exc_info.value.message
    assert "响应顶层字段：ok" in exc_info.value.message


async def test_auto_execute_with_auth_injects_credentials(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """C-15 全链路：执行前登录 → 凭据头注入全部 REST 请求。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)
    seen_auth: list[str | None] = []

    def secured_service(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/auth/login/":
            return httpx.Response(200, json={"token": "tok-e2e", "username": "admin"})
        seen_auth.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"id": 1, "name": "用户A"})

    _write_case(te_store, expected="2xx 响应结构符合接口定义")
    await run_auto_execute(
        engine, state, te_store, "http://mock:9000",
        httpx.MockTransport(secured_service), auth=_AUTH,
    )
    assert seen_auth == ["Token tok-e2e"]
    payload = te_store.read_json(_JOB, "execution.json")
    assert payload["summary"]["total"] == 1
    assert payload["results"][0]["passed"] is True


async def test_auto_execute_auth_login_failure_degrades(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """C-15：登录失败 → X-EXEC-01 环境类（断言不进缺陷、节点降级口径由 runner 测试覆盖）。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)

    def login_reject(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(401, json={"detail": "凭据错误"})

    _write_case(te_store, expected="2xx 响应结构符合接口定义")
    with pytest.raises(PipelineError) as exc_info:
        await run_auto_execute(
            engine, state, te_store, "http://mock:9000",
            httpx.MockTransport(login_reject), auth=_AUTH,
        )
    assert exc_info.value.code == "X-EXEC-01"


# ---- P-12：Django APPEND_SLASH 301 补偿 ----

async def test_rest_runner_301_append_slash_compensation() -> None:
    """无尾斜杠 POST 被 301 到同路径 + "/" → 同 method 重发一次。"""
    hits: list[str] = []

    def slash_service(request: httpx.Request) -> httpx.Response:
        hits.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/auth/login":
            return httpx.Response(
                301, headers={"Location": "http://mock:9000/api/auth/login/"}
            )
        if request.url.path == "/api/auth/login/":
            return httpx.Response(200, json={"token": "tok-x"})
        return httpx.Response(404)

    runner = RestRunner("http://mock:9000", httpx.MockTransport(slash_service))
    response = await runner.invoke(
        InvokeRequest(
            interface={
                "signature": {"method": "POST", "path": "/api/auth/login"},
                "params": [],
            },
            params={},
        )
    )
    assert response.ok is True
    assert response.status == 200
    assert hits == ["POST /api/auth/login", "POST /api/auth/login/"]


async def test_rest_runner_405_append_slash_compensation() -> None:
    """Nginx 将无尾斜杠 POST 直接拒为 405 时，补斜杠后重发一次。"""
    hits: list[str] = []

    def slash_service(request: httpx.Request) -> httpx.Response:
        hits.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/auth/login":
            return httpx.Response(405)
        if request.url.path == "/api/auth/login/":
            return httpx.Response(200, json={"token": "tok-x"})
        return httpx.Response(404)

    runner = RestRunner("http://mock:9000", httpx.MockTransport(slash_service))
    response = await runner.invoke(
        InvokeRequest(
            interface={
                "signature": {"method": "POST", "path": "/api/auth/login"},
                "params": [],
            },
            params={},
        )
    )
    assert response.status == 200
    assert hits == ["POST /api/auth/login", "POST /api/auth/login/"]


async def test_rest_runner_does_not_attach_session_to_login_path() -> None:
    """登录用例必须使用自身用户名密码，不能携带预登录会话。"""
    seen_headers: dict[str, str] = {}

    def service(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"token": "tok-x"})

    runner = RestRunner(
        "http://mock:9000",
        httpx.MockTransport(service),
        {"Authorization": "Token existing-session"},
        auth_exempt_paths={"/api/auth/login/"},
    )
    response = await runner.invoke(
        InvokeRequest(
            interface={
                "signature": {"method": "POST", "path": "/api/auth/login"},
                "params": [
                    {"name": "username", "in": "body"},
                    {"name": "password", "in": "body"},
                ],
            },
            params={"username": "u", "password": "p"},
        )
    )
    assert response.status == 200
    assert "authorization" not in seen_headers


async def test_rest_runner_prefers_django_trailing_slash() -> None:
    """已知服务使用 Django 尾斜杠路由时，首发即使用正确路径。"""
    hits: list[str] = []

    def service(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        return httpx.Response(200, json={})

    runner = RestRunner(
        "http://mock:9000", httpx.MockTransport(service), prefer_trailing_slash=True
    )
    response = await runner.invoke(
        InvokeRequest(
            interface={"signature": {"method": "GET", "path": "/api/products"}}, params={}
        )
    )
    assert response.status == 200
    assert hits == ["/api/products/"]


async def test_rest_runner_301_to_other_path_not_compensated() -> None:
    """301 指向不同路径（真实重定向）不补偿——防把页面重定向当成功。"""
    hits: list[str] = []

    def redirect_service(request: httpx.Request) -> httpx.Response:
        hits.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/v1/users":
            return httpx.Response(301, headers={"Location": "http://mock:9000/login"})
        return httpx.Response(200, text="page")

    runner = RestRunner("http://mock:9000", httpx.MockTransport(redirect_service))
    response = await runner.invoke(
        InvokeRequest(
            interface={
                "signature": {"method": "GET", "path": "/api/v1/users"},
                "params": [],
            },
            params={},
        )
    )
    assert hits == ["GET /api/v1/users"]  # 仅一次请求，不跟随异路径 301
    assert response.status == 301


def test_mutates_auth_session_recognizes_logout_only() -> None:
    """登出用例完成后要刷新 token；普通已认证查询不应额外登录。"""
    assert mutates_auth_session(
        {"signature": {"path": "/api/auth/logout", "summary": "用户退出登录"}}
    )
    assert not mutates_auth_session(
        {"signature": {"path": "/api/auth/user", "summary": "获取当前登录用户"}}
    )


def test_resolved_auth_mode_supports_explicit_and_legacy_negative_cases() -> None:
    assert resolved_auth_mode({"auth_mode": "invalid"}) == "invalid"
    assert (
        resolved_auth_mode({"category": "exception", "expected": "HTTP 401；token 无效"})
        == "missing"
    )
    assert resolved_auth_mode({"category": "normal", "expected": "status=200"}) == "inherit"


async def test_rest_runner_unwraps_unknown_container_and_can_omit_auth() -> None:
    """unknown 包装参数按真实载荷展开；认证反例不能继承全局 token。"""
    captured: dict[str, Any] = {}

    def service(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    runner = RestRunner(
        "http://mock:9000", httpx.MockTransport(service), {"Authorization": "Token valid"}
    )
    response = await runner.invoke(
        InvokeRequest(
            interface={"signature": {"method": "POST", "path": "/accounts"}, "params": [
                {"name": "account_data", "in": "body", "type": "unknown"}
            ]},
            params={"account_data": {"name": "Alice"}},
            auth_mode="missing",
        )
    )
    assert response.status == 200
    assert captured == {"json": {"name": "Alice"}, "authorization": None}


async def test_rest_runner_unwraps_unknown_query_container() -> None:
    """unknown query 容器必须展开为真实查询键，不能传 query={...}。"""
    captured: dict[str, str] = {}

    def service(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={})

    runner = RestRunner("http://mock:9000", httpx.MockTransport(service))
    response = await runner.invoke(
        InvokeRequest(
            interface={
                "signature": {"method": "GET", "path": "/transactions/query"},
                "params": [{"name": "query", "in": "query", "type": "unknown"}],
            },
            params={"query": {"fund_code": "F001", "business_code": "24"}},
        )
    )
    assert response.status == 200
    assert captured == {"fund_code": "F001", "business_code": "24"}


async def test_auto_execute_routes_interfaces_by_repository_service_url(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """C-07：不同仓库的接口按 code_location.repo 路由到各自服务地址。"""
    engine, te_store, orch_store = env
    state = engine.open_job(_JOB)
    interfaces = orch_store.read_json(_JOB, "interfaces.json")
    interfaces["interfaces"].append(
        {
            "id": "IF-REST-002",
            "protocol": "rest",
            "signature": {"method": "GET", "path": "/api/v1/orders", "summary": "查订单"},
            "params": [],
            "code_location": {
                "repo": "order-center",
                "file": "OrderController.java",
                "start_line": 10,
                "end_line": 20,
            },
            "is_fallback": False,
        }
    )
    orch_store.write(_JOB, "interfaces.json", interfaces)
    cases = te_store.read_json(_JOB, "testcases.json")
    cases["cases"].append(
        {
            "id": "TC-004",
            "interface_id": "IF-REST-002",
            "category": "normal",
            "title": "正常查询订单",
            "expected": "status=200",
            "params": {},
            "script_path": "scripts/test_TC_004.py",
            "loadable": True,
        }
    )
    te_store.write(_JOB, "testcases.json", cases)
    calls: list[tuple[str, str]] = []

    def service(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host or "", request.url.path))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"ok": True})

    await run_auto_execute(
        engine,
        state,
        te_store,
        "http://user-host:9000",
        httpx.MockTransport(service),
        service_base_urls={"order-center": "http://order-host:9100"},
    )

    business_calls = {(host, path) for host, path in calls if path != "/health"}
    assert ("user-host", "/api/v1/users") in business_calls
    assert ("order-host", "/api/v1/orders") in business_calls
