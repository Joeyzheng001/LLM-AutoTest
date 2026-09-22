"""P0-02 进程内队列测试：并发上限、重复入队和取消。"""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.job_queue import LocalJobQueue, QueueConflict
from orchestrator.persistence import Database


@pytest.mark.asyncio
async def test_queue_respects_concurrency_limit_and_runs_all_jobs() -> None:
    queue = LocalJobQueue(max_workers=2)
    active = 0
    peak = 0
    completed: list[str] = []

    async def run(job_id: str) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        completed.append(job_id)
        active -= 1

    try:
        tasks = [
            queue.submit(f"job-{index}", lambda index=index: run(f"job-{index}"))
            for index in range(5)
        ]
        await asyncio.gather(*tasks)
        assert peak == 2
        assert sorted(completed) == [f"job-{index}" for index in range(5)]
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_queue_releases_completed_task_registry_and_allows_rerun() -> None:
    """已完成任务不应永久占用进程内 tasks 映射，后续重跑仍可入队。"""
    queue = LocalJobQueue(max_workers=1)
    try:
        first = queue.submit("job-cleanup", lambda: asyncio.sleep(0))
        await first
        await asyncio.sleep(0)
        assert "job-cleanup" not in queue.tasks

        second = queue.submit("job-cleanup", lambda: asyncio.sleep(0))
        await second
        await asyncio.sleep(0)
        assert "job-cleanup" not in queue.tasks
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_queue_rejects_duplicate_active_job_and_cancels_running_work() -> None:
    queue = LocalJobQueue(max_workers=1)
    started = asyncio.Event()
    finished = asyncio.Event()

    async def long_job() -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            finished.set()

    try:
        task = queue.submit("job-duplicate", long_job)
        await started.wait()
        with pytest.raises(QueueConflict):
            queue.submit("job-duplicate", long_job)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(finished.wait(), timeout=1)
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_queue_skips_duplicate_shared_job_without_reclaiming_after_release(tmp_path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'queue.db'}")
    db.create_schema()
    first = LocalJobQueue(max_workers=1, database=db, worker_id="worker-a", lease_seconds=1)
    second = LocalJobQueue(max_workers=1, database=db, worker_id="worker-b", lease_seconds=1)
    started = asyncio.Event()
    release = asyncio.Event()
    ran_second = asyncio.Event()

    async def first_job() -> None:
        started.set()
        await release.wait()

    async def second_job() -> None:
        ran_second.set()

    try:
        first_task = first.submit("job-shared", first_job)
        await started.wait()
        second_task = second.submit("job-shared", second_job)
        await asyncio.sleep(0.05)
        await asyncio.wait_for(second_task, timeout=1)
        assert not ran_second.is_set()
        release.set()
        await first_task
        assert not ran_second.is_set()
    finally:
        await first.shutdown()
        await second.shutdown()


@pytest.mark.asyncio
async def test_queue_cancels_execution_when_database_lease_is_lost(tmp_path) -> None:
    """租约被其他 Worker 接管后，旧执行必须停止而不是继续写产物。"""
    db = Database(f"sqlite:///{tmp_path / 'lease-loss.db'}")
    db.create_schema()
    queue = LocalJobQueue(max_workers=1, database=db, worker_id="worker-a", lease_seconds=1)
    started = asyncio.Event()
    finished = asyncio.Event()

    async def long_job() -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            finished.set()

    try:
        task = queue.submit("job-lease-loss", long_job)
        await started.wait()
        # 模拟数据库租约已被恢复实例接管/释放；旧 Worker 的下一次心跳必须中止执行。
        assert db.release_job("job-lease-loss", worker_id="worker-a", status="stopped") is True
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(finished.wait(), timeout=2)
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_queue_database_claim_failure_does_not_leave_worker_or_job_stuck(tmp_path) -> None:
    """数据库租约领取异常应收敛任务并保留 Worker，不能永久卡在 queued。"""
    db = Database(f"sqlite:///{tmp_path / 'claim-failure.db'}")
    db.create_schema()
    original_claim = db.claim_job

    def fail_claim(*args, **kwargs):
        raise RuntimeError("database temporarily unavailable")

    db.claim_job = fail_claim  # type: ignore[method-assign]
    queue = LocalJobQueue(max_workers=1, database=db, worker_id="worker-a")
    ran = False

    async def job() -> None:
        nonlocal ran
        ran = True

    try:
        task = queue.submit("job-claim-failure", job)
        await asyncio.wait_for(task, timeout=1)
        record = db.get_job("job-claim-failure")
        assert record is not None
        assert record.status == "failed"
        assert record.error_code == "X-ORCH-03"
        assert ran is False
        # 领取异常不能杀死 Worker；恢复方法后下一任务仍可执行。
        db.claim_job = original_claim  # type: ignore[method-assign]
        second = queue.submit("job-claim-recovered", job)
        await asyncio.wait_for(second, timeout=1)
        assert ran is True
    finally:
        await queue.shutdown()


@pytest.mark.asyncio
async def test_queue_heartbeat_database_failure_cancels_execution(tmp_path) -> None:
    """续租驱动异常按租约不确定处理，旧执行必须停止而非继续写证据。"""
    db = Database(f"sqlite:///{tmp_path / 'heartbeat-failure.db'}")
    db.create_schema()
    original_heartbeat = db.heartbeat_job
    queue = LocalJobQueue(max_workers=1, database=db, worker_id="worker-a", lease_seconds=1)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def long_job() -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    calls = 0

    def fail_heartbeat(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("database heartbeat unavailable")

    db.heartbeat_job = fail_heartbeat  # type: ignore[method-assign]
    try:
        task = queue.submit("job-heartbeat-failure", long_job)
        await started.wait()
        # 续租间隔为 lease_seconds/3，等待一次异常心跳触发取消。
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert calls >= 1
        assert cancelled.is_set()
    finally:
        db.heartbeat_job = original_heartbeat  # type: ignore[method-assign]
        await queue.shutdown()
