"""Read current and archived report rounds without exposing archive paths."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_STAMP = re.compile(r"^\d{8}T\d{12}Z$")
_REPORT_ARTIFACTS = frozenset({
    "execution.json", "test_report.md", "testcases.json", "interfaces.json",
})
_ARTIFACT_NODES = {
    "execution.json": "auto_execute",
    "test_report.md": "test_report",
    "testcases.json": "testcase_generate",
    "interfaces.json": "interface_reconstruct",
}


def list_report_runs(job_dir: Path) -> list[dict[str, Any]]:
    """List completed execution artifacts, including legacy node archives."""
    runs: list[dict[str, Any]] = []
    current = job_dir / "execution.json"
    if current.is_file():
        item = _run_entry(current, "current", job_dir / "test_report.md")
        if item:
            runs.append(item)
    stamps = _archive_stamps(job_dir)
    for stamp in sorted(stamps, reverse=True):
        path = _artifact_at_or_after(job_dir, "auto_execute", "execution.json", stamp)
        if path is None:
            continue
        report = job_dir / "archive" / "test_report" / f"{stamp}_test_report.md"
        item = _run_entry(path, stamp, report)
        if item:
            runs.append(item)
    return runs


def resolve_report_artifact(job_dir: Path, run_id: str, name: str) -> Path | None:
    """Resolve four report artifacts for a known current or archived round."""
    if name not in _REPORT_ARTIFACTS:
        return None
    if run_id == "current":
        path = job_dir / name
        return path if path.is_file() else None
    if not _STAMP.fullmatch(run_id):
        return None
    if run_id not in _archive_stamps(job_dir):
        return None
    node = _ARTIFACT_NODES[name]
    if name == "test_report.md":
        path = job_dir / "archive" / node / f"{run_id}_{name}"
        return path if path.is_file() else None
    return _artifact_at_or_after(job_dir, node, name, run_id)


def _archive_stamps(job_dir: Path) -> set[str]:
    stamps: set[str] = set()
    for node, name in (("auto_execute", "execution.json"),
                       ("test_report", "test_report.md")):
        for path in (job_dir / "archive" / node).glob(f"*_{name}"):
            stamp = path.name.removesuffix(f"_{name}")
            if _STAMP.fullmatch(stamp):
                stamps.add(stamp)
    return stamps


def _artifact_at_or_after(job_dir: Path, node: str, name: str, stamp: str) -> Path | None:
    # A rerun from a later node keeps earlier artifacts in place. The first
    # later archive of such an artifact is the version this round used.
    archive_dir = job_dir / "archive" / node
    archived = sorted(archive_dir.glob(f"*_{name}"))
    for path in archived:
        archived_stamp = path.name.removesuffix(f"_{name}")
        if _STAMP.fullmatch(archived_stamp) and archived_stamp >= stamp:
            return path
    current = job_dir / name
    return current if current.is_file() else None


def _run_entry(execution: Path, run_id: str, report: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(execution.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("summary"), dict):
        return None
    timestamps = [str(row.get("timestamp")) for row in payload.get("results") or []
                  if isinstance(row, dict) and row.get("timestamp")]
    archived_at = None
    if run_id != "current":
        try:
            archived_at = datetime.strptime(run_id, "%Y%m%dT%H%M%S%fZ").replace(
                tzinfo=timezone.utc
            ).isoformat()
        except ValueError:
            return None
    return {
        "id": run_id,
        "is_current": run_id == "current",
        "executed_at": max(timestamps) if timestamps else None,
        "archived_at": archived_at,
        "has_report": report.is_file(),
        "summary": {key: payload["summary"].get(key, 0) for key in (
            "total", "passed", "failed_assertion", "failed_environment",
            "observed_unjudged", "not_executed",
        )},
    }
