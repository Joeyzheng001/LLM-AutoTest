"""产物管理器：job 目录、排他写权、Schema 校验、节点完成判据（docs/12 第一/二章）。

- 目录布局固定：`<root>/<job_id>/{9 产物 + state.json + scripts/ fixtures/ logs/ archive/}`；
- 产物文件名 = schemas/ 下的 Schema 文件名，一一对应，禁止自造别名；
- 写权排他：每个产物只允许一个服务写入（orchestrator / test-executor 二分），
  物理隔离不同容器的写冲突；
- 「节点已完成」唯一判据：产物存在且通过 Schema 校验（test_report.md 仅查存在性）。
"""

from __future__ import annotations

from common.e2e_contracts import evidence_path

import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from common.errors import X_ORCH_01, PipelineError
from common.object_storage import ObjectStorage, ObjectStorageError

# C-12 安全：job_id 完整正则（字符集不含路径分隔符与 ..；长度 ≤64+4）。
# 单点下沉在 job_dir——CLI/API/内部调用统一兜底，防目录穿越参与 rmtree/写入。
JOB_ID_RE = re.compile(r"^job-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# docs/12 §二产物清单与写方（service 级）：甲/乙产出的数据经 orchestrator 落盘，
# 丙负责的执行侧产物由 test-executor 直接写入产物卷。
ARTIFACT_FILES: tuple[str, ...] = (
    "requirement_diff.json",
    "requirement_items.json",
    "requirement_review.json",
    "code_inventory.json",
    "interface_candidates.json",
    "interfaces.json",
    "change_set.json",
    "review_mapping.json",
    "testcases.json",
    "e2e_design.json",
    "e2e_execution.json",
    "e2e_defects.json",
    "e2e_history.json",
    "e2e_regression.json",
    "execution.json",
    "defects.json",
    "regression_index.json",
    "metrics.json",
)
_MD_ARTIFACTS: tuple[str, ...] = ("test_report.md",)
_INTERNAL_FILES: frozenset[str] = frozenset({".job-credentials.enc", "artifact_manifest.json"})

_WRITE_OWNERS: dict[str, str] = {
    "requirement_diff.json": "orchestrator",
    "requirement_items.json": "orchestrator",
    "requirement_review.json": "orchestrator",
    "code_inventory.json": "orchestrator",
    "interface_candidates.json": "orchestrator",
    "interfaces.json": "orchestrator",
    "change_set.json": "orchestrator",
    "review_mapping.json": "orchestrator",
    "testcases.json": "test-executor",
    "e2e_design.json": "test-executor",
    "e2e_execution.json": "test-executor",
    "e2e_defects.json": "test-executor",
    "e2e_history.json": "test-executor",
    "e2e_regression.json": "test-executor",
    "execution.json": "test-executor",
    "defects.json": "test-executor",
    "regression_index.json": "test-executor",
    "metrics.json": "orchestrator",
    "test_report.md": "orchestrator",
}

_SUBDIRS: tuple[str, ...] = ("scripts", "fixtures", "logs", "archive")


def ensure_job_id(job_id: str) -> str:
    """校验 job_id（C-12）：不合规即拒（宁停不猜），返回原值。

    非法示例：`job-../../x`（目录穿越）、中文/空格/超长——均无法成为产物目录名。
    """
    if not JOB_ID_RE.match(job_id):
        raise PipelineError(
            code=X_ORCH_01,
            message=(
                f"job_id 非法（C-12 安全口径 ^job-[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}）：{job_id!r}；"
                "留空可自动生成 job-web-…"
            ),
        )
    return job_id


class ArtifactStore:
    """产物卷访问器（每个服务实例持有自己的 service 名）。"""

    def __init__(
        self,
        root: Path,
        schemas_dir: Path,
        service: str,
        object_storage: ObjectStorage | None = None,
        database: Any | None = None,
        deduplicate_artifact_index: bool = False,
    ) -> None:
        """root 为产物卷根；service 用于写权校验（orchestrator / test-executor）。"""
        self._root = root.resolve()
        self._schemas_dir = schemas_dir
        self._service = service
        self._object_storage = object_storage
        self._database = database
        self._deduplicate_artifact_index = deduplicate_artifact_index

    @property
    def database(self) -> Any | None:
        """返回可选数据库门面，供任务配置在共享卷不可见时回退读取。"""
        return self._database

    def create_job(self, job_id: str) -> Path:
        """创建 job 目录（幂等：已存在时仅补齐子目录）。"""
        job_dir = self.job_dir(job_id)
        for sub in _SUBDIRS:
            (job_dir / sub).mkdir(parents=True, exist_ok=True)
        return job_dir

    def job_dir(self, job_id: str) -> Path:
        """job 目录路径（不保证存在）；C-12：入口校验 + 根包含断言（防穿越兑底）。"""
        ensure_job_id(job_id)
        candidate = (self._root / job_id).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise PipelineError(
                code=X_ORCH_01,
                message=f"job 目录逸出产物卷根（C-12）：{candidate} 不在 {self._root} 内",
            )
        return candidate

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
        storage_uri = self._mirror_file(job_id, target)
        self._register_artifact(
            job_id, filename, target, storage_uri, payload.get("schema_version")
        )
        return target

    def write_raw(self, job_id: str, relative_path: str, text: str) -> Path:
        """写入非 Schema 产物：原子落盘 + 白名单路径限制。

        白名单（docs/12 产物布局）：test-executor 可写 scripts/ 下相对路径；
        orchestrator 可写 test_report.md（Markdown 报告，无 JSON Schema）。

        Raises:
            PipelineError: X-ORCH-01——路径不合规或写权不属于本服务。
        """
        rel = Path(relative_path)
        if self._service == "test-executor":
            if rel.is_absolute() or ".." in rel.parts or rel.parts[0] != "scripts":
                raise PipelineError(
                    code=X_ORCH_01,
                    message=f"write_raw 仅允许 scripts/ 下相对路径：{relative_path}",
                )
        elif self._service == "orchestrator" and relative_path == "test_report.md":
            pass  # Markdown 报告（甲写，存在性由 is_node_completed 判定）
        else:
            raise PipelineError(
                code=X_ORCH_01,
                message=(
                    f"write_raw 无 {self._service} 写权路径：{relative_path}"
                    "（test-executor→scripts/；orchestrator→test_report.md）"
                ),
            )
        target = self.job_dir(job_id).joinpath(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
        storage_uri = self._mirror_file(job_id, target)
        self._register_artifact(job_id, relative_path, target, storage_uri, None)
        return target

    def write_bytes(self, job_id: str, relative_path: str, content: bytes) -> Path:
        """Write binary execution evidence under the test-executor evidence directory."""
        relative = Path(relative_path)
        if (
            self._service != "test-executor"
            or relative.is_absolute()
            or not evidence_path(relative.as_posix())
        ):
            raise PipelineError(
                code=X_ORCH_01,
                message=f"write_bytes 仅允许 test-executor 写已登记的 E2E 执行证据：{relative_path}",
            )
        target = self.job_dir(job_id) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(target)
        storage_uri = self._mirror_file(job_id, target)
        self._register_artifact(job_id, relative.as_posix(), target, storage_uri, None)
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
        path = self.ensure_local(job_id, filename)
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return payload

    def ensure_local(self, job_id: str, relative_path: str) -> Path:
        """确保产物在本地可读；本地缺失时按对象键从镜像恢复。"""
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise PipelineError(code=X_ORCH_01, message=f"非法读取路径：{relative_path}")
        target = self.job_dir(job_id) / relative
        if target.is_file():
            return target
        if self._object_storage is None:
            raise FileNotFoundError(target)
        getter = getattr(self._object_storage, "get_file", None)
        if not callable(getter):
            raise FileNotFoundError(target)
        temporary = target.with_suffix(target.suffix + ".download")
        try:
            getter(f"{job_id}/{relative.as_posix()}", temporary)
            if not temporary.is_file():
                raise FileNotFoundError(target)
            expected_sha256: str | None = None
            if self._database is not None:
                latest_artifact = getattr(self._database, "latest_artifact", None)
                if callable(latest_artifact):
                    record = latest_artifact(job_id, relative.as_posix())
                    if record is not None:
                        expected_sha256 = str(getattr(record, "sha256", "") or "") or None
            if expected_sha256 is not None:
                actual_sha256 = hashlib.sha256(temporary.read_bytes()).hexdigest()
                if actual_sha256 != expected_sha256:
                    raise PipelineError(
                        code=X_ORCH_01,
                        message=(
                            f"对象存储恢复校验失败：{relative.as_posix()}（SHA-256 不匹配）"
                        ),
                    )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.replace(target)
        except ObjectStorageError:
            temporary.unlink(missing_ok=True)
            raise
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return target

    def is_node_completed(self, job_id: str, filename: str) -> bool:
        """节点完成判据：产物存在且过校验（test_report.md 仅存在性）。"""
        try:
            path = self.ensure_local(job_id, filename)
        except (FileNotFoundError, OSError, ObjectStorageError, PipelineError):
            return False
        if filename in _MD_ARTIFACTS:
            return path.stat().st_size > 0 and self._mirror_present(job_id, path)
        try:
            payload = self.read_json(job_id, filename)
        except (json.JSONDecodeError, OSError):
            return False
        return not self.validate(job_id, filename, payload) and self._mirror_present(job_id, path)

    def _mirror_present(self, job_id: str, path: Path) -> bool:
        """对象存储模式下用 HEAD 防止本地产物先落盘导致误判完成。"""
        if self._object_storage is None:
            return True
        exists = getattr(self._object_storage, "exists", None)
        if not callable(exists):
            # 兼容仅实现 put_file 的旧/测试后端；正式 S3 后端实现 exists。
            return True
        relative = path.relative_to(self.job_dir(job_id)).as_posix()
        try:
            return bool(exists(f"{job_id}/{relative}"))
        except (ObjectStorageError, OSError):
            return False

    def archive(self, job_id: str, filename: str) -> Path | None:
        """--force-node 重跑前归档旧产物（docs/12 §1.2：禁止原地覆盖）。"""
        source = self.job_dir(job_id) / filename
        if not source.is_file():
            return None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.job_dir(job_id) / "archive" / f"{stamp}_{filename}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        storage_uri = self._mirror_file(job_id, target)
        self._register_artifact(
            job_id,
            target.relative_to(self.job_dir(job_id)).as_posix(),
            target,
            storage_uri,
            None,
        )
        return target

    def mirror_file(self, job_id: str, relative_path: str) -> str | None:
        """镜像任务目录内的非 Schema 文件（如 input/需求文档）。"""
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise PipelineError(code=X_ORCH_01, message=f"非法镜像路径：{relative_path}")
        target = self.job_dir(job_id) / relative
        if not target.is_file():
            raise PipelineError(code=X_ORCH_01, message=f"待镜像文件不存在：{relative_path}")
        storage_uri = self._mirror_file(job_id, target)
        self._register_artifact(job_id, relative_path, target, storage_uri, None)
        return storage_uri

    def write_manifest(self, job_id: str) -> Path:
        """写入内部产物清单，供对象存储双写/迁移使用（不作为节点完成判据）。"""
        root = self.job_dir(job_id)
        files: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name in _INTERNAL_FILES or path.suffix == ".tmp":
                continue
            try:
                relative = path.relative_to(root)
                content = path.read_bytes()
                stat = path.stat()
            except OSError:
                continue
            relative_name = relative.as_posix()
            if relative.parts and relative.parts[0] == ".trash":
                continue
            if relative.parts and relative.parts[0] == "input":
                kind = "document"
            elif relative.parts and relative.parts[0] == "scripts":
                kind = "script"
            elif relative.parts and relative.parts[0] == "archive":
                kind = "archive"
            elif relative.parts and relative.parts[0] == "evidence":
                kind = "evidence"
            elif relative_name.endswith(".jsonl"):
                kind = "log"
            else:
                kind = "artifact"
            files.append(
                {
                    "path": relative_name,
                    "object_key": f"{job_id}/{relative_name}",
                    "storage_uri": self.storage_uri(job_id, relative_name),
                    "kind": kind,
                    "size_bytes": stat.st_size,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "schema_valid": (
                        self.validate(job_id, path.name, json.loads(content)) == []
                        if path.suffix == ".json" and path.name in ARTIFACT_FILES
                        else None
                    ),
                }
            )
        payload = {
            "schema_version": "1.0",
            "job_id": job_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "files": files,
            "total_files": len(files),
            "total_bytes": sum(int(item["size_bytes"]) for item in files),
        }
        target = root / "artifact_manifest.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
        self._mirror_file(job_id, target)
        return target

    def _mirror_file(self, job_id: str, path: Path) -> str | None:
        """把本地成功落盘的文件镜像到对象存储；未配置时保持本地模式。"""
        if self._object_storage is None:
            return None
        relative = path.relative_to(self.job_dir(job_id)).as_posix()
        try:
            return self._object_storage.put_file(path, f"{job_id}/{relative}")
        except ObjectStorageError as exc:
            raise PipelineError(
                code=X_ORCH_01,
                message=f"对象存储镜像失败：{path.name}（{exc}）",
                node_id=None,
            ) from exc
        except Exception as exc:  # pragma: no cover - 防御性保护，不回显云端错误
            raise PipelineError(
                code=X_ORCH_01,
                message=f"对象存储镜像失败：{path.name}",
                node_id=None,
            ) from exc

    def _register_artifact(
        self,
        job_id: str,
        filename: str,
        path: Path,
        storage_uri: str | None,
        schema_version: str | None,
    ) -> None:
        """数据库启用时登记版本与校验和；登记失败阻止节点静默完成。"""
        if self._database is None:
            return
        try:
            self._database.register_artifact(
                job_id,
                filename,
                path,
                schema_version=schema_version,
                storage_uri=storage_uri,
                is_valid=True,
                deduplicate=self._deduplicate_artifact_index,
            )
        except Exception as exc:  # pragma: no cover - 由数据库驱动具体异常决定
            raise PipelineError(
                code=X_ORCH_01,
                message=f"产物索引登记失败：{filename}",
                node_id=None,
            ) from exc

    def storage_uri(self, job_id: str, relative_path: str) -> str:
        """返回 manifest 使用的地址；未配置对象存储时返回 file URI。"""
        target = self.job_dir(job_id) / relative_path
        object_key = f"{job_id}/{Path(relative_path).as_posix()}"
        if self._object_storage is not None:
            uri_for = getattr(self._object_storage, "uri_for", None)
            if callable(uri_for):
                return str(uri_for(object_key))
        return target.as_uri()

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
