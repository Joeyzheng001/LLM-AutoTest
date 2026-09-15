"""test_report 节点测试：程序化汇总 / 真实-兜底分列 / X-RPT-01/02 / metrics 快照。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.artifacts import ArtifactStore
from orchestrator.engine import Engine
from orchestrator.errors import X_ORCH_01, X_ORCH_02, PipelineError
from orchestrator.nodes import run_test_report
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

_EXECUTION: dict[str, Any] = {
    "schema_version": "1.0",
    "job_id": _JOB,
    "results": [
        {
            "case_id": "TC-001",
            "interface_id": "IF-REST-001",
            "passed": True,
            "duration_ms": 12,
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
            "interface_id": "IF-GRPC-001",
            "passed": True,
            "duration_ms": 0,
            "timestamp": "2026-09-15T00:00:02+00:00",
        },
    ],
    "summary": {
        "total": 3,
        "passed": 2,
        "failed_assertion": 1,
        "failed_environment": 0,
    },
    "degraded_protocols": [
        {
            "protocol": "grpc",
            "mode": "脚本生成成功+Mock执行演示",
            "reason": "Runner 未就绪（X-EXEC-03），降级为 Mock 执行演示",
        }
    ],
}


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Engine, ArtifactStore]:
    """引擎 + orchestrator 产物卷 + 前六节点已完成的前置状态。"""
    store = ArtifactStore(tmp_path, _SCHEMAS, "orchestrator")
    engine = Engine(store)
    state = engine.init_job(_JOB)
    orch_artifacts: list[tuple[str, dict[str, Any]]] = [
        (
            "requirement_items.json",
            {
                "schema_version": "1.0",
                "job_id": _JOB,
                "items": [{"id": "REQ-001", "title": "用户查询", "description": "按 ID 查询用户"}],
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
                        "protocol": "grpc",
                        "signature": "OrderService/GetOrder",
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
                    }
                ],
                "risks": [],
                "missing_features": [],
            },
        ),
    ]
    for node_name in ("req_parse", "code_scan", "interface_reconstruct", "code_review"):
        engine.mark_running(state, node_name)
    for _filename, payload in orch_artifacts:
        store.write(_JOB, _filename, payload)
    engine.mark_completed(state, "req_parse")
    engine.mark_completed(state, "code_scan")
    engine.mark_completed(state, "interface_reconstruct")
    engine.mark_completed(state, "code_review")
    # GEN/EXEC 属丙写：直接经 te 身份落盘，状态由产物判据补齐（open_job 时）
    te_store = ArtifactStore(tmp_path, _SCHEMAS, "test-executor")
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
            ],
        },
    )
    te_store.write(_JOB, "execution.json", _EXECUTION)
    engine.mark_running(state, "testcase_generate")
    engine.mark_completed(state, "testcase_generate")
    engine.mark_running(state, "auto_execute")
    engine.mark_completed(state, "auto_execute")
    return engine, store


async def test_report_real_demo_split(env: tuple[Engine, ArtifactStore]) -> None:
    """双产物落盘；真实/兜底分列（D5）+ 降级说明（F-RPT-03）+ 通过率口径。"""
    engine, store = env
    state = engine.open_job(_JOB)

    path = await run_test_report(engine, state, store)

    assert path.is_file()
    report = path.read_text(encoding="utf-8")
    # D5 分列：真实执行章节只含 REST，演示章节只含 gRPC
    assert "## 2. 真实执行明细" in report
    assert "## 3. Mock 执行演示明细" in report
    section2 = report.split("## 2. 真实执行明细")[1].split("## 3.")[0]
    assert "TC-001" in section2 and "TC-003" not in section2
    section3 = report.split("## 3. Mock 执行演示明细")[1].split("## 4.")[0]
    assert "TC-003" in section3 and "不得作为真实质量结论" in section3
    # F-RPT-03 降级说明
    assert "## 4. 降级说明" in report and "grpc" in report
    # 汇总数值（程序化，F-RPT-02）
    assert "| 总用例 | 3 |" in report
    assert "| 通过 | 2 |" in report
    assert "| 断言失败（计缺陷） | 1 |" in report
    assert "66.7%（2/3 可判读）" in report
    # 失败明细含原因
    assert "HTTP 500" in report
    # metrics 双产物
    metrics = store.read_json(_JOB, "metrics.json")
    assert metrics["automation_rate"]["denominator"] == 9
    assert metrics["automation_rate"]["value"] is None  # data_complete=false（X-MTR-02）
    assert metrics["automation_rate"]["data_complete"] is False
    node_by_name = {n["name"]: n for n in metrics["nodes"]}
    assert node_by_name["req_parse"]["status"] == "completed"
    assert node_by_name["test_report"]["status"] == "completed"
    assert node_by_name["defect_list"]["status"] == "manual_intervention"
    assert "未执行" in node_by_name["retest_regression"]["intervention_reason"]
    # 节点完成
    assert state.node("test_report").status is NodeStatus.COMPLETED


async def test_report_all_environment_failure(
    env: tuple[Engine, ArtifactStore],
) -> None:
    """X-RPT-02：全量环境失败 → 无可判读结果，通过率不输出（而非 0%）。"""
    engine, store = env
    state = engine.open_job(_JOB)
    execution = json.loads(json.dumps(_EXECUTION))
    execution["results"] = [
        {
            "case_id": r["case_id"],
            "interface_id": "IF-REST-001",
            "passed": False,
            "failure_kind": "environment",
            "failure_reason": "被测服务不可达（X-EXEC-01，探测失败）",
            "timeout": False,
            "duration_ms": 0,
            "timestamp": r["timestamp"],
        }
        for r in _EXECUTION["results"]
    ]
    execution["summary"] = {
        "total": 3,
        "passed": 0,
        "failed_assertion": 0,
        "failed_environment": 3,
    }
    execution.pop("degraded_protocols")
    te_store = ArtifactStore(store.job_dir(_JOB).parent, _SCHEMAS, "test-executor")
    te_store.write(_JOB, "execution.json", execution)

    await run_test_report(engine, state, store)

    report = (store.job_dir(_JOB) / "test_report.md").read_text(encoding="utf-8")
    assert "不输出——无可判读结果（X-RPT-02" in report
    assert "0.0%" not in report


async def test_report_execution_missing(env: tuple[Engine, ArtifactStore]) -> None:
    """X-RPT-01：execution.json 缺失 → 节点失败，禁止空报告。"""
    engine, store = env
    state = engine.open_job(_JOB)
    (store.job_dir(_JOB) / "execution.json").unlink()

    with pytest.raises(PipelineError) as err:
        await run_test_report(engine, state, store)

    assert err.value.code == X_ORCH_01
    assert "X-RPT-01" in err.value.message
    assert not (store.job_dir(_JOB) / "test_report.md").exists()
    assert state.node("test_report").status is NodeStatus.PENDING


async def test_report_execution_corrupted(env: tuple[Engine, ArtifactStore]) -> None:
    """X-RPT-01：execution.json 损坏 → 节点失败。"""
    engine, store = env
    state = engine.open_job(_JOB)
    (store.job_dir(_JOB) / "execution.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(PipelineError) as err:
        await run_test_report(engine, state, store)

    assert err.value.code == X_ORCH_01
    assert "损坏" in err.value.message


async def test_report_pipeline_degraded_nodes_listed(
    env: tuple[Engine, ArtifactStore],
) -> None:
    """流水线降级节点进降级说明章节 + metrics 按 D1 计人工介入。"""
    engine, store = env
    state = engine.open_job(_JOB)
    # 把 code_review 模拟为降级节点（状态手动改，产物保留）
    state.node("code_review").status = NodeStatus.MANUAL_INTERVENTION
    state.degraded = (
        {
            "node": "code_review",
            "reason": "LLM 输出不可信，人工复核",
            "marked_at": "2026-09-15T00:00:00+00:00",
        },
    )
    # 降级节点不阻塞下游（D1）：auto_execute 已完成，check_runnable 只看前序
    await run_test_report(engine, state, store)

    report = (store.job_dir(_JOB) / "test_report.md").read_text(encoding="utf-8")
    assert "`code_review`：LLM 输出不可信，人工复核" in report
    metrics = store.read_json(_JOB, "metrics.json")
    node_by_name = {n["name"]: n for n in metrics["nodes"]}
    assert node_by_name["code_review"]["status"] == "manual_intervention"
    assert node_by_name["code_review"]["degraded"] is True
    assert node_by_name["code_review"]["degrade_reason"] == "LLM 输出不可信，人工复核"


async def test_report_skip_order_guard(env: tuple[Engine, ArtifactStore]) -> None:
    """auto_execute 未完成：X-ORCH-02。"""
    engine, store = env
    state = engine.open_job(_JOB)
    state.node("auto_execute").status = NodeStatus.PENDING

    with pytest.raises(PipelineError) as err:
        await run_test_report(engine, state, store)

    assert err.value.code == X_ORCH_02
