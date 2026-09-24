"""产物管理器测试：写权排他、Schema 校验前置、节点完成判据、归档。

直接使用仓库根 schemas/ 的真实 Schema 校验（保证代码与冻结物同步）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from common.artifacts import ArtifactStore
from common.errors import PipelineError
from common.object_storage import ObjectStorageError

from orchestrator.persistence import Database

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


class _FakeObjectStorage:
    """不访问网络的镜像后端替身。"""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.uploads: list[tuple[str, str]] = []
        self.objects: dict[str, bytes] = {}

    def put_file(self, local_path: Path, object_key: str) -> str:
        if self.fail:
            raise ObjectStorageError("fake upload failed")
        self.uploads.append((str(local_path), object_key))
        self.objects[object_key] = local_path.read_bytes()
        return self.uri_for(object_key)

    def uri_for(self, object_key: str) -> str:
        return f"s3://bucket/{object_key}"

    def exists(self, object_key: str) -> bool:
        return object_key in self.objects

    def get_file(self, object_key: str, destination: Path) -> None:
        if object_key not in self.objects:
            raise ObjectStorageError("fake object missing")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.objects[object_key])


def test_object_storage_mirror_and_manifest(tmp_path: Path) -> None:
    """对象存储开启时本地仍可读，同时镜像键和 manifest 地址稳定。"""
    backend = _FakeObjectStorage()
    store = ArtifactStore(tmp_path, _SCHEMAS, "orchestrator", object_storage=backend)
    store.write(_JOB, "requirement_items.json", _VALID_REQUIREMENT_ITEMS)
    path = store.job_dir(_JOB) / "input" / "requirements.md"
    path.parent.mkdir(parents=True)
    path.write_text("# req\n", encoding="utf-8")
    store.mirror_file(_JOB, "input/requirements.md")
    manifest = store.write_manifest(_JOB)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    entries = {entry["path"]: entry for entry in payload["files"]}
    assert entries["requirement_items.json"]["storage_uri"].startswith("s3://bucket/")
    assert entries["input/requirements.md"]["object_key"] == f"{_JOB}/input/requirements.md"
    assert any(key == f"{_JOB}/requirement_items.json" for _, key in backend.uploads)


def test_object_storage_failure_blocks_artifact_completion(tmp_path: Path) -> None:
    """镜像失败不能把本地产物静默当作已完成。"""
    store = ArtifactStore(
        tmp_path, _SCHEMAS, "orchestrator", object_storage=_FakeObjectStorage(fail=True)
    )
    with pytest.raises(PipelineError, match="对象存储镜像失败"):
        store.write(_JOB, "requirement_items.json", _VALID_REQUIREMENT_ITEMS)
    # 本地文件即使暂时存在，也不能绕过对象存储完成判据。
    assert store.is_node_completed(_JOB, "requirement_items.json") is False


def test_object_storage_restores_missing_artifact(tmp_path: Path) -> None:
    backend = _FakeObjectStorage()
    store = ArtifactStore(tmp_path, _SCHEMAS, "orchestrator", object_storage=backend)
    store.write(_JOB, "requirement_items.json", _VALID_REQUIREMENT_ITEMS)
    local_path = store.job_dir(_JOB) / "requirement_items.json"
    local_path.unlink()

    payload = store.read_json(_JOB, "requirement_items.json")
    assert payload["items"][0]["id"] == "REQ-001"
    assert local_path.is_file()
    assert store.is_node_completed(_JOB, "requirement_items.json") is True


def test_object_storage_restore_rejects_checksum_mismatch(tmp_path: Path) -> None:
    """对象恢复必须匹配数据库登记的 SHA-256，不能只靠 JSON Schema。"""
    backend = _FakeObjectStorage()
    database = Database(f"sqlite:///{tmp_path / 'meta.db'}")
    database.create_schema()
    database.ensure_job(_JOB)
    store = ArtifactStore(
        tmp_path, _SCHEMAS, "orchestrator", object_storage=backend, database=database
    )
    store.write(_JOB, "requirement_items.json", _VALID_REQUIREMENT_ITEMS)
    local_path = store.job_dir(_JOB) / "requirement_items.json"
    local_path.unlink()
    backend.objects[f"{_JOB}/requirement_items.json"] = b'{"schema_version":"1.0"}'

    with pytest.raises(PipelineError, match="SHA-256 不匹配"):
        store.read_json(_JOB, "requirement_items.json")
    assert not local_path.exists()


def test_database_artifact_index_versions_every_write(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'meta.db'}")
    database.create_schema()
    database.ensure_job(_JOB)
    store = ArtifactStore(tmp_path, _SCHEMAS, "orchestrator", database=database)
    store.write(_JOB, "requirement_items.json", _VALID_REQUIREMENT_ITEMS)
    store.write(_JOB, "requirement_items.json", _VALID_REQUIREMENT_ITEMS)
    records = [
        record
        for record in database.list_artifacts(_JOB)
        if record.filename == "requirement_items.json"
    ]
    assert [record.version for record in records] == [2, 1]
    assert all(record.is_valid for record in records)
