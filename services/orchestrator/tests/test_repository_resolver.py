from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.persistence import Database
from orchestrator.repository_resolver import (
    RepositoryResolutionError,
    _classify_name_status,
    preview_branch_diff,
    resolve_repository_inputs,
)

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


def test_classify_name_status_groups_git_diff_output() -> None:
    stdout = "\n".join(
        [
            "A\tnew/CreateUser.java",
            "C\tsrc/Copied.java",
            "M\tsrc/UserService.java",
            "T\tsrc/TypeChanged.java",
            "D\tsrc/Deleted.java",
            "R100\told/Old.java\tnew/Renamed.java",
            "X\tunknown/Kind.java",
        ]
    )
    added, modified, removed = _classify_name_status(stdout)

    assert added == ["new/CreateUser.java", "src/Copied.java", "new/Renamed.java"]
    assert modified == ["src/UserService.java", "src/TypeChanged.java", "unknown/Kind.java"]
    assert removed == ["src/Deleted.java", "old/Old.java"]


def test_preview_branch_diff_returns_changed_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "keep.txt").write_text("base\n", encoding="utf-8")
    (repo / "del.txt").write_text("del\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")

    _git(repo, "checkout", "-b", "feature")
    (repo / "keep.txt").write_text("changed\n", encoding="utf-8")
    (repo / "add.txt").write_text("new\n", encoding="utf-8")
    (repo / "del.txt").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feature changes")

    result = preview_branch_diff(
        {
            "name": "demo",
            "source": "local",
            "path": str(repo),
            "branch": "feature",
            "baseline_branch": "main",
        },
        cache_root=tmp_path / "artifacts",
    )

    assert "error" not in result
    assert result["baseline"] == "main"
    assert result["target"] == "feature"
    assert result["added"] == ["add.txt"]
    assert result["modified"] == ["keep.txt"]
    assert result["removed"] == ["del.txt"]


def test_preview_branch_diff_requires_baseline(tmp_path: Path) -> None:
    result = preview_branch_diff(
        {"name": "demo", "source": "local", "path": str(tmp_path), "branch": "main"},
        cache_root=tmp_path,
    )

    assert result["error"] == "未指定基线分支，无法对比差异"
    assert result["added"] == []
    assert result["modified"] == []
    assert result["removed"] == []
