"""任务持久化基础层（P0-01）。

本模块只负责数据库模型、建表和最小登记操作，不改变现有文件型流水线的执行语义。
生产环境使用 PostgreSQL，测试和本地迁移可以使用 SQLite；数据库未配置时由上层保持
兼容模式。JSON 产物仍由 ArtifactStore 保存，数据库只保存任务元数据、状态、事件和
产物索引，避免把大文件塞进关系库。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from common.security import redact_secrets
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
)
from sqlalchemy.engine import Engine as SqlEngine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    """兼容 SQLite 返回的无时区 datetime。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class Base(DeclarativeBase):
    """所有持久化模型的基类。"""


class JobRecord(Base):
    """任务级元数据。"""

    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)

    nodes: Mapped[list[JobNodeRecord]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class JobNodeRecord(Base):
    """任务节点状态、租约和错误信息。"""

    __tablename__ = "job_nodes"
    __table_args__ = (UniqueConstraint("job_id", "node_name", name="uq_job_node"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id", ondelete="CASCADE"), index=True)
    node_name: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    degraded_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)

    job: Mapped[JobRecord] = relationship(back_populates="nodes")


class JobEventRecord(Base):
    """追加式任务事件；payload 只允许脱敏内容。"""

    __tablename__ = "job_events"

    # SQLite 只有 INTEGER PRIMARY KEY 才能跨驱动自动递增；PostgreSQL 的
    # INTEGER 序列足够承载事件量，后续可按部署规模迁移为 BIGINT identity。
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id", ondelete="CASCADE"), index=True)
    node_name: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ArtifactRecord(Base):
    """产物文件/对象存储索引，不保存产物正文。"""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("job_id", "filename", "version", name="uq_artifact_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(256), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    schema_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_uri: Mapped[str] = mapped_column(Text)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    validation_error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JobConfigRecord(Base):
    """任务配置的脱敏版本快照。"""

    __tablename__ = "job_configs"
    __table_args__ = (UniqueConstraint("job_id", "version", name="uq_job_config_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    config_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class RepoSnapshotRecord(Base):
    """仓库来源和固定版本信息；不保存 Token。"""

    __tablename__ = "repo_snapshots"
    __table_args__ = (
        UniqueConstraint("job_id", "repo_name", name="uq_repo_snapshot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id", ondelete="CASCADE"), index=True)
    repo_name: Mapped[str] = mapped_column(String(128))
    source_type: Mapped[str] = mapped_column(String(16), default="local")
    source_url_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    baseline_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    target_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    baseline_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workspace_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SchemaMigrationRecord(Base):
    """已执行的数据库迁移版本。"""

    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Database:
    """数据库门面。

    `sqlite:///...` 适合本地迁移和单元测试；部门部署应使用 PostgreSQL URL。
    本类不在构造时自动创建表，调用方显式执行 `create_schema`，避免服务启动时
    未经迁移就修改生产数据库。
    """

    CURRENT_SCHEMA_VERSION = "001_initial"

    def __init__(self, url: str, *, echo: bool = False) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine: SqlEngine = create_engine(
            url,
            echo=echo,
            future=True,
            connect_args=connect_args,
            # 部门 PostgreSQL 连接可能经过 LB/NAT 被回收；每次取连接前探活，
            # 并定期轮换连接，避免瞬态断链冒泡为任务失败。
            pool_pre_ping=True,
            pool_recycle=1800,
        )
        self._session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        """创建缺失表；生产迁移接入 Alembic 后由迁移脚本负责版本升级。"""
        Base.metadata.create_all(self.engine)

    def migrate_schema(self) -> list[str]:
        """执行幂等数据库迁移并返回本次新增的版本。

        当前基线迁移负责登记完整 SQLAlchemy 模型（001_initial）。后续模型变更
        必须新增显式版本和迁移函数，禁止把 ``create_all`` 当作生产升级机制。
        ``create_schema`` 仍保留给 SQLite 单测和本地兼容调用。
        """
        self.create_schema()
        applied: list[str] = []
        with self.session() as session:
            known = {
                record.version
                for record in session.scalars(
                    select(SchemaMigrationRecord).order_by(SchemaMigrationRecord.version)
                )
            }
            for version in (self.CURRENT_SCHEMA_VERSION,):
                if version in known:
                    continue
                session.add(SchemaMigrationRecord(version=version))
                applied.append(version)
        return applied

    def schema_version(self) -> str | None:
        """返回当前数据库已登记的最高迁移版本。"""
        with self.session() as session:
            return cast(
                str | None,
                session.scalar(
                    select(SchemaMigrationRecord.version)
                    .order_by(SchemaMigrationRecord.version.desc())
                    .limit(1)
                ),
            )

    def healthcheck(self) -> bool:
        """执行轻量数据库探活，不修改任何业务数据。"""
        with self.session() as session:
            session.execute(select(1))
        return True

    @contextmanager
    def session(self) -> Iterator[Any]:
        """提供事务作用域，异常时自动回滚。"""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ensure_job(
        self,
        job_id: str,
        *,
        created_by: str | None = None,
        project_id: str | None = None,
    ) -> JobRecord:
        """幂等创建任务记录；已有任务只补齐缺失的归属信息。"""
        with self.session() as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                record = JobRecord(
                    job_id=job_id,
                    created_by=created_by,
                    project_id=project_id,
                )
                session.add(record)
                session.flush()
            else:
                if record.created_by is None and created_by is not None:
                    record.created_by = created_by
                if record.project_id is None and project_id is not None:
                    record.project_id = project_id
            return cast(JobRecord, record)

    def get_job(self, job_id: str) -> JobRecord | None:
        """读取任务索引；返回脱离 session 仍可安全使用的元数据副本。"""
        with self.session() as session:
            record = session.get(JobRecord, job_id)
            return cast(JobRecord | None, record)

    def save_job_config(
        self,
        job_id: str,
        payload: dict[str, Any],
        *,
        created_by: str | None = None,
    ) -> int:
        """保存脱敏任务配置快照，返回版本号，供共享卷丢失时恢复运行参数。"""
        safe_payload = redact_secrets(payload)
        with self.session() as session:
            job = session.get(JobRecord, job_id)
            if job is None:
                job = JobRecord(job_id=job_id, created_by=created_by)
                session.add(job)
                session.flush()
            latest = session.scalar(
                select(func.max(JobConfigRecord.version)).where(
                    JobConfigRecord.job_id == job_id
                )
            )
            version = int(latest or 0) + 1
            session.add(
                JobConfigRecord(
                    job_id=job_id,
                    version=version,
                    config_json=json.dumps(safe_payload, ensure_ascii=False),
                    created_by=created_by,
                )
            )
            job.config_version = version
            job.updated_at = utc_now()
            return version

    def latest_job_config(self, job_id: str) -> dict[str, Any] | None:
        """读取最新脱敏任务配置；不存在时返回 None。"""
        with self.session() as session:
            record = session.scalar(
                select(JobConfigRecord)
                .where(JobConfigRecord.job_id == job_id)
                .order_by(JobConfigRecord.version.desc())
                .limit(1)
            )
            if record is None:
                return None
            try:
                payload = json.loads(record.config_json)
            except json.JSONDecodeError:
                return None
            return payload if isinstance(payload, dict) else None

    def claim_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 120,
        created_by: str | None = None,
    ) -> bool:
        """领取整条流水线任务，避免多个服务实例同时执行同一个 job。"""
        if lease_seconds < 1:
            raise ValueError("lease_seconds 必须大于 0")
        now = utc_now()
        with self.session() as session:
            job = session.scalar(
                select(JobRecord).where(JobRecord.job_id == job_id).with_for_update()
            )
            if job is None:
                job = JobRecord(job_id=job_id, created_by=created_by)
                session.add(job)
                session.flush()
            lease_expires_at = _as_utc(job.lease_expires_at)
            if (
                job.worker_id
                and job.worker_id != worker_id
                and lease_expires_at is not None
                and lease_expires_at > now
            ):
                return False
            job.status = "running"
            job.worker_id = worker_id
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.heartbeat_at = now
            job.updated_at = now
            job.version += 1
            if job.started_at is None:
                job.started_at = now
            return True

    def heartbeat_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> bool:
        """续任务级租约；失去持有权或租约过期时返回 False。"""
        if lease_seconds < 1:
            raise ValueError("lease_seconds 必须大于 0")
        now = utc_now()
        with self.session() as session:
            job = session.scalar(select(JobRecord).where(JobRecord.job_id == job_id))
            if job is None or job.worker_id != worker_id:
                return False
            lease_expires_at = _as_utc(job.lease_expires_at)
            if lease_expires_at is None or lease_expires_at <= now:
                return False
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.heartbeat_at = now
            job.updated_at = now
            job.version += 1
            return True

    def release_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        status: str = "completed",
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """释放任务级租约并记录终态；非持有者不能覆盖任务状态。"""
        now = utc_now()
        with self.session() as session:
            job = session.scalar(select(JobRecord).where(JobRecord.job_id == job_id))
            if job is None or job.worker_id != worker_id:
                return False
            job.status = status
            job.worker_id = None
            job.lease_expires_at = None
            job.heartbeat_at = now
            job.updated_at = now
            job.finished_at = now if status in {"completed", "failed", "stopped"} else None
            job.error_code = error_code
            job.error_message = error_message
            job.version += 1
            return True

    def is_job_locked(self, job_id: str) -> bool:
        """判断任务是否有待执行请求或未过期租约。"""
        now = utc_now()
        with self.session() as session:
            job = session.scalar(select(JobRecord).where(JobRecord.job_id == job_id))
            if job is None:
                return False
            if job.status == "queued":
                return True
            expires_at = _as_utc(job.lease_expires_at)
            return bool(job.worker_id and expires_at is not None and expires_at > now)

    def stop_job(self, job_id: str) -> bool:
        """将无活动租约的排队任务标记为 stopped，供取消尚未领取的任务使用。"""
        now = utc_now()
        with self.session() as session:
            job = session.scalar(select(JobRecord).where(JobRecord.job_id == job_id))
            if job is None:
                return False
            expires_at = _as_utc(job.lease_expires_at)
            if job.worker_id and expires_at is not None and expires_at > now:
                return False
            job.status = "stopped"
            job.worker_id = None
            job.lease_expires_at = None
            job.heartbeat_at = now
            job.updated_at = now
            job.finished_at = now
            job.version += 1
            return True

    def fail_queued_job(
        self,
        job_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> bool:
        """数据库领取异常时收敛无租约排队任务，避免任务永久卡在 queued。"""
        now = utc_now()
        with self.session() as session:
            job = session.scalar(
                select(JobRecord).where(JobRecord.job_id == job_id).with_for_update()
            )
            if job is None:
                return False
            expires_at = _as_utc(job.lease_expires_at)
            if job.worker_id and expires_at is not None and expires_at > now:
                return False
            job.status = "failed"
            job.worker_id = None
            job.lease_expires_at = None
            job.heartbeat_at = now
            job.updated_at = now
            job.finished_at = now
            job.error_code = error_code
            job.error_message = error_message
            job.version += 1
            return True

    def record_node(
        self,
        job_id: str,
        node_name: str,
        *,
        status: str,
        attempts: int = 0,
        error: dict[str, Any] | None = None,
        worker_id: str | None = None,
    ) -> bool:
        """登记或更新节点快照。

        任务执行器传入 ``worker_id`` 时，只有仍持有未过期任务租约的 Worker
        才能回写，避免旧实例在租约切换后覆盖新实例状态。历史迁移、人工索引
        补录等非执行路径可省略身份，保持只登记快照的兼容语义。
        """
        now = utc_now()
        with self.session() as session:
            job = session.get(JobRecord, job_id)
            if worker_id is not None:
                if job is None or job.worker_id != worker_id:
                    return False
                lease_expires_at = _as_utc(job.lease_expires_at)
                if lease_expires_at is None or lease_expires_at <= now:
                    return False
            node = session.scalar(
                select(JobNodeRecord).where(
                    JobNodeRecord.job_id == job_id,
                    JobNodeRecord.node_name == node_name,
                )
            )
            if node is None:
                node = JobNodeRecord(job_id=job_id, node_name=node_name)
                session.add(node)
            node.status = status
            node.attempts = attempts
            node.updated_at = now
            if status == "running":
                node.started_at = node.started_at or now
                node.finished_at = None
            elif status in {"completed", "failed", "stopped", "manual_intervention"}:
                node.finished_at = now
            node.error_json = json.dumps(error, ensure_ascii=False) if error else None
            node.degraded_reason = (
                str(error.get("message"))
                if status == "manual_intervention" and isinstance(error, dict)
                else None
            )
            if job is not None:
                job.updated_at = now
                job.state_version += 1
            return True

    def list_nodes(self, job_id: str) -> list[JobNodeRecord]:
        """读取任务节点快照，按固定流水线顺序无法在数据库层保证时使用名称排序。"""
        with self.session() as session:
            return list(
                session.scalars(
                    select(JobNodeRecord)
                    .where(JobNodeRecord.job_id == job_id)
                    .order_by(JobNodeRecord.id.asc())
                )
            )

    def claim_node(
        self,
        job_id: str,
        node_name: str,
        *,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> bool:
        """以租约方式领取节点；未过期租约只能由原 Worker 续领。"""
        if lease_seconds < 1:
            raise ValueError("lease_seconds 必须大于 0")
        now = utc_now()
        with self.session() as session:
            node = session.scalar(
                select(JobNodeRecord)
                .where(
                    JobNodeRecord.job_id == job_id,
                    JobNodeRecord.node_name == node_name,
                )
                .with_for_update()
            )
            if node is None:
                if session.get(JobRecord, job_id) is None:
                    raise ValueError(f"job {job_id} 不存在")
                node = JobNodeRecord(job_id=job_id, node_name=node_name)
                session.add(node)
                session.flush()
            lease_expires_at = _as_utc(node.lease_expires_at)
            if (
                node.worker_id
                and node.worker_id != worker_id
                and lease_expires_at is not None
                and lease_expires_at > now
            ):
                return False
            if node.status in {"completed", "manual_intervention"} and node.worker_id != worker_id:
                return False
            node.status = "running"
            node.attempts += 1
            node.started_at = node.started_at or now
            node.updated_at = now
            node.worker_id = worker_id
            node.lease_expires_at = now + timedelta(seconds=lease_seconds)
            node.heartbeat_at = now
            node.version += 1
            return True

    def heartbeat_node(
        self,
        job_id: str,
        node_name: str,
        *,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> bool:
        """续租；Worker 丢失或租约已过期时返回 False。"""
        if lease_seconds < 1:
            raise ValueError("lease_seconds 必须大于 0")
        now = utc_now()
        with self.session() as session:
            node = session.scalar(
                select(JobNodeRecord).where(
                    JobNodeRecord.job_id == job_id,
                    JobNodeRecord.node_name == node_name,
                )
            )
            if node is None or node.worker_id != worker_id:
                return False
            lease_expires_at = _as_utc(node.lease_expires_at)
            if lease_expires_at is None or lease_expires_at <= now:
                return False
            node.lease_expires_at = now + timedelta(seconds=lease_seconds)
            node.heartbeat_at = now
            node.updated_at = now
            node.version += 1
            return True

    def release_node(
        self,
        job_id: str,
        node_name: str,
        *,
        worker_id: str,
        status: str = "completed",
        error: dict[str, Any] | None = None,
    ) -> bool:
        """释放节点租约；非持有者不能覆盖节点终态。"""
        now = utc_now()
        with self.session() as session:
            node = session.scalar(
                select(JobNodeRecord).where(
                    JobNodeRecord.job_id == job_id,
                    JobNodeRecord.node_name == node_name,
                )
            )
            if node is None or node.worker_id != worker_id:
                return False
            node.status = status
            node.worker_id = None
            node.lease_expires_at = None
            node.heartbeat_at = now
            node.updated_at = now
            node.finished_at = now if status in {"completed", "failed", "stopped"} else None
            node.error_json = json.dumps(error, ensure_ascii=False) if error else None
            node.version += 1
            return True

    def append_event(
        self,
        job_id: str,
        event_type: str,
        *,
        node_name: str | None = None,
        payload: dict[str, Any] | None = None,
        created_by: str | None = None,
    ) -> None:
        """追加脱敏事件；调用方不得传入 Token、密码或 API Key。"""
        with self.session() as session:
            session.add(
                JobEventRecord(
                    job_id=job_id,
                    node_name=node_name,
                    event_type=event_type,
                    payload_json=json.dumps(
                        redact_secrets(payload or {}), ensure_ascii=False
                    ),
                    created_by=created_by,
                )
            )

    def list_events(
        self,
        job_id: str,
        *,
        limit: int = 200,
        before_event_id: int | None = None,
    ) -> list[JobEventRecord]:
        """读取任务审计/运行事件，最新事件在前，支持按 event_id 翻页。"""
        if limit < 1:
            return []
        with self.session() as session:
            statement = select(JobEventRecord).where(JobEventRecord.job_id == job_id)
            if before_event_id is not None and before_event_id > 0:
                statement = statement.where(JobEventRecord.event_id < before_event_id)
            statement = statement.order_by(
                JobEventRecord.created_at.desc(), JobEventRecord.event_id.desc()
            ).limit(min(limit, 1000))
            return list(session.scalars(statement))

    def purge_events_before(self, cutoff: datetime) -> int:
        """删除早于 cutoff 的审计/运行事件，返回删除数量。"""
        with self.session() as session:
            records = list(
                session.scalars(select(JobEventRecord).where(JobEventRecord.created_at < cutoff))
            )
            for record in records:
                session.delete(record)
            return len(records)

    def count_jobs(
        self,
        *,
        created_by: str | None = None,
        project_id: str | None = None,
        statuses: set[str] | None = None,
        created_since: datetime | None = None,
    ) -> int:
        """按归属和状态统计任务，供部门级配额闸门使用。"""
        with self.session() as session:
            statement = select(func.count(JobRecord.job_id))
            if created_by is not None:
                statement = statement.where(JobRecord.created_by == created_by)
            if project_id is not None:
                statement = statement.where(JobRecord.project_id == project_id)
            if statuses:
                statement = statement.where(JobRecord.status.in_(sorted(statuses)))
            if created_since is not None:
                statement = statement.where(JobRecord.created_at >= created_since)
            return int(session.scalar(statement) or 0)

    def register_artifact(
        self,
        job_id: str,
        filename: str,
        path: Path,
        *,
        version: int = 1,
        schema_version: str | None = None,
        storage_uri: str | None = None,
        is_valid: bool = True,
        validation_error: list[str] | None = None,
        deduplicate: bool = True,
    ) -> None:
        """登记已落盘产物的校验和和存储地址。"""
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.session() as session:
            existing = (
                session.scalar(
                    select(ArtifactRecord).where(
                        ArtifactRecord.job_id == job_id,
                        ArtifactRecord.filename == filename,
                        ArtifactRecord.sha256 == digest,
                        ArtifactRecord.storage_uri == (storage_uri or str(path)),
                        ArtifactRecord.is_valid.is_(True),
                    )
                )
                if deduplicate
                else None
            )
            if deduplicate and existing is not None:
                return
            latest_version = session.scalar(
                select(func.max(ArtifactRecord.version)).where(
                    ArtifactRecord.job_id == job_id,
                    ArtifactRecord.filename == filename,
                )
            )
            effective_version = max(version, int(latest_version or 0) + 1)
            session.add(
                ArtifactRecord(
                    job_id=job_id,
                    filename=filename,
                    version=effective_version,
                    schema_version=schema_version,
                    sha256=digest,
                    size_bytes=path.stat().st_size,
                    storage_uri=storage_uri or str(path),
                    is_valid=is_valid,
                    validation_error_json=(
                        json.dumps(validation_error, ensure_ascii=False)
                        if validation_error
                        else None
                    ),
                )
            )

    def list_jobs(
        self,
        *,
        limit: int = 100,
        created_by: str | None = None,
        project_id: str | None = None,
    ) -> list[JobRecord]:
        """按更新时间倒序读取任务，避免扫描整个产物根目录。"""
        with self.session() as session:
            statement = select(JobRecord).where(JobRecord.status != "archived")
            if created_by is not None:
                statement = statement.where(JobRecord.created_by == created_by)
            if project_id is not None:
                statement = statement.where(JobRecord.project_id == project_id)
            return list(
                session.scalars(
                    statement.order_by(JobRecord.updated_at.desc()).limit(min(max(limit, 1), 1000))
                )
            )

    def archive_job(self, job_id: str) -> bool:
        """标记已进入产物回收区的任务，避免数据库恢复逻辑再次入队。"""
        with self.session() as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                return False
            record.status = "archived"
            record.worker_id = None
            record.lease_expires_at = None
            record.heartbeat_at = utc_now()
            record.finished_at = record.finished_at or utc_now()
            record.updated_at = utc_now()
            record.version += 1
            return True

    def list_artifacts(self, job_id: str) -> list[ArtifactRecord]:
        """按文件名、版本倒序读取任务产物索引。"""
        with self.session() as session:
            return list(
                session.scalars(
                    select(ArtifactRecord)
                    .where(ArtifactRecord.job_id == job_id)
                    .order_by(ArtifactRecord.filename.asc(), ArtifactRecord.version.desc())
                )
            )

    def latest_artifact(self, job_id: str, filename: str) -> ArtifactRecord | None:
        """返回指定文件最新的有效产物索引，供对象恢复校验完整性。"""
        with self.session() as session:
            return cast(
                ArtifactRecord | None,
                session.scalar(
                    select(ArtifactRecord)
                    .where(
                        ArtifactRecord.job_id == job_id,
                        ArtifactRecord.filename == filename,
                        ArtifactRecord.is_valid.is_(True),
                    )
                    .order_by(ArtifactRecord.version.desc())
                    .limit(1)
                ),
            )

    def register_repo_snapshot(
        self,
        job_id: str,
        repo_name: str,
        *,
        source_type: str,
        source_url_hash: str | None,
        target_ref: str | None,
        baseline_ref: str | None,
        target_sha: str | None,
        baseline_sha: str | None,
        workspace_path: str | None,
        cache_hit: bool,
    ) -> None:
        """登记任务实际使用的源码版本（不保存访问令牌或原始 URL）。"""
        with self.session() as session:
            record = session.scalar(
                select(RepoSnapshotRecord).where(
                    RepoSnapshotRecord.job_id == job_id,
                    RepoSnapshotRecord.repo_name == repo_name,
                )
            )
            if record is None:
                record = RepoSnapshotRecord(job_id=job_id, repo_name=repo_name)
                session.add(record)
            record.source_type = source_type
            record.source_url_hash = source_url_hash
            record.target_ref = target_ref
            record.baseline_ref = baseline_ref
            record.target_sha = target_sha
            record.baseline_sha = baseline_sha
            record.workspace_path = workspace_path
            record.cache_hit = cache_hit

    def list_repo_snapshots(self, job_id: str) -> list[RepoSnapshotRecord]:
        """读取任务源码快照元数据，供报告/迁移使用。"""
        with self.session() as session:
            return list(
                session.scalars(
                    select(RepoSnapshotRecord)
                    .where(RepoSnapshotRecord.job_id == job_id)
                    .order_by(RepoSnapshotRecord.repo_name.asc())
                )
            )

    def list_recoverable_jobs(self, *, limit: int = 1000) -> list[str]:
        """返回启动时可恢复的 queued/租约已过期 running 任务。"""
        now = utc_now()
        with self.session() as session:
            records = list(
                session.scalars(
                    select(JobRecord)
                    .where(JobRecord.status.in_(["queued", "running"]))
                    .order_by(JobRecord.updated_at.asc())
                    .limit(limit)
                )
            )
        recoverable: list[str] = []
        for record in records:
            if record.status == "queued":
                recoverable.append(record.job_id)
                continue
            expires_at = _as_utc(record.lease_expires_at)
            if expires_at is None or expires_at <= now:
                recoverable.append(record.job_id)
        return recoverable
