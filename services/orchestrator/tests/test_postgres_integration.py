"""可选 PostgreSQL 真实集成测试。

仅当显式设置 ``AUTOTEST_TEST_DATABASE_URL`` 且目标库名包含 ``test`` 时执行，
用于发布前验证 PostgreSQL 行锁和多个独立队列实例的租约语义。默认单测不会
连接任何外部数据库；禁止把生产库 DSN 配给该变量。
"""

from __future__ import annotations

import asyncio
import os
import queue
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete
from sqlalchemy.engine import make_url

from orchestrator.job_queue import LocalJobQueue
from orchestrator.persistence import Database, JobRecord

_DATABASE_URL = os.environ.get("AUTOTEST_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not _DATABASE_URL,
    reason="设置 AUTOTEST_TEST_DATABASE_URL 指向专用 PostgreSQL 测试库后运行",
)


@pytest.fixture(scope="module")
def postgres_database() -> Iterator[Database]:
    """初始化并连接显式专用测试库；拒绝 SQLite 和库名不含 test 的 DSN。"""
    if not _DATABASE_URL:
        pytest.skip("未配置 PostgreSQL 集成测试 DSN")
    url = make_url(_DATABASE_URL)
    if url.get_backend_name() != "postgresql":
        pytest.fail("AUTOTEST_TEST_DATABASE_URL 必须指向 PostgreSQL")
    if "test" not in (url.database or "").lower():
        pytest.fail("为防止误操作，PostgreSQL 集成测试库名必须包含 test")
    database = Database(_DATABASE_URL)
    database.migrate_schema()
    yield database
    database.engine.dispose()


def _new_job_id() -> str:
    return f"job-pg-it-{uuid.uuid4().hex[:20]}"


def _delete_job(database: Database, job_id: str) -> None:
    """只清理本测试创建的随机 job 记录；数据库本身不删除或重建。"""
    with database.session() as session:
        session.execute(delete(JobRecord).where(JobRecord.job_id == job_id))


def test_postgres_row_lock_allows_only_one_simultaneous_job_claim(
    postgres_database: Database,
) -> None:
    """两个独立 DB engine 同时争抢时只允许一个 Worker 获得同一任务租约。"""
    job_id = _new_job_id()
    databases = [Database(_DATABASE_URL), Database(_DATABASE_URL)]
    barrier = threading.Barrier(2)
    results: queue.Queue[tuple[int, bool]] = queue.Queue()

    def claim(index: int) -> None:
        barrier.wait(timeout=10)
        claimed = databases[index].claim_job(
            job_id, worker_id=f"pg-worker-{index}", lease_seconds=30
        )
        results.put((index, claimed))

    try:
        postgres_database.ensure_job(job_id)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(claim, index) for index in range(2)]
            for future in futures:
                future.result(timeout=15)
        claims = [results.get_nowait() for _ in range(2)]
        assert sum(claimed for _, claimed in claims) == 1
        winner = next(index for index, claimed in claims if claimed)
        loser = 1 - winner

        assert databases[loser].claim_job(
            job_id, worker_id=f"pg-worker-{loser}", lease_seconds=30
        ) is False
        assert databases[loser].record_node(
            job_id,
            "auto_execute",
            status="completed",
            worker_id=f"pg-worker-{loser}",
        ) is False
        assert databases[winner].heartbeat_job(
            job_id, worker_id=f"pg-worker-{winner}", lease_seconds=30
        ) is True
        assert databases[winner].record_node(
            job_id,
            "auto_execute",
            status="completed",
            worker_id=f"pg-worker-{winner}",
        ) is True
        assert databases[winner].release_job(
            job_id, worker_id=f"pg-worker-{winner}", status="completed"
        ) is True
        record = databases[winner].get_job(job_id)
        assert record is not None and record.status == "completed"
    finally:
        for database in databases:
            database.engine.dispose()
        _delete_job(postgres_database, job_id)


@pytest.mark.asyncio
async def test_postgres_independent_queues_do_not_execute_same_job_twice(
    postgres_database: Database,
) -> None:
    """两个独立 LocalJobQueue 连接同一 PostgreSQL 时第二个 Worker 必须跳过。"""
    job_id = _new_job_id()
    first_database = Database(_DATABASE_URL)
    second_database = Database(_DATABASE_URL)
    first_queue = LocalJobQueue(
        max_workers=1, database=first_database, worker_id="pg-queue-worker-a", lease_seconds=30
    )
    second_queue = LocalJobQueue(
        max_workers=1, database=second_database, worker_id="pg-queue-worker-b", lease_seconds=30
    )
    started = asyncio.Event()
    release = asyncio.Event()
    executed: list[str] = []

    async def first_job() -> None:
        executed.append("first")
        started.set()
        await release.wait()

    async def second_job() -> None:
        executed.append("second")

    try:
        postgres_database.ensure_job(job_id)
        first_task = first_queue.submit(job_id, first_job)
        await asyncio.wait_for(started.wait(), timeout=5)
        second_task = second_queue.submit(job_id, second_job)
        await asyncio.wait_for(second_task, timeout=5)
        assert executed == ["first"]

        release.set()
        await asyncio.wait_for(first_task, timeout=5)
        record = postgres_database.get_job(job_id)
        assert record is not None and record.status == "completed"
        assert executed == ["first"]
    finally:
        release.set()
        await first_queue.shutdown()
        await second_queue.shutdown()
        first_database.engine.dispose()
        second_database.engine.dispose()
        _delete_job(postgres_database, job_id)
