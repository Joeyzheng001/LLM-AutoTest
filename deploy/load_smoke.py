"""轻量 HTTP 压测基线，不提交任务、不调用 LLM。

示例：
    uv run python deploy/load_smoke.py --url http://127.0.0.1:8000 --path /ready \
      --requests 100 --concurrency 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from typing import Any

import httpx


async def _run(url: str, path: str, total: int, concurrency: int) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    statuses: list[int] = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        async def one() -> None:
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.get(f"{url.rstrip('/')}/{path.lstrip('/')}")
                    statuses.append(response.status_code)
                except httpx.HTTPError:
                    statuses.append(599)
                latencies.append((time.perf_counter() - started) * 1000)

        await asyncio.gather(*(one() for _ in range(total)))

    ordered = sorted(latencies)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    failures = sum(status >= 500 for status in statuses)
    return {
        "url": url,
        "path": path,
        "requests": total,
        "concurrency": concurrency,
        "failures_5xx_or_network": failures,
        "status_counts": {str(code): statuses.count(code) for code in sorted(set(statuses))},
        "latency_ms": {
            "p50": round(statistics.median(ordered), 2) if ordered else None,
            "p95": round(ordered[p95_index], 2) if ordered else None,
            "max": round(max(ordered), 2) if ordered else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-AutoTest HTTP smoke baseline")
    parser.add_argument("--url", required=True, help="编排服务地址")
    parser.add_argument("--path", default="/ready", help="只读探测路径，默认 /ready")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1:
        parser.error("requests 和 concurrency 必须大于 0")
    result = asyncio.run(_run(args.url, args.path, args.requests, args.concurrency))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
