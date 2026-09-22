"""P1-06 产物生命周期：过期任务先回收、宽限期后再清除。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orchestrator.artifact_lifecycle import (
    find_expired_jobs,
    purge_quarantine,
    quarantine_jobs,
)


def _write_state(root: Path, job_id: str, updated_at: datetime, status: str = "completed") -> Path:
    job_dir = root / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "state.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "updated_at": updated_at.isoformat(),
                "nodes": [{"name": "req_parse", "status": status}],
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "test_report.md").write_text("evidence", encoding="utf-8")
    return job_dir


def test_expired_jobs_skip_running_and_active_tasks(tmp_path: Path) -> None:
    now = datetime(2026, 9, 21, tzinfo=UTC)
    old = now - timedelta(days=30)
    _write_state(tmp_path, "job-old-1", old)
    _write_state(tmp_path, "job-running-1", old, status="running")
    _write_state(tmp_path, "job-active-1", old)

    candidates = find_expired_jobs(
        tmp_path,
        retention_days=7,
        active_job_ids={"job-active-1"},
        now=now,
    )

    assert [candidate.job_id for candidate in candidates] == ["job-old-1"]
    assert find_expired_jobs(tmp_path, retention_days=0, now=now) == []


def test_quarantine_is_recoverable_before_grace_period(tmp_path: Path) -> None:
    now = datetime(2026, 9, 21, tzinfo=UTC)
    job_dir = _write_state(tmp_path, "job-old-2", now - timedelta(days=30))
    candidates = find_expired_jobs(tmp_path, retention_days=7, now=now)
    assert quarantine_jobs(tmp_path, candidates, now=now) == ["job-old-2"]
    assert not job_dir.exists()
    assert (tmp_path / ".trash").is_dir()
    assert purge_quarantine(tmp_path, grace_days=7, now=now) == []

    batch = next((tmp_path / ".trash").iterdir())
    old_time = (now - timedelta(days=8)).timestamp()
    os.utime(batch, (old_time, old_time))
    assert purge_quarantine(tmp_path, grace_days=7, now=now) == [batch.name]
    assert not (tmp_path / ".trash").exists()
