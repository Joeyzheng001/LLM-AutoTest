from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.persistence import Database
from orchestrator.repository_resolver import RepositoryResolutionError, resolve_repository_inputs

_GIT_BINARY = "git"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - tests invoke a fixed local git executable
        [_GIT_BINARY, *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def test_remote_repository_is_cached_at_requested_commit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "test")
    (source / "README.md").write_text("v1\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "v1")
    commit = subprocess.run(  # noqa: S603 - tests invoke a fixed local git executable
        [_GIT_BINARY, "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
    ).stdout.strip()
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(source), str(remote))

    config = [
        {
            "name": "demo",
            "source": "git",
            "remote_url": remote.as_uri(),
            "branch": "main",
            "commit": commit,
            "language": "python",
        }
    ]
    first = resolve_repository_inputs(config, cache_root=tmp_path / "artifacts")
    second = resolve_repository_inputs(config, cache_root=tmp_path / "artifacts")

    assert first[0].path == second[0].path
    assert Path(first[0].path, "README.md").read_text(encoding="utf-8") == "v1\n"
    assert first[0].branch == "main"
    assert first[0].language == "python"


def test_repository_snapshot_is_registered_without_remote_url_or_token(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "test")
    (source / "README.md").write_text("v1\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "v1")
    db = Database(f"sqlite:///{tmp_path / 'state.db'}")
    db.create_schema()
    db.ensure_job("job-repo-snapshot")

    resolve_repository_inputs(
        [{"name": "demo", "source": "local", "path": str(source)}],
        cache_root=tmp_path / "artifacts",
        database=db,
        job_id="job-repo-snapshot",
    )

    snapshots = db.list_repo_snapshots("job-repo-snapshot")
    assert len(snapshots) == 1
    assert snapshots[0].source_type == "local"
    assert snapshots[0].target_sha
    assert snapshots[0].source_url_hash is None


def test_local_repository_requires_absolute_existing_path(tmp_path: Path) -> None:
    with pytest.raises(RepositoryResolutionError, match="本地路径"):
        resolve_repository_inputs(
            [{"name": "missing", "source": "local", "path": "relative/repo"}],
            cache_root=tmp_path,
        )
