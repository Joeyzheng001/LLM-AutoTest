"""任务产物生命周期管理（P1-06 第一阶段）。

不改变现有 job 目录和产物 Schema：过期任务先移动到同一产物根目录下的
`.trash/` 回收区，宽限期后才物理删除。默认 retention_days=0，未显式配置
时不会执行任何清理；运行中或队列中的任务始终跳过。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from common.artifacts import JOB_ID_RE


@dataclass(frozen=True)
class CleanupCandidate:
    """一个可以进入回收区的任务目录。"""

    job_id: str
    path: Path
    updated_at: datetime
    size_bytes: int


def _parse_timestamp(value: Any, fallback: datetime) -> datetime:
    """读取 state.json 时间；历史损坏值回退到目录修改时间。"""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return fallback


def _directory_size(path: Path) -> int:
    """统计目录大小；单个文件不可读时跳过，不阻断其他任务清理。"""
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def _is_running(state: dict[str, Any], active_job_ids: set[str]) -> bool:
    """以队列活跃集合和 state 节点双重判断运行中，宁可少清不误删。"""
    if str(state.get("job_id", "")) in active_job_ids:
        return True
    nodes = state.get("nodes")
    return isinstance(nodes, list) and any(
        isinstance(node, dict) and node.get("status") == "running" for node in nodes
    )


def find_expired_jobs(
    root: Path,
    *,
    retention_days: int,
    active_job_ids: set[str] | None = None,
    now: datetime | None = None,
    limit: int = 100,
) -> list[CleanupCandidate]:
    """列出过期且不在运行中的任务，不做任何删除或移动。"""
    if retention_days <= 0 or limit <= 0:
        return []
    current = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = current - timedelta(days=retention_days)
    active = active_job_ids or set()
    candidates: list[CleanupCandidate] = []
    if not root.is_dir():
        return candidates
    for job_dir in sorted(root.iterdir()):
        if (
            not job_dir.is_dir()
            or job_dir.name == ".trash"
            or not JOB_ID_RE.fullmatch(job_dir.name)
        ):
            continue
        state_path = job_dir / "state.json"
        if not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            modified = datetime.fromtimestamp(state_path.stat().st_mtime, tz=UTC)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(state, dict) or _is_running(state, active):
            continue
        updated_at = _parse_timestamp(state.get("updated_at"), modified)
        if updated_at >= cutoff:
            continue
        candidates.append(
            CleanupCandidate(
                job_id=job_dir.name,
                path=job_dir,
                updated_at=updated_at,
                size_bytes=_directory_size(job_dir),
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


def quarantine_jobs(
    root: Path,
    candidates: list[CleanupCandidate],
    *,
    active_job_ids: set[str] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """把候选任务原子移动到回收区，返回实际移动的 job_id。"""
    if not candidates:
        return []
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    trash_root = root / ".trash" / stamp
    active = active_job_ids or set()
    moved: list[str] = []
    for candidate in candidates:
        if not candidate.path.is_dir():
            continue
        if candidate.job_id in active:
            continue
        # 扫描和移动之间可能正好开始了新任务；再次读 state，宁可少清不误删。
        try:
            state = json.loads((candidate.path / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(state, dict) and _is_running(state, active):
            continue
        target = trash_root / candidate.job_id
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(candidate.path), str(target))
        except OSError:
            continue
        moved.append(candidate.job_id)
    return moved


def purge_quarantine(
    root: Path,
    *,
    grace_days: int,
    now: datetime | None = None,
    limit: int = 100,
) -> list[str]:
    """清理超过宽限期的回收批次；宽限期 <=0 时保持禁用。"""
    if grace_days <= 0 or limit <= 0:
        return []
    trash_root = root / ".trash"
    if not trash_root.is_dir():
        return []
    cutoff = (now or datetime.now(UTC)).astimezone(UTC) - timedelta(days=grace_days)
    removed: list[str] = []
    for batch in sorted(trash_root.iterdir()):
        if not batch.is_dir():
            continue
        try:
            modified = datetime.fromtimestamp(batch.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if modified >= cutoff:
            continue
        try:
            shutil.rmtree(batch)
        except OSError:
            continue
        removed.append(batch.name)
        if len(removed) >= limit:
            break
    try:
        if trash_root.is_dir() and not any(trash_root.iterdir()):
            trash_root.rmdir()
    except OSError:
        pass
    return removed
