"""历史任务产物索引迁移（P1-06）。

只登记现有文件并可选镜像，不删除、不改写业务产物。重复执行按 SHA 与 storage URI
幂等，损坏或不符合任务目录约定的文件进入报告而不阻断其他 job。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.artifacts import ARTIFACT_FILES, JOB_ID_RE, ArtifactStore
from orchestrator.persistence import Database


@dataclass
class MigrationReport:
    """一次历史索引迁移的统计。"""

    jobs: int = 0
    files: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _is_evidence_file(relative: Path) -> bool:
    """判断文件是否属于用户证据/执行留痕，排除状态和敏感配置。"""
    if relative.name in {
        "state.json",
        "job_input.json",
        ".job-credentials.enc",
        "artifact_manifest.json",
    }:
        return False
    if relative.name in ARTIFACT_FILES or relative.name == "test_report.md":
        return True
    return bool(
        relative.parts
        and relative.parts[0] in {"input", "scripts", "fixtures", "logs", "archive"}
    )


def migrate_artifact_index(
    root: Path,
    schemas_dir: Path,
    database: Database,
    *,
    object_storage: Any | None = None,
    job_id: str | None = None,
) -> MigrationReport:
    """扫描历史任务并登记/镜像证据文件；默认处理全部合法 job。"""
    report = MigrationReport()
    if not root.is_dir():
        return report
    store = ArtifactStore(
        root,
        schemas_dir,
        "orchestrator",
        object_storage=object_storage,
        database=database,
        deduplicate_artifact_index=True,
    )
    for job_dir in sorted(root.iterdir()):
        if (
            not job_dir.is_dir()
            or job_dir.name == ".trash"
            or not JOB_ID_RE.fullmatch(job_dir.name)
            or (job_id is not None and job_dir.name != job_id)
        ):
            continue
        state_path = job_dir / "state.json"
        if not state_path.is_file():
            report.skipped += 1
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError("state.json 顶层不是对象")
            database.ensure_job(job_dir.name)
            report.jobs += 1
        except (OSError, ValueError, TypeError) as exc:
            report.errors.append(f"{job_dir.name}: state.json 无法读取（{type(exc).__name__}）")
            continue
        _ = state  # 状态本身保留在兼容视图；本步骤只迁移产物索引。
        for path in sorted(job_dir.rglob("*")):
            if not path.is_file():
                continue
            try:
                relative = path.relative_to(job_dir)
            except ValueError:
                report.skipped += 1
                continue
            if not _is_evidence_file(relative):
                report.skipped += 1
                continue
            try:
                store.mirror_file(job_dir.name, relative.as_posix())
                report.files += 1
            except Exception as exc:  # 单个文件失败不阻断其他历史 job
                report.errors.append(
                    f"{job_dir.name}/{relative.as_posix()}: 迁移失败（{type(exc).__name__}）"
                )
    return report
