"""产物管理器：job 目录、排他写权、Schema 校验、节点完成判据（docs/12 第一/二章）。

- 目录布局固定：`<root>/<job_id>/{9 产物 + state.json + scripts/ fixtures/ logs/ archive/}`；
- 产物文件名 = schemas/ 下的 Schema 文件名，一一对应，禁止自造别名；
- 写权排他：每个产物只允许一个服务写入（orchestrator / test-executor 二分），
  物理隔离不同容器的写冲突；
- 「节点已完成」唯一判据：产物存在且通过 Schema 校验（test_report.md 仅查存在性）。
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from orchestrator.errors import X_ORCH_01, PipelineError

# docs/12 §二产物清单与写方（service 级）：甲/乙产出的数据经 orchestrator 落盘，
# 丙负责的执行侧产物由 test-executor 直接写入产物卷。
ARTIFACT_FILES: tuple[str, ...] = (
    "requirement_items.json",
    "code_inventory.json",
    "interface_candidates.json",
    "interfaces.json",
    "review_mapping.json",
    "testcases.json",
    "execution.json",
    "defects.json",
    "regression_index.json",
    "metrics.json",
)
_MD_ARTIFACTS: tuple[str, ...] = ("test_report.md",)

_WRITE_OWNERS: dict[str, str] = {
    "requirement_items.json": "orchestrator",
    "code_inventory.json": "orchestrator",
    "interface_candidates.json": "orchestrator",
    "interfaces.json": "orchestrator",
    "review_mapping.json": "orchestrator",
    "testcases.json": "test-executor",
    "execution.json": "test-executor",
    "defects.json": "test-executor",
    "regression_index.json": "test-executor",
    "metrics.json": "orchestrator",
    "test_report.md": "orchestrator",
}

_SUBDIRS: tuple[str, ...] = ("scripts", "fixtures", "logs", "archive")


class ArtifactStore:
    """产物卷访问器（每个服务实例持有自己的 service 名）。"""

    def __init__(self, root: Path, schemas_dir: Path, service: str) -> None:
        """root 为产物卷根；service 用于写权校验（orchestrator / test-executor）。"""
        self._root = root
        self._schemas_dir = schemas_dir
        self._service = service

    def create_job(self, job_id: str) -> Path:
        """创建 job 目录（幂等：已存在时仅补齐子目录）。"""
        job_dir = self.job_dir(job_id)
        for sub in _SUBDIRS:
            (job_dir / sub).mkdir(parents=True, exist_ok=True)
        return job_dir

    def job_dir(self, job_id: str) -> Path:
        """job 目录路径（不保证存在）。"""
        return self._root / job_id

    def write(self, job_id: str, filename: str, payload: dict[str, Any]) -> Path:
        """写入产物：先过 Schema 校验，再原子落盘（tmp + replace）。

        校验失败即拒绝写入（诚实红线：不落不合规产物）。
        Raises:
            PipelineError: X-ORCH-01——payload 不符对应 Schema；或写权不属于本服务。
        """
        self._check_writable(filename)
        errors = self.validate(job_id, filename, payload)
        if errors:
            raise PipelineError(
                code=X_ORCH_01,
                message=f"产物校验失败：{filename}",
                node_id=None,
                details={"errors": errors[:10]},
            )
        target = self.job_dir(job_id) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
        return target

    def write_raw(self, job_id: str, relative_path: str, text: str) -> Path:
        """写入非 Schema 产物（scripts/ 下脚本等）：原子落盘 + 路径限制。

        仅允许 scripts/ 前缀（防止写到产物区或目录逃逸）；调用方服务须为
        test-executor（docs/12 产物布局：scripts/ 丙写，orchestrator 只读）。

        Raises:
            PipelineError: X-ORCH-01——路径不合规或写权不属于本服务。
        """
        if self._service != "test-executor":
            raise PipelineError(
                code=X_ORCH_01,
                message=f"write_raw 写权属于 test-executor（当前 {self._service}）",
            )
        rel = Path(relative_path)
        if rel.is_absolute() or ".." in rel.parts or rel.parts[0] != "scripts":
            raise PipelineError(
                code=X_ORCH_01,
                message=f"write_raw 仅允许 scripts/ 下相对路径：{relative_path}",
            )
        target = self.job_dir(job_id).joinpath(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
        return target

    def validate(self, job_id: str, filename: str, payload: dict[str, Any]) -> list[str]:
        """按 schemas/<同名>.json 校验 payload；md 产物与未知文件名另行处理。"""
        _ = job_id  # 路径无关：校验只取决于文件名对应的 Schema
        if filename in _MD_ARTIFACTS:
            return []  # 报告为 Markdown，无 JSON Schema（存在性由 is_node_completed 判定）
        if filename not in ARTIFACT_FILES:
            return [f"未知产物文件名：{filename}（不在 docs/12 §二 清单内）"]
        schema_path = self._schemas_dir / filename
        if not schema_path.is_file():
            return [f"Schema 文件缺失：{schema_path}"]
        schema: dict[str, Any] = json.loads(schema_path.read_text(encoding="utf-8"))
        return [
            f"{'/'.join(map(str, error.path)) or '<root>'}: {error.message}"
            for error in Draft202012Validator(schema).iter_errors(payload)
        ]

    def read_json(self, job_id: str, filename: str) -> dict[str, Any]:
        """读取产物 JSON（只读操作，任何服务可调用）。"""
        path = self.job_dir(job_id) / filename
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return payload

    def is_node_completed(self, job_id: str, filename: str) -> bool:
        """节点完成判据：产物存在且过校验（test_report.md 仅存在性）。"""
        path = self.job_dir(job_id) / filename
        if not path.is_file():
            return False
        if filename in _MD_ARTIFACTS:
            return path.stat().st_size > 0
        try:
            payload = self.read_json(job_id, filename)
        except (json.JSONDecodeError, OSError):
            return False
        return not self.validate(job_id, filename, payload)

    def archive(self, job_id: str, filename: str) -> Path | None:
        """--force-node 重跑前归档旧产物（docs/12 §1.2：禁止原地覆盖）。"""
        source = self.job_dir(job_id) / filename
        if not source.is_file():
            return None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.job_dir(job_id) / "archive" / f"{stamp}_{filename}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        return target

    def _check_writable(self, filename: str) -> None:
        """写权排他校验：不属于本服务的产物禁止写入。"""
        owner = _WRITE_OWNERS.get(filename)
        if owner is None:
            raise PipelineError(
                code=X_ORCH_01,
                message=f"未知产物文件名：{filename}（不在 docs/12 §二 清单内）",
            )
        if owner != self._service:
            raise PipelineError(
                code=X_ORCH_01,
                message=f"写权冲突：{filename} 的写方是 {owner}，当前服务 {self._service} 无权写入",
            )
