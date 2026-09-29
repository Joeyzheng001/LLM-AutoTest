"""编排引擎测试：断点续跑对账、跳序防护、降级清单、完成判据强制。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from common.artifacts import ArtifactStore
from common.engine import NODE_ARTIFACTS, Engine, JobState
from common.errors import PipelineError
from common.pipeline import NODE_ORDER, NodeStatus

from helpers import change_set_payload, requirement_review_payload

_SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"
_JOB = "job-3f2a1b8c-0000-4000-8000-000000000001"

_VALID_REQUIREMENT_ITEMS = {
    "schema_version": "1.0",
    "job_id": _JOB,
    "items": [
        {"id": "REQ-001", "title": "用户查询", "description": "按 ID 查询用户信息"},
    ],
}


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Engine, ArtifactStore]:
    """引擎 + 同一产物卷。"""
    store = ArtifactStore(tmp_path, _SCHEMAS, "orchestrator")
    return Engine(store), store


def test_node_artifacts_cover_nine_nodes() -> None:
    """节点→产物映射与 NODE_ORDER 完全一致（口径锁死测试）。"""
    assert tuple(NODE_ARTIFACTS) == NODE_ORDER


def test_init_job_writes_initial_state(env: tuple[Engine, ArtifactStore]) -> None:
    """新建 job：九节点全 pending，state.json 落盘。"""
    engine, store = env
    state = engine.init_job(_JOB)
    assert len(state.nodes) == 9
    assert all(n.status is NodeStatus.PENDING for n in state.nodes)
    assert engine.next_node(state) == "req_parse"
    assert (store.job_dir(_JOB) / "state.json").is_file()
    # 误重复新建：拒绝（防覆盖历史）
    with pytest.raises(PipelineError, match="已存在"):
        engine.init_job(_JOB)


def test_open_missing_job_rejected(env: tuple[Engine, ArtifactStore]) -> None:
    """打开不存在的 job：X-ORCH-01。"""
    engine, _ = env
    with pytest.raises(PipelineError) as excinfo:
        engine.open_job("job-nonexist")
    assert excinfo.value.code == "X-ORCH-01"


def test_check_runnable_blocks_skip(env: tuple[Engine, ArtifactStore]) -> None:
    """跳序防护：前序未完成时请求后续节点 → X-ORCH-02。"""
    engine, _ = env
    state = engine.init_job(_JOB)
    with pytest.raises(PipelineError) as excinfo:
        engine.check_runnable(state, "code_review")
    assert excinfo.value.code == "X-ORCH-02"
    assert excinfo.value.details == {"blocked_by": "req_parse"}
    # 首节点永远可跑
    engine.check_runnable(state, "req_parse")


def test_mark_completed_requires_artifact(env: tuple[Engine, ArtifactStore]) -> None:
    """完成判据强制：产物未落盘时拒绝置 completed（X-ORCH-01）。"""
    engine, _ = env
    state = engine.init_job(_JOB)
    engine.mark_running(state, "req_parse")
    with pytest.raises(PipelineError) as excinfo:
        engine.mark_completed(state, "req_parse")
    assert excinfo.value.code == "X-ORCH-01"


def test_full_lifecycle_one_node(env: tuple[Engine, ArtifactStore]) -> None:
    """单节点完整生命周期：running → 产物落盘 → completed → 续跑跳过。"""
    engine, store = env
    state = engine.init_job(_JOB)
    engine.mark_running(state, "req_parse")
    engine.mark_failed(state, "req_parse", PipelineError(code="X-LLM-01", message="超时"))
    assert state.node("req_parse").status is NodeStatus.PENDING
    assert state.node("req_parse").attempts == 1
    assert state.node("req_parse").error is not None

    # 产物落盘后置完成
    store.write(_JOB, "requirement_review.json", requirement_review_payload(_JOB))
    store.write(_JOB, "requirement_items.json", _VALID_REQUIREMENT_ITEMS)
    engine.mark_completed(state, "req_parse")
    assert state.node("req_parse").status is NodeStatus.COMPLETED
    assert engine.next_node(state) == "code_scan"

    # 断点续跑：重启后已完成节点被跳过（next_node 仍指向 code_scan）
    reopened = engine.open_job(_JOB)
    assert reopened.node("req_parse").status is NodeStatus.COMPLETED
    assert engine.next_node(reopened) == "code_scan"
    assert reopened.resumed == 1
    # 完成节点的前序约束现在满足
    engine.check_runnable(reopened, "code_scan")


def test_reconcile_repairs_state_from_artifacts(env: tuple[Engine, ArtifactStore]) -> None:
    """对账以产物为准：产物在而状态未记 → 补 completed（崩溃恢复场景）。"""
    engine, store = env
    engine.init_job(_JOB)
    store.write(_JOB, "requirement_review.json", requirement_review_payload(_JOB))
    store.write(_JOB, "requirement_items.json", _VALID_REQUIREMENT_ITEMS)
    state = engine.open_job(_JOB)  # state 里 req_parse 仍是 pending
    assert state.node("req_parse").status is NodeStatus.COMPLETED


def test_reconcile_downgrades_on_artifact_loss(env: tuple[Engine, ArtifactStore]) -> None:
    """对账诚实降级：状态 completed 但产物失效 → 回 pending 并记错误。"""
    engine, store = env
    state = engine.init_job(_JOB)
    store.write(_JOB, "requirement_review.json", requirement_review_payload(_JOB))
    store.write(_JOB, "requirement_items.json", _VALID_REQUIREMENT_ITEMS)
    engine.mark_completed(state, "req_parse")
    # 产物被外力破坏（内容改为不合规）
    (store.job_dir(_JOB) / "requirement_items.json").write_text("{}", encoding="utf-8")
    reopened = engine.open_job(_JOB)
    assert reopened.node("req_parse").status is NodeStatus.PENDING
    assert reopened.node("req_parse").error is not None


def test_mark_degraded_keeps_denominator(env: tuple[Engine, ArtifactStore]) -> None:
    """降级按 manual_intervention 计入：automation_rate = completed/9（D1 分母不缩减）。"""
    engine, store = env
    state = engine.init_job(_JOB)
    store.write(_JOB, "requirement_review.json", requirement_review_payload(_JOB))
    store.write(_JOB, "requirement_items.json", _VALID_REQUIREMENT_ITEMS)
    engine.mark_completed(state, "req_parse")
    engine.mark_degraded(state, "code_scan", "仓库不可达，转为人工扫描")
    assert state.node("code_scan").status is NodeStatus.MANUAL_INTERVENTION
    assert state.degraded[0]["node"] == "code_scan"
    assert state.automation_rate == pytest.approx(1 / 9)
    # 降级节点视同已处理：next_node 跳过它，后续节点可继续自动推进（D1 口径）
    assert engine.next_node(state) == "interface_reconstruct"
    engine.check_runnable(state, "interface_reconstruct")


def test_state_roundtrip(env: tuple[Engine, ArtifactStore]) -> None:
    """state.json 序列化/反序列化无损（含 error 与 degraded）。"""
    engine, store = env
    state = engine.init_job(_JOB)
    engine.mark_running(state, "req_parse")
    engine.mark_failed(state, "req_parse", PipelineError(code="X-LLM-01", message="超时"))
    engine.mark_degraded(state, "code_scan", "演示兜底")
    loaded = JobState.from_dict(
        json.loads((store.job_dir(_JOB) / "state.json").read_text(encoding="utf-8"))
    )
    assert loaded.node("req_parse").attempts == 1
    assert loaded.node("req_parse").error is not None
    assert loaded.degraded == state.degraded


def test_corrupt_state_rejected(env: tuple[Engine, ArtifactStore]) -> None:
    """state.json 损坏：X-ORCH-01 拒绝（宁停不猜）。"""
    engine, store = env
    engine.init_job(_JOB)
    (store.job_dir(_JOB) / "state.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(PipelineError) as excinfo:
        engine.open_job(_JOB)
    assert excinfo.value.code == "X-ORCH-01"


def test_interface_reconstruct_requires_all_artifacts(
    env: tuple[Engine, ArtifactStore],
) -> None:
    """interface_reconstruct 完成判据：候选、接口、变更集三份产物都合规（#52 新增第三份）。"""
    engine, store = env
    state = engine.init_job(_JOB)
    # 只落 candidates 不落 interfaces/change_set：不得置完成
    store.write(
        _JOB,
        "interface_candidates.json",
        {"schema_version": "1.0", "job_id": _JOB, "candidates": []},
    )
    with pytest.raises(PipelineError):
        engine.mark_completed(state, "interface_reconstruct")
    # 补齐 candidates + interfaces，仍缺 change_set：依然不得置完成
    store.write(
        _JOB,
        "interfaces.json",
        {
            "schema_version": "1.0",
            "job_id": _JOB,
            "interfaces": [
                {
                    "id": "IF-REST-001",
                    "protocol": "rest",
                    "signature": {"summary": "查用户"},
                    "code_location": {
                        "repo": "user-service",
                        "file": "UserController.java",
                        "start_line": 1,
                        "end_line": 2,
                    },
                    "is_fallback": False,
                }
            ],
        },
    )
    with pytest.raises(PipelineError):
        engine.mark_completed(state, "interface_reconstruct")
    store.write(_JOB, "change_set.json", change_set_payload(_JOB))
    engine.mark_completed(state, "interface_reconstruct")
