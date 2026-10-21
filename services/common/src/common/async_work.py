"""有界异步任务：按输入顺序合并，失败或取消时回收全部工作任务。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar, cast

T = TypeVar("T")
R = TypeVar("R")


async def ordered_parallel_map(
    items: Sequence[T], work: Callable[[T], Awaitable[R]], *, concurrency: int
) -> list[R]:
    """只创建固定数量的 worker；不会一次创建全量模型调用任务。"""
    if concurrency < 1:
        raise ValueError("concurrency 必须大于 0")
    results: list[R | None] = [None] * len(items)
    cursor = 0

    async def worker() -> None:
        nonlocal cursor
        while cursor < len(items):
            # 同一事件循环内，领取索引到递增之间没有 await。
            index = cursor
            cursor += 1
            results[index] = await work(items[index])

    tasks = [asyncio.create_task(worker()) for _ in range(min(concurrency, len(items)))]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return cast(list[R], results)
