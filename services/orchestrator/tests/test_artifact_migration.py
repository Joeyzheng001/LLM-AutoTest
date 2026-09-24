"""历史任务产物索引迁移回归。"""

from __future__ import annotations

import json
from pathlib import Path

from common.artifacts import ArtifactStore

from orchestrator.artifact_migration import migrate_artifact_index
from orchestrator.persistence import Database

_SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"
_JOB = "job-migrate-0001"


def test_migrate_artifact_index_is_idempotent(tmp_path: Path) -> None:
    job_dir = tmp_path / _JOB
    job_dir.mkdir()
    (job_dir / "state.json").write_text(
        json.dumps({"job_id": _JOB, "nodes": [], "updated_at": "2026-09-21T00:00:00+00:00"}),
        encoding="utf-8",
    )
    store = ArtifactStore(tmp_path, _SCHEMAS, "orchestrator")
    payload = {
        "schema_version": "1.0",
        "job_id": _JOB,
        "items": [{"id": "REQ-001", "title": "查询", "description": "查询用户"}],
    }
    store.write(_JOB, "requirement_items.json", payload)
    (job_dir / "job_input.json").write_text('{"password":"must-not-index"}', encoding="utf-8")
    (job_dir / "input").mkdir()
    (job_dir / "input" / "requirements.md").write_text("# req", encoding="utf-8")

    database = Database(f"sqlite:///{tmp_path / 'meta.db'}")
    database.create_schema()
    first = migrate_artifact_index(tmp_path, _SCHEMAS, database)
    second = migrate_artifact_index(tmp_path, _SCHEMAS, database)

    assert first.jobs == 1
    assert first.files == 2
    assert second.files == 2
    records = database.list_artifacts(_JOB)
    assert {record.filename for record in records} == {
        "requirement_items.json",
        "input/requirements.md",
    }
    assert "password" not in {record.filename for record in records}
    assert len(records) == 2

