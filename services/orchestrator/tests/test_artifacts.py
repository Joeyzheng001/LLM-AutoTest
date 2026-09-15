"""产物管理器测试：写权排他、Schema 校验前置、节点完成判据、归档。

直接使用仓库根 schemas/ 的真实 Schema 校验（保证代码与冻结物同步）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.artifacts import ArtifactStore
from orchestrator.errors import PipelineError

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
def store(tmp_path: Path) -> ArtifactStore:
    """orchestrator 身份的产物管理器（真实 Schema 目录）。"""
    return ArtifactStore(tmp_path, _SCHEMAS, "orchestrator")


def test_create_job_layout(store: ArtifactStore, tmp_path: Path) -> None:
    """job 目录含四个子目录（docs/12 §二布局）。"""
    job_dir = store.create_job(_JOB)
    for sub in ("scripts", "fixtures", "logs", "archive"):
        assert (job_dir / sub).is_dir()
    assert job_dir == tmp_path / _JOB
    # 幂等：重复创建不报错
    store.create_job(_JOB)


def test_write_valid_artifact(store: ArtifactStore) -> None:
    """合法产物：校验通过后原子落盘，节点判据为真。"""
    path = store.write(_JOB, "requirement_items.json", _VALID_REQUIREMENT_ITEMS)
    assert path.is_file()
    assert store.is_node_completed(_JOB, "requirement_items.json") is True
    assert store.read_json(_JOB, "requirement_items.json")["items"][0]["id"] == "REQ-001"


def test_write_rejects_invalid_payload(store: ArtifactStore) -> None:
    """不合规产物（缺必填字段）拒绝写入：不落盘，报 X-ORCH-01。"""
    bad = {"schema_version": "1.0", "job_id": _JOB}  # 缺 items
    with pytest.raises(PipelineError) as excinfo:
        store.write(_JOB, "requirement_items.json", bad)
    assert excinfo.value.code == "X-ORCH-01"
    assert not (store.job_dir(_JOB) / "requirement_items.json").exists()


def test_write_ownership_is_exclusive(tmp_path: Path) -> None:
    """写权排他：test-executor 不得写 orchestrator 的产物，反之亦然。"""
    other = ArtifactStore(tmp_path, _SCHEMAS, "test-executor")
    with pytest.raises(PipelineError) as excinfo:
        other.write(_JOB, "requirement_items.json", _VALID_REQUIREMENT_ITEMS)
    assert excinfo.value.code == "X-ORCH-01"
    assert "写权冲突" in excinfo.value.message

    store = ArtifactStore(tmp_path, _SCHEMAS, "orchestrator")
    with pytest.raises(PipelineError) as excinfo:
        store.write(_JOB, "execution.json", {})
    assert "写权冲突" in excinfo.value.message


def test_write_rejects_unknown_filename(store: ArtifactStore) -> None:
    """清单外文件名（自造别名）禁止写入。"""
    with pytest.raises(PipelineError) as excinfo:
        store.write(_JOB, "requirements.json", {})
    assert excinfo.value.code == "X-ORCH-01"


def test_is_node_completed_missing_or_corrupt(store: ArtifactStore) -> None:
    """产物缺失 / JSON 损坏时节点判据为假（不把存在当完成）。"""
    assert store.is_node_completed(_JOB, "interfaces.json") is False
    job_dir = store.create_job(_JOB)
    (job_dir / "interfaces.json").write_text("{broken", encoding="utf-8")
    assert store.is_node_completed(_JOB, "interfaces.json") is False


def test_archive_before_rerun(store: ArtifactStore) -> None:
    """--force-node 重跑前归档旧产物：移入 archive/，禁止原地覆盖。"""
    store.write(_JOB, "requirement_items.json", _VALID_REQUIREMENT_ITEMS)
    archived = store.archive(_JOB, "requirement_items.json")
    assert archived is not None and archived.is_file()
    assert archived.parent.name == "archive"
    assert not (store.job_dir(_JOB) / "requirement_items.json").exists()
    # 无旧产物时归档返回 None（不报错）
    assert store.archive(_JOB, "metrics.json") is None


def test_md_artifact_existence_only(store: ArtifactStore) -> None:
    """test_report.md 无 JSON Schema：完成判据为非空存在。"""
    assert store.is_node_completed(_JOB, "test_report.md") is False
    job_dir = store.create_job(_JOB)
    (job_dir / "test_report.md").write_text("# 测试报告\n", encoding="utf-8")
    assert store.is_node_completed(_JOB, "test_report.md") is True
