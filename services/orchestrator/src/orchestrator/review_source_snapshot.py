"""Keep Code Review source evidence pinned to the scan's Git revision."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from common.errors import X_ORCH_01, PipelineError

_REVISION = re.compile(r"^[0-9a-fA-F]{40,64}$")
_SOURCE_SUFFIXES = frozenset({
    ".py", ".java", ".kt", ".js", ".ts", ".yml", ".yaml", ".properties", ".xml",
})


def pinned_review_source(job_dir: Path, repo: dict[str, object]) -> Path:
    """Materialize reviewable tracked files from target_sha inside the job.

    Java parser snapshots are shared and pruned when another revision is scanned.
    Reading that path later can silently erase every Code Review citation. Git
    blobs provide the same committed bytes without relying on that lifecycle.
    """
    source = Path(str(repo.get("path") or ""))
    revision = str(repo.get("target_sha") or "")
    name = str(repo.get("name") or "")
    if not source.is_dir() or not _REVISION.fullmatch(revision):
        raise _snapshot_error(name, "源码仓库或 target_sha 不可用")
    source = source.resolve()
    key = hashlib.sha256(f"{source}\0{revision}".encode()).hexdigest()[:20]
    parent = job_dir / ".review-sources"
    destination = parent / key
    marker = destination / ".target-sha"
    if marker.is_file() and marker.read_text(encoding="ascii") == revision:
        return destination
    try:
        commit = _git(source, "rev-parse", "--verify", f"{revision}^{{commit}}")
        if commit.decode().strip().lower() != revision.lower():
            raise _snapshot_error(name, "target_sha 与本地 Git 提交不一致")
        tree = _git(source, "ls-tree", "-rz", "--full-tree", revision)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
        raise _snapshot_error(name, "扫描时的 Git 提交已无法读取，请重新扫描源码") from exc
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".review-source-", dir=parent))
    count = 0
    try:
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            header, raw_path = entry.split(b"\t", 1)
            mode, kind, object_id = header.split()
            if kind != b"blob" or mode not in {b"100644", b"100755"}:
                continue
            relative = PurePosixPath(os.fsdecode(raw_path))
            if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() not in _SOURCE_SUFFIXES:
                continue
            target = stage.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_git(source, "cat-file", "blob", object_id.decode("ascii")))
            count += 1
        if not count:
            raise _snapshot_error(name, "目标提交没有可审查的源码文件")
        (stage / ".target-sha").write_text(revision, encoding="ascii")
        if destination.exists():
            shutil.rmtree(destination)
        stage.replace(destination)
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise _snapshot_error(name, "目标提交源码提取失败，请重新扫描源码") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return destination


def _git(source: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(source), *args],
        check=True, capture_output=True, timeout=120,
    ).stdout


def _snapshot_error(name: str, detail: str) -> PipelineError:
    return PipelineError(
        code=X_ORCH_01,
        message=f"仓库 {name} 的 Code Review 源码快照不可用：{detail}",
        node_id="code_review",
    )
