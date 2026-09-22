"""进程内后台任务队列（P0-02）。

请求只负责入队，固定数量的 Worker 协程负责实际执行；配置数据库后，领取、心跳
和释放使用任务级租约跨实例串行化。任务本身仍由调用方提供，因此不会改变
PipelineRunner 的节点顺序和产物语义；租约丢失时会主动取消旧执行，避免双写证据。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from functools import partial
from typing import Any

from orchestrator.persistence import Database

logger = logging.getLogger(__name__)


class QueueConflict(RuntimeError):
    """同一 job 已有运行中或排队中的任务。"""


@dataclass
class _QueueItem:
    job_id: str
    factory: Callable[[], Coroutine[Any, Any, None]]
    result: asyncio.Future[None]
    task: asyncio.Task[None] | None = None
    execution: asyncio.Task[None] | None = None
    cancelled: bool = False


class LocalJobQueue:
    """带并发上限的进程内队列。

    `tasks` 保留现有 API 对运行中任务的观察方式；任务完成后从活动注册表移除，避免
    长期运行时按历史 job 数量累积 `asyncio.Task` 引用。已完成/失败状态由数据库或
    state.json 负责查询，不能依赖进程内 Task 引用。
    """

    def __init__(
        self,
        max_workers: int = 2,
        *,
        database: Database | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 120,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers 必须大于 0")
        self.max_workers = max_workers
        if lease_seconds < 1:
            raise ValueError("lease_seconds 必须大于 0")
        self.database = database
        self.worker_id = worker_id or f"worker-{os.getpid()}-{id(self)}"
        self.lease_seconds = lease_seconds
        self._queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._items: dict[str, _QueueItem] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    @property
    def queued_count(self) -> int:
        """当前尚未被 Worker 领取的任务数。"""
        return self._queue.qsize()

    @property
    def active_count(self) -> int:
        """当前排队或执行中的任务数。"""
        return sum(1 for task in self.tasks.values() if not task.done())

    def submit(
        self, job_id: str, factory: Callable[[], Coroutine[Any, Any, None]]
    ) -> asyncio.Task[None]:
        """提交任务；同一 job 的活动任务拒绝重复入队。"""
        if self._closed:
            raise RuntimeError("任务队列已关闭")
        previous = self.tasks.get(job_id)
        if previous is not None and not previous.done():
            raise QueueConflict(f"job {job_id} 已在队列中或运行中")
        if self.database is not None:
            # 先登记 queued，再由 Worker 领取 running 租约；服务重启时可据此恢复。
            self.database.ensure_job(job_id)

        loop = asyncio.get_running_loop()
        item = _QueueItem(job_id=job_id, factory=factory, result=loop.create_future())
        task = loop.create_task(self._await_result(item), name=f"job-queue:{job_id}")
        item.task = task
        task.add_done_callback(partial(self._forget_task, job_id))
        self._items[job_id] = item
        self.tasks[job_id] = task
        self._ensure_workers()
        self._queue.put_nowait(item)
        return task

    def _forget_task(self, job_id: str, task: asyncio.Task[None]) -> None:
        """任务终态后释放进程内引用；外部状态仍由持久化层保留。"""
        if self.tasks.get(job_id) is task:
            self.tasks.pop(job_id, None)

    def _ensure_workers(self) -> None:
        alive = [worker for worker in self._workers if not worker.done()]
        self._workers = alive
        for index in range(len(alive), self.max_workers):
            self._workers.append(
                asyncio.create_task(self._worker_loop(), name=f"job-worker:{index}")
            )

    async def _await_result(self, item: _QueueItem) -> None:
        try:
            await item.result
        except asyncio.CancelledError:
            item.cancelled = True
            if item.execution is not None and not item.execution.done():
                item.execution.cancel()
            if not item.result.done():
                item.result.cancel()
            raise
        finally:
            if self._items.get(item.job_id) is item:
                self._items.pop(item.job_id, None)

    async def _worker_loop(self) -> None:
        while not self._closed:
            item = await self._queue.get()
            try:
                if item.cancelled or item.task is None or item.task.cancelled():
                    if not item.result.done():
                        item.result.cancel()
                    continue
                if self.database is not None:
                    try:
                        claimed = await asyncio.to_thread(
                            self.database.claim_job,
                            item.job_id,
                            worker_id=self.worker_id,
                            lease_seconds=self.lease_seconds,
                        )
                    except Exception as exc:  # pragma: no cover - driver-specific failures
                        # 数据库不可用不能让 Worker 协程退出、也不能让队列 Future 永久等待。
                        # 尝试将无租约任务收敛为 failed；若数据库本身也不可用，至少释放
                        # 队列项，避免进程内 Worker 和 Future 永久悬挂。
                        logger.exception("job %s 数据库租约领取失败", item.job_id)
                        fail_queued = getattr(self.database, "fail_queued_job", None)
                        if callable(fail_queued):
                            try:
                                await asyncio.to_thread(
                                    fail_queued,
                                    item.job_id,
                                    error_code="X-ORCH-03",
                                    error_message=f"数据库租约领取失败（{type(exc).__name__}）",
                                )
                            except Exception:  # pragma: no cover - 数据库完全不可用
                                logger.exception("job %s 无法收敛数据库失败状态", item.job_id)
                        if not item.result.done():
                            item.result.set_result(None)
                        continue
                    if not claimed:
                        # 其他实例已持有租约，或任务已被其他实例处理。不能等待
                        # 租约释放后再次领取，否则服务重启恢复会把同一 job 顺序执行两次。
                        # 显式重跑由 API 在旧任务结束后重新 submit，拥有新的队列项。
                        if not item.result.done():
                            item.result.set_result(None)
                        logger.info("job %s 未获得数据库租约，本实例跳过执行", item.job_id)
                        continue
                item.execution = asyncio.create_task(
                    item.factory(), name=f"job-execution:{item.job_id}"
                )
                execution_status = "completed"
                heartbeat = (
                    asyncio.create_task(self._heartbeat_loop(item))
                    if self.database is not None
                    else None
                )
                execution_error: BaseException | None = None
                execution_cancelled = False
                try:
                    await item.execution
                except asyncio.CancelledError:
                    execution_status = "stopped"
                    execution_cancelled = True
                except BaseException as exc:
                    execution_status = "failed"
                    execution_error = exc
                finally:
                    if heartbeat is not None:
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat, return_exceptions=True)
                    if self.database is not None:
                        try:
                            await asyncio.to_thread(
                                self.database.release_job,
                                item.job_id,
                                worker_id=self.worker_id,
                                status=execution_status,
                            )
                        except Exception:  # pragma: no cover - 驱动/网络相关
                            # 释放失败不能杀死 Worker；数据库恢复后由租约过期/恢复逻辑
                            # 收敛状态，当前进程仍需继续处理其它任务。
                            logger.exception("job %s 数据库租约释放失败", item.job_id)
                # 先释放数据库租约，再唤醒调用方，避免调用方读到仍为 running 的任务。
                if not item.result.done():
                    if execution_cancelled:
                        item.result.cancel()
                    elif execution_error is not None:
                        item.result.set_exception(execution_error)
                    else:
                        item.result.set_result(None)
            finally:
                self._queue.task_done()

    async def _heartbeat_loop(self, item: _QueueItem) -> None:
        """任务执行期间按租约三分之一周期续租。"""
        if self.database is None:
            return
        interval = max(1.0, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await asyncio.to_thread(
                    self.database.heartbeat_job,
                    item.job_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
            except Exception as exc:  # pragma: no cover - 驱动/网络相关
                logger.exception(
                    "job %s 数据库租约续租异常（%s），取消旧 Worker 的执行",
                    item.job_id,
                    type(exc).__name__,
                )
                if item.execution is not None and not item.execution.done():
                    item.execution.cancel()
                return
            if not renewed:
                logger.warning(
                    "job %s 租约续租失败，取消旧 Worker 的执行，避免继续写入产物",
                    item.job_id,
                )
                if item.execution is not None and not item.execution.done():
                    item.execution.cancel()
                return

    async def shutdown(self) -> None:
        """服务退出时取消队列和 Worker，避免后台协程泄漏。"""
        self._closed = True
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self.tasks.values(), *self._workers, return_exceptions=True)
        self._workers.clear()
        self._items.clear()


__all__ = ["LocalJobQueue", "QueueConflict"]
