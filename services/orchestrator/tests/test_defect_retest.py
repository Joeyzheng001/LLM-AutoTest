"""defect_list / retest_regression 节点测试。

fixture：te 身份产物卷 + 前七节点完成 + 含断言失败/环境失败/通过的 execution。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from helpers import requirement_review_payload
from orchestrator.artifacts import ArtifactStore
from orchestrator.engine import Engine
from orchestrator.errors import X_ORCH_01, X_ORCH_02, PipelineError
from orchestrator.nodes import run_defect_list, run_retest_regression
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
        "id": "IF-REST-002",
        "protocol": "rest",
        "signature": {"method": "GET", "path": "/api/v1/orders", "summary": "查订单"},
        "params": [{"name": "order_id", "type": "string", "required": True, "in": "query"}],
        "code_location": {
            "repo": "order-center",
            "file": "OrderController.java",
            "start_line": 10,
            "end_line": 20,
        },
        "is_fallback": True,
    },
]

_CASES: list[dict[str, Any]] = [
    {
        "id": "TC-001",
        "interface_id": "IF-REST-001",
        "requirement_id": "REQ-001",
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
        "requirement_id": "REQ-001",
        "category": "exception",
        "title": "非法 id",
        "expected": "400",
        "params": {"id": -1},
        "script_path": "scripts/test_TC_002.py",
        "loadable": True,
    },
    {
        "id": "TC-003",
        "interface_id": "IF-REST-002",
        "requirement_id": "REQ-002",
        "category": "normal",
        "title": "查订单",
        "expected": "订单详情",
        "params": {"order_id": "O-1"},
        "script_path": "scripts/test_TC_003.py",
        "loadable": True,
    },
    {
        "id": "TC-004",
        "interface_id": "IF-REST-001",
        "requirement_id": "REQ-001",
        "category": "boundary",
        "title": "环境失败用例",
        "expected": "200",
        "params": {"id": 999},
        "script_path": "scripts/test_TC_004.py",
        "loadable": True,
    },
]

_EXECUTION: dict[str, Any] = {
    "schema_version": "1.0",
    "job_id": _JOB,
    "results": [
        {
            "case_id": "TC-001",
            "interface_id": "IF-REST-001",
            "passed": True,
            "duration_ms": 5,
            "timestamp": "2026-09-15T00:00:00+00:00",
        },
        {
            "case_id": "TC-002",
            "interface_id": "IF-REST-001",
            "passed": False,
            "failure_kind": "assertion",
            "failure_reason": "HTTP 500（演示态简化断言：2xx 判通过；语义断言：400）",
            "timeout": False,
            "duration_ms": 8,
            "timestamp": "2026-09-15T00:00:01+00:00",
        },
        {
            "case_id": "TC-003",
            "interface_id": "IF-REST-002",
            "passed": False,
            "failure_kind": "assertion",
            "failure_reason": "HTTP 500（演示态简化断言：2xx 判通过；语义断言：订单详情）",
            "timeout": False,
            "duration_ms": 7,
            "timestamp": "2026-09-15T00:00:02+00:00",
        },
        {
            "case_id": "TC-004",
            "interface_id": "IF-REST-001",
            "passed": False,
            "failure_kind": "environment",
            "failure_reason": "被测服务不可达（X-EXEC-01，探测失败）",
            "timeout": False,
            "duration_ms": 0,
            "timestamp": "2026-09-15T00:00:03+00:00",
        },
    ],
    "summary": {"total": 4, "passed": 1, "failed_assertion": 2, "failed_environment": 1},
}

_METRICS: dict[str, Any] = {
    "schema_version": "1.0",
    "job_id": _JOB,
    "automation_rate": {"value": None, "numerator": 7, "denominator": 9, "data_complete": False},
    "nodes": [
        {"name": n, "status": "completed"}
        for n in (
            "req_parse",
            "code_scan",
            "interface_reconstruct",
            "code_review",
            "testcase_generate",
            "auto_execute",
            "test_report",
        )
    ]
    + [
        {"name": n, "status": "manual_intervention", "intervention_reason": "未执行（fixture）"}
        for n in ("defect_list", "retest_regression")
    ],
}

_ORCH_ARTIFACTS: list[tuple[str, dict[str, Any]]] = [
    (
        "requirement_items.json",
        {
            "schema_version": "1.0",
            "job_id": _JOB,
            "items": [
                {"id": "REQ-001", "title": "用户查询", "description": "按 ID 查询用户"},
                {"id": "REQ-002", "title": "订单查询", "description": "按单号查询订单"},
            ],
        },
    ),
    (
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
                    "protocol": "rest",
                    "signature": "GET /api/v1/orders",
                    "code_location": _INTERFACES[1]["code_location"],
                    "is_fallback": False,
                },
            ],
        },
    ),
    ("interfaces.json", {"schema_version": "1.0", "job_id": _JOB, "interfaces": _INTERFACES}),
    (
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
                },
                {
                    "requirement_id": "REQ-002",
                    "interface_id": "IF-REST-002",
                    "status": "implemented",
                    "confidence": 0.9,
                },
            ],
            "risks": [],
            "missing_features": [],
        },
    ),
    ("test_report.md", ""),
]


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Engine, ArtifactStore, ArtifactStore]:
    """引擎 + 双身份产物卷 + 前七节点已完成（test_report 由产物判据补齐状态）。"""
    orch_store = ArtifactStore(tmp_path, _SCHEMAS, "orchestrator")
    te_store = ArtifactStore(tmp_path, _SCHEMAS, "test-executor")
    engine = Engine(orch_store)
    state = engine.init_job(_JOB)
    orch_store.write(_JOB, "requirement_review.json", requirement_review_payload(_JOB))
    for node_name in (
        "req_parse",
        "code_scan",
        "interface_reconstruct",
        "code_review",
        "testcase_generate",
        "auto_execute",
        "test_report",
    ):
        engine.mark_running(state, node_name)
    for filename, payload in _ORCH_ARTIFACTS:
        if filename == "test_report.md":
            orch_store.write_raw(_JOB, filename, "# 占位报告（fixture）\n")
        else:
            orch_store.write(_JOB, filename, payload)
    # test_report 双产物：metrics.json（甲写，Schema 校验）
    orch_store.write(_JOB, "metrics.json", _METRICS)
    te_store.write(
        _JOB, "testcases.json", {"schema_version": "1.0", "job_id": _JOB, "cases": _CASES}
    )
    te_store.write(_JOB, "execution.json", _EXECUTION)
    # 用例脚本落盘（复测与归档对象）
    for case in _CASES:
        rel = str(case["script_path"])
        target = te_store.job_dir(_JOB) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {case['id']} 脚本\n", encoding="utf-8")
    for node_name in ("req_parse", "code_scan", "interface_reconstruct", "code_review"):
        engine.mark_completed(state, node_name)
    engine.mark_completed(state, "testcase_generate")
    engine.mark_completed(state, "auto_execute")
    engine.mark_completed(state, "test_report")
    return engine, te_store, orch_store


def _ok_service(request: httpx.Request) -> httpx.Response:
    """模拟被测 REST 服务全通。"""
    if request.url.path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    return httpx.Response(200, json={"ok": True})


def _fail_service(request: httpx.Request) -> httpx.Response:
    """模拟被测 REST 服务持续 500（复测仍失败）。"""
    if request.url.path == "/health":
        return httpx.Response(200)
    return httpx.Response(500, json={"error": "boom"})


async def _run_defect_list(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> dict[str, Any]:
    """helper：跑 defect_list 并返回产物。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)
    await run_defect_list(engine, state, te_store)
    return te_store.read_json(_JOB, "defects.json")


async def test_defect_list_only_assertion_failures(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """R-DEF-01：仅断言失败进清单；环境失败不入；DEF-### 程序编号。"""
    payload = await _run_defect_list(env)

    defects = payload["defects"]
    assert [d["case_id"] for d in defects] == ["TC-002", "TC-003"]
    assert [d["id"] for d in defects] == ["DEF-001", "DEF-002"]
    # 环境失败（TC-004）不入清单
    assert all(d["case_id"] != "TC-004" for d in defects)
    # F-DEF-01 复现步骤：脚本引用 + 参数回填
    assert defects[0]["reproduce"] == {
        "script_path": "scripts/test_TC_002.py",
        "params": {"id": -1},
    }
    # X-DEF-03：is_fallback 从接口透传
    assert defects[0]["is_fallback"] is False
    assert defects[1]["is_fallback"] is True
    # R-DEF-03：history 起点 discovered
    assert defects[0]["history"][0]["event"] == "discovered"
    assert defects[0]["status"] == "discovered"
    # 失败原因透传
    assert "HTTP 500" in defects[0]["failure_reason"]


async def test_defect_list_skip_order_guard(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """auto_execute 未完成：X-ORCH-02。"""
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)
    state.node("auto_execute").status = NodeStatus.PENDING

    with pytest.raises(PipelineError) as err:
        await run_defect_list(engine, state, te_store)

    assert err.value.code == X_ORCH_02


async def test_retest_regression_pass_and_archive(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """复测通过 → closed_suggested + 脚本移动归档 + 溯源随档（F-DEF-03/F-REG-01/02）。"""
    await _run_defect_list(env)
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)

    path = await run_retest_regression(
        engine, state, te_store, "http://mock:9000", httpx.MockTransport(_ok_service)
    )

    assert path.is_file()
    # F-DEF-03：复测通过 → 关闭建议 + history 流转
    defects = te_store.read_json(_JOB, "defects.json")["defects"]
    assert all(d["status"] == "closed_suggested" for d in defects)
    events = [h["event"] for h in defects[0]["history"]]
    assert events == ["discovered", "retest_started", "retest_passed", "close_suggested"]
    # F-REG-01：归档 = 移动（原目录不再含沉淀脚本）
    job_dir = te_store.job_dir(_JOB)
    assert not (job_dir / "scripts/test_TC_002.py").exists()
    assert not (job_dir / "scripts/test_TC_003.py").exists()
    assert not (job_dir / "scripts/test_TC_001.py").exists()  # 已执行通过的基线也沉淀
    # F-REG-02：溯源随档 + R-REG-02 按协议组织
    index = te_store.read_json(_JOB, "regression_index.json")
    assert index["suites"] == [
        {
            "protocol": "rest",
            "scripts": [
                {
                    "case_id": "TC-002",
                    "script_path": "scripts/regression/rest/test_TC_002.py",
                    "requirement_id": "REQ-001",
                    "interface_id": "IF-REST-001",
                    "archived_at": defects[0]["history"][-1]["timestamp"],
                },
                    {
                        "case_id": "TC-003",
                        "script_path": "scripts/regression/rest/test_TC_003.py",
                        "requirement_id": "REQ-002",
                        "interface_id": "IF-REST-002",
                        "archived_at": defects[1]["history"][-1]["timestamp"],
                    },
                    {
                        "case_id": "TC-001",
                        "script_path": "scripts/regression/rest/test_TC_001.py",
                        "requirement_id": "REQ-001",
                        "interface_id": "IF-REST-001",
                        "archived_at": index["suites"][0]["scripts"][2]["archived_at"],
                    },
            ],
        }
    ]
    assert state.node("retest_regression").status is NodeStatus.COMPLETED


async def test_retest_regression_still_failed_retained(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """复测仍失败 → retained + 脚本留原目录（未沉淀）。"""
    await _run_defect_list(env)
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)

    await run_retest_regression(
        engine, state, te_store, "http://mock:9000", httpx.MockTransport(_fail_service)
    )

    defects = te_store.read_json(_JOB, "defects.json")["defects"]
    assert all(d["status"] == "retained" for d in defects)
    events = [h["event"] for h in defects[0]["history"]]
    assert events == ["discovered", "retest_started", "retest_failed", "retained"]
    # 脚本未移动（R-REG-01：原执行目录保持未沉淀用例）
    job_dir = te_store.job_dir(_JOB)
    assert (job_dir / "scripts/test_TC_002.py").exists()
    assert not (job_dir / "scripts/regression/rest/test_TC_002.py").exists()
    # 即使缺陷仍失败，已执行通过的非缺陷基线仍可沉淀
    index = te_store.read_json(_JOB, "regression_index.json")
    assert [s["case_id"] for s in index["suites"][0]["scripts"]] == ["TC-001"]


async def test_retest_regression_script_missing_skipped(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """X-DEF-02：脚本被人工删除 → 指明缺失用例，该项跳过（不流转不归档）。"""
    await _run_defect_list(env)
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)
    (te_store.job_dir(_JOB) / "scripts/test_TC_002.py").unlink()

    await run_retest_regression(
        engine, state, te_store, "http://mock:9000", httpx.MockTransport(_ok_service)
    )

    payload = te_store.read_json(_JOB, "defects.json")
    defects = payload["defects"]
    by_case = {d["case_id"]: d for d in defects}
    # TC-002 跳过：保持 discovered，history 无复测事件
    assert by_case["TC-002"]["status"] == "discovered"
    assert [h["event"] for h in by_case["TC-002"]["history"]] == ["discovered"]
    # TC-003 正常复测流转
    assert by_case["TC-003"]["status"] == "closed_suggested"
    # 警告指明缺失用例
    assert len(payload["warnings"]) == 1
    assert "X-DEF-02" in payload["warnings"][0]
    assert "TC-002" in payload["warnings"][0]
    # 复测通过的 TC-003 与已执行通过的 TC-001 均归档
    index = te_store.read_json(_JOB, "regression_index.json")
    assert [s["case_id"] for s in index["suites"][0]["scripts"]] == ["TC-003", "TC-001"]


async def test_retest_regression_no_requirement_skips_archive(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """F-REG-02 溯源缺失：复测通过但不归档，警告记录（不造数据）。"""
    # 去掉 TC-002 的 requirement_id
    te_only = env[1]
    testcases = te_only.read_json(_JOB, "testcases.json")
    for case in testcases["cases"]:
        if case["id"] == "TC-002":
            case.pop("requirement_id", None)
    te_only.write(_JOB, "testcases.json", testcases)
    await _run_defect_list(env)
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)

    await run_retest_regression(
        engine, state, te_store, "http://mock:9000", httpx.MockTransport(_ok_service)
    )

    payload = te_store.read_json(_JOB, "defects.json")
    # 缺陷仍正常流转（状态与沉淀独立）
    assert all(d["status"] == "closed_suggested" for d in payload["defects"])
    assert any("F-REG-02 溯源缺失" in w for w in payload["warnings"])
    # 无溯源用例脚本留原目录
    job_dir = te_store.job_dir(_JOB)
    assert (job_dir / "scripts/test_TC_002.py").exists()
    assert not (job_dir / "scripts/regression/rest/test_TC_002.py").exists()
    # 无溯源的 TC-002 不沉淀；TC-003 与已执行通过的 TC-001 均沉淀
    index = te_store.read_json(_JOB, "regression_index.json")
    assert [s["case_id"] for s in index["suites"][0]["scripts"]] == ["TC-003", "TC-001"]


async def test_retest_regression_missing_defects(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """defects.json 文件缺失（状态仍 completed）：X-ORCH-01（X-RPT-01 同源口径）。"""
    await _run_defect_list(env)
    engine, te_store, _orch_store = env
    state = engine.open_job(_JOB)
    (te_store.job_dir(_JOB) / "defects.json").unlink()

    with pytest.raises(PipelineError) as err:
        await run_retest_regression(
            engine, state, te_store, "http://mock:9000", httpx.MockTransport(_ok_service)
        )

    assert err.value.code == X_ORCH_01
    assert "defects.json 缺失" in err.value.message
    assert state.node("retest_regression").status is NodeStatus.PENDING
