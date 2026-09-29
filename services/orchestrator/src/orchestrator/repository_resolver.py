"""将任务中的本地/远程仓库配置解析为源码扫描输入。

远程仓库在流水线开始前检出到产物卷之外的专用缓存目录，扫描器只接收
检出后的本地路径。访问令牌通过 ``GIT_CONFIG_*`` 环境变量传给 git，避免
把凭据拼进 URL、命令行参数或日志；缓存键包含远程地址和目标版本，防止
不同任务或不同提交之间串用源码。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from common.errors import PipelineError

from orchestrator.javaparser_client import RepositoryInput

_GIT_TIMEOUT_S = 300
_LOCK_TIMEOUT_S = 120
_LOCK_POLL_S = 0.1
_GIT_BINARY = shutil.which("git") or "git"
_REMOTE_SCHEMES = ("http://", "https://", "ssh://", "git://", "file://")


class RepositoryResolutionError(PipelineError):
    """仓库无法准备，流水线不应继续使用不确定的源码版本。"""

    def __init__(self, message: str) -> None:
        super().__init__(code="X-CODE-04", message=message)


def resolve_repository_inputs(
    repositories: list[dict[str, Any]],
    *,
    cache_root: Path,
    git_tokens: dict[str, str] | None = None,
    database: Any | None = None,
    job_id: str | None = None,
) -> list[RepositoryInput]:
    """解析仓库配置。

    ``source=local`` 兼容原有绝对路径模式；``source=git`` 支持
    ``remote_url + branch/commit``，优先使用 commit，便于重跑得到同一源码。
    ``git_tokens`` 的键可为仓库名或 remote_url，值永不写入返回值。
    """
    resolved: list[RepositoryInput] = []
    token_map = git_tokens or {}
    for raw in repositories:
        if not isinstance(raw, dict):
            raise RepositoryResolutionError("仓库配置结构不合法")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise RepositoryResolutionError("仓库名称不能为空")
        source = str(raw.get("source") or "local").strip().lower()
        branch = str(raw.get("branch") or "main").strip() or "main"
        baseline = str(raw.get("baseline_branch") or "").strip() or None
        commits = _parse_commits(raw)
        include_paths = tuple(str(item) for item in raw.get("include_paths", ()))
        language = str(raw.get("language") or "java")
        service_base_url = str(raw.get("service_base_url") or "").strip() or None
        if source == "local":
            path = Path(str(raw.get("path") or ""))
            if not path.is_absolute() or not path.is_dir():
                raise RepositoryResolutionError(
                    f"仓库 {name} 的本地路径不存在或不是绝对目录：{path}"
                )
            # 工作区 HEAD 同时用于两处：快照留痕，以及 java-parser 的幂等指纹
            # （见 RepositoryInput.workspace_revision）。只算一次——git 子进程不该为一个值跑两遍。
            workspace_revision = _git_revision(path, "HEAD")
            target_ref = commits[-1] if commits else branch
            declared_revision = _git_revision(path, target_ref)
            resolved_input = RepositoryInput(
                name=name,
                path=str(path),
                branch=branch,
                baseline_branch=baseline,
                is_fallback=bool(raw.get("is_fallback", False)),
                include_paths=include_paths,
                language=language,
                service_base_url=service_base_url,
                commits=commits,
                workspace_revision=workspace_revision,
                # 配置分支指向的提交：与工作区 HEAD 比对可发现「工作区不在配置分支上」。
                declared_revision=declared_revision,
                # Python AST 也必须读取配置目标版本的快照，不得直接读可变工作区。
                target_sha=declared_revision or workspace_revision,
            )
            resolved.append(resolved_input)
            _register_snapshot(
                database,
                job_id,
                name=name,
                source_type="local",
                source_url=None,
                target_ref=target_ref,
                baseline_ref=baseline,
                target_sha=declared_revision or workspace_revision,
                baseline_sha=_git_revision(path, baseline) if baseline else None,
                workspace_path=path,
                cache_hit=False,
            )
            continue
        if source != "git":
            raise RepositoryResolutionError(f"仓库 {name} 的 source 仅支持 local/git")
        remote_url = str(raw.get("remote_url") or "").strip()
        if not _is_supported_remote(remote_url):
            raise RepositoryResolutionError(
                f"仓库 {name} 的 remote_url 不是支持的 Git 地址：{remote_url}"
            )
        commit = str(raw.get("commit") or "").strip() or None
        access_token = token_map.get(name) or token_map.get(remote_url)
        checkout, cache_hit = _checkout_remote(
            name=name,
            remote_url=remote_url,
            branch=branch,
            commit=commits[-1] if commits else commit,
            cache_root=cache_root,
            access_token=access_token,
        )
        # 同上：检出目录的 HEAD 既进快照，也进 java-parser 幂等指纹。
        workspace_revision = _git_revision(checkout, "HEAD", access_token=access_token)
        target_ref = commits[-1] if commits else (commit or branch)
        declared_revision = (
            _git_revision(checkout, target_ref, access_token=access_token) or workspace_revision
        )
        resolved_input = RepositoryInput(
            name=name,
            path=str(checkout),
            branch=branch,
            baseline_branch=baseline,
            is_fallback=bool(raw.get("is_fallback", False)),
            include_paths=include_paths,
            language=language,
            service_base_url=service_base_url,
            commits=commits,
            workspace_revision=workspace_revision,
            declared_revision=declared_revision,
            target_sha=declared_revision,
        )
        resolved.append(resolved_input)
        _register_snapshot(
            database,
            job_id,
            name=name,
            source_type="git",
            source_url=remote_url,
            target_ref=target_ref,
            baseline_ref=baseline,
            target_sha=declared_revision,
            baseline_sha=(
                _git_revision(checkout, baseline, access_token=access_token)
                if baseline
                else None
            ),
            workspace_path=checkout,
            cache_hit=cache_hit,
        )
    return resolved


def _parse_commits(raw: dict[str, Any]) -> tuple[str, ...]:
    """解析提交号列表（C-09 扩展）：优先 ``commits``，兼容旧 ``commit`` 单值。"""
    values = raw.get("commits")
    if values is None:
        legacy = str(raw.get("commit") or "").strip()
        values = [legacy] if legacy else []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(str(value).strip() for value in values if str(value).strip())


def _is_supported_remote(remote_url: str) -> bool:
    """只接受明确的 Git URL，拒绝本地路径和 shell 片段。"""
    return bool(remote_url) and (
        remote_url.startswith(_REMOTE_SCHEMES) or remote_url.startswith("git@")
    ) and "\x00" not in remote_url


def _checkout_remote(
    *,
    name: str,
    remote_url: str,
    branch: str,
    commit: str | None,
    cache_root: Path,
    access_token: str | None,
) -> tuple[Path, bool]:
    """按内容寻址缓存检出指定版本，并返回工作区目录。"""
    version = commit or f"branch:{branch}"
    cache_key = hashlib.sha256(f"{remote_url}\0{version}".encode()).hexdigest()[:32]
    cache_dir = (cache_root / "repo-cache" / cache_key).resolve()
    root = cache_root.resolve()
    if root not in cache_dir.parents:
        raise RepositoryResolutionError("Git 缓存路径逸出产物根目录")
    checkout = cache_dir / "checkout"
    cache_hit = checkout.joinpath(".git").exists()
    lock = cache_dir.with_name(f"{cache_dir.name}.lock")
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    _acquire_lock(lock, name)
    try:
        if not (checkout / ".git").exists():
            if cache_dir.exists():
                shutil.rmtree(cache_dir)
            cache_dir.mkdir(parents=True, exist_ok=True)
            _run_git(
                ["clone", "--no-checkout", "--filter=blob:none", remote_url, str(checkout)],
                cwd=cache_dir,
                access_token=access_token,
            )
        else:
            _run_git(
                ["fetch", "--all", "--tags", "--prune"],
                cwd=checkout,
                access_token=access_token,
            )
        target = commit or branch
        _run_git(
            ["rev-parse", "--verify", f"{target}^{{commit}}"],
            cwd=checkout,
            access_token=access_token,
        )
        _run_git(["checkout", "--detach", target], cwd=checkout, access_token=access_token)
        _run_git(["reset", "--hard", target], cwd=checkout, access_token=access_token)
        _run_git(["clean", "-fdx"], cwd=checkout, access_token=access_token)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # 报错只暴露仓库名/版本，不把 git 的 URL 或环境变量带入 API 响应。
        raise RepositoryResolutionError(
            f"仓库 {name} 检出失败（版本 {version}），请检查地址、权限和提交号"
        ) from exc
    finally:
        _release_lock(lock)
    return checkout, cache_hit


def _git_revision(
    workspace: Path,
    ref: str,
    *,
    access_token: str | None = None,
) -> str | None:
    """读取源码工作区的固定提交号；本地非 Git 目录返回空值。"""
    if not (workspace / ".git").exists():
        return None
    try:
        result = _run_git(
            ["rev-parse", "--verify", f"{ref}^{{commit}}"],
            cwd=workspace,
            access_token=access_token,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value or None


def workspace_scope_warnings(repositories: list[RepositoryInput]) -> list[str]:
    """比对「配置分支指向的提交」与「工作区实际 HEAD」，不一致即告警（不阻断）。

    `source=local` 模式下系统**不 checkout**：配置里的 `branch` 只用于算变更清单，
    实际扫描读的是**工作区当前内容**。两者脱节时接口候选会静默扫错分支——实测踩过
    两次（11 个接口只剩 1 个），排查成本很高，所以在扫描产物里如实留痕。
    """
    warnings: list[str] = []
    for repo in repositories:
        declared = repo.declared_revision
        actual = repo.workspace_revision
        if not declared or not actual or declared == actual:
            continue
        warnings.append(
            f"仓库 {repo.name} 的工作区不在配置的分支上：配置 {repo.branch} 指向 "
            f"{declared[:8]}，工作区 HEAD 是 {actual[:8]}"
            "——source=local 不会自动 checkout，扫描读到的是工作区当前内容"
        )
    return warnings


def _register_snapshot(
    database: Any | None,
    job_id: str | None,
    *,
    name: str,
    source_type: str,
    source_url: str | None,
    target_ref: str | None,
    baseline_ref: str | None,
    target_sha: str | None,
    baseline_sha: str | None,
    workspace_path: Path,
    cache_hit: bool,
) -> None:
    """将解析后的实际来源登记到数据库；无数据库时保持文件模式兼容。"""
    if database is None or not job_id:
        return
    register = getattr(database, "register_repo_snapshot", None)
    if not callable(register):
        return
    source_url_hash = (
        hashlib.sha256(source_url.encode("utf-8")).hexdigest() if source_url else None
    )
    try:
        register(
            job_id,
            name,
            source_type=source_type,
            source_url_hash=source_url_hash,
            target_ref=target_ref,
            baseline_ref=baseline_ref,
            target_sha=target_sha,
            baseline_sha=baseline_sha,
            workspace_path=str(workspace_path),
            cache_hit=cache_hit,
        )
    except Exception as exc:
        raise RepositoryResolutionError(f"仓库 {name} 的源码快照登记失败") from exc


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    access_token: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """运行无 shell 的 git 命令；敏感头仅通过子进程环境注入。"""
    environment = os.environ.copy()
    if access_token:
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {access_token}",
            }
        )
    return subprocess.run(  # noqa: S603 - args are a fixed git subcommand list; shell=False
        [_GIT_BINARY, *args],
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
    )


def _acquire_lock(lock: Path, name: str) -> None:
    """用原子 mkdir 防止同一缓存被并发 clone；超时即明确失败。"""
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    while True:
        try:
            lock.mkdir()
            return
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RepositoryResolutionError(
                    f"仓库 {name} 的 Git 缓存正在被其他任务占用"
                ) from None
            time.sleep(_LOCK_POLL_S)
        except OSError as exc:
            raise RepositoryResolutionError(f"仓库 {name} 无法创建 Git 缓存锁") from exc


def _release_lock(lock: Path) -> None:
    try:
        lock.rmdir()
    except OSError:
        pass


def preview_branch_diff(
    repo: dict[str, Any],
    *,
    cache_root: Path,
    access_token: str | None = None,
) -> dict[str, Any]:
    """预览仓库差异文件清单（不创建 job、不写产物）。

    两种变更范围来源：``commits``（提交号 diff 并集，优先）或
    ``baseline_branch``（分支对比）。与 java-parser 扫描侧分类规则一致；
    remote 仓库复用 ``_checkout_remote`` 的版本化缓存；失败时返回带
    ``error`` 字段的结构而非抛异常，供配置页直接展示。
    """
    name = str(repo.get("name") or "").strip()
    source = str(repo.get("source") or "local").strip().lower()
    branch = str(repo.get("branch") or "main").strip() or "main"
    baseline = str(repo.get("baseline_branch") or "").strip() or None
    commits = _parse_commits(repo)

    def _error(message: str) -> dict[str, Any]:
        return {
            "name": name,
            "baseline": baseline,
            "target": branch,
            "commits": list(commits),
            "added": [],
            "modified": [],
            "removed": [],
            "error": message,
        }

    if not commits and not baseline:
        return _error("未指定基线分支或提交号，无法对比差异")

    if source == "local":
        workspace = Path(str(repo.get("path") or ""))
        if not workspace.is_absolute() or not workspace.is_dir():
            return _error("本地路径不存在或不是绝对目录")
    elif source == "git":
        remote_url = str(repo.get("remote_url") or "").strip()
        commit = commits[-1] if commits else (str(repo.get("commit") or "").strip() or None)
        if not _is_supported_remote(remote_url):
            return _error("remote_url 不是支持的 Git 地址")
        try:
            workspace, _ = _checkout_remote(
                name=name,
                remote_url=remote_url,
                branch=branch,
                commit=commit,
                cache_root=cache_root,
                access_token=access_token,
            )
        except RepositoryResolutionError as exc:
            return _error(exc.message)
    else:
        return _error("source 仅支持 local/git")

    if commits:
        added: list[str] = []
        modified: list[str] = []
        removed: list[str] = []
        for commit in commits:
            try:
                result = _run_git(
                    ["diff", "--name-status", f"{commit}^", commit],
                    cwd=workspace,
                    access_token=access_token,
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                # 根提交没有父提交：退化为 git show 该提交全量变更
                try:
                    result = _run_git(
                        ["show", "--name-status", "--format=", commit],
                        cwd=workspace,
                        access_token=access_token,
                    )
                except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    return _error(f"提交号比对失败（提交不存在）：{commit}")
            entry_added, entry_modified, entry_removed = _classify_name_status(result.stdout)
            for value in entry_added:
                if value not in added:
                    added.append(value)
            for value in entry_modified:
                if value not in modified:
                    modified.append(value)
            for value in entry_removed:
                if value not in removed:
                    removed.append(value)
        return {
            "name": name,
            "baseline": baseline,
            "target": branch,
            "commits": list(commits),
            "added": added,
            "modified": modified,
            "removed": removed,
        }

    try:
        result = _run_git(
            ["diff", "--name-status", f"{baseline}...{branch}"],
            cwd=workspace,
            access_token=access_token,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return _error(f"分支比对失败（非 Git 仓库或 ref 不存在）：{baseline}...{branch}")

    added, modified, removed = _classify_name_status(result.stdout)
    return {
        "name": name,
        "baseline": baseline,
        "target": branch,
        "added": added,
        "modified": modified,
        "removed": removed,
    }


def _classify_name_status(stdout: str) -> tuple[list[str], list[str], list[str]]:
    """把 ``git diff --name-status`` 输出拆成 added/modified/removed 三组。

    与 java-parser 的 diffBranches 规则一致：A/C→added；M/T→modified；
    D→removed；R→旧路径 removed + 新路径 added；未知状态码保守归 modified。
    """
    added: list[str] = []
    modified: list[str] = []
    removed: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        kind = parts[0][:1]
        if kind in ("A", "C"):
            added.append(parts[1])
        elif kind in ("M", "T"):
            modified.append(parts[1])
        elif kind == "D":
            removed.append(parts[1])
        elif kind == "R":
            removed.append(parts[1])
            if len(parts) >= 3:
                added.append(parts[2])
        else:
            modified.append(parts[1])
    return added, modified, removed
