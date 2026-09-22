"""部门部署验收探针：只读检查统一入口的健康、就绪、指标和鉴权。

示例：
    python deploy/acceptance_smoke.py --url http://autotest.intra
    python deploy/acceptance_smoke.py --url https://autotest.test --insecure-tls  # 仅测试环境

共享部署若要验证受保护 API，请通过环境变量提供凭据，避免令牌进入 shell 历史：
    AUTOTEST_ACCEPTANCE_TOKEN='...' AUTOTEST_ACCEPTANCE_PROJECT_ID=fund \
      python deploy/acceptance_smoke.py --url http://autotest.intra

脚本不提交任务、不调用 LLM，也不会把响应正文或令牌写入输出。
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def _probe(
    base_url: str,
    path: str,
    *,
    headers: dict[str, str],
    timeout: float,
    retries: int,
    ssl_context: ssl.SSLContext | None = None,
) -> dict[str, Any]:
    """探测一个只读端点；仅返回可审计的状态摘要，不回显正文。"""
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    last_error = ""
    for attempt in range(retries + 1):
        request = urllib.request.Request(  # noqa: S310 - URL comes from deployment operator
            url, headers=headers, method="GET"
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - URL comes from deployment operator
                request, timeout=timeout, context=ssl_context
            ) as response:
                body = response.read(64 * 1024)
                content_type = response.headers.get("Content-Type", "")
                payload: dict[str, Any] | None = None
                try:
                    decoded = json.loads(body.decode("utf-8"))
                    if isinstance(decoded, dict):
                        payload = decoded
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = None
                return {
                    "path": path,
                    "status_code": response.status,
                    "json_status": payload.get("status") if payload else None,
                    "content_type": content_type,
                    # 只记录是否包含本系统指标前缀，不输出指标正文，避免把任务标签
                    # 或未来新增的敏感标签意外带入部署日志。
                    "has_autotest_metric": (
                        path == "/metrics" and b"autotest_" in body
                    ),
                }
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if attempt >= retries:
                return {"path": path, "status_code": exc.code, "error": last_error}
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            last_error = type(exc).__name__
            if attempt >= retries:
                return {"path": path, "status_code": None, "error": last_error}
        time.sleep(min(0.5 * (attempt + 1), 2.0))
    return {"path": path, "status_code": None, "error": last_error or "unknown"}


def run_checks(
    base_url: str,
    *,
    token: str | None = None,
    project_id: str | None = None,
    timeout: float = 5.0,
    retries: int = 2,
    insecure_tls: bool = False,
) -> dict[str, Any]:
    """执行统一入口验收，返回不含敏感信息的结果。"""
    if timeout <= 0:
        raise ValueError("timeout 必须大于 0")
    if retries < 0:
        raise ValueError("retries 不能为负数")
    if bool(token) != bool(project_id):
        raise ValueError("token 与 project_id 必须同时提供")

    headers = {"Accept": "application/json"}
    # 证书校验默认开启；只有部署人员显式传 --insecure-tls 才允许测试环境自签名证书。
    ssl_context = ssl._create_unverified_context() if insecure_tls else None  # noqa: S323
    if token and project_id:
        headers.update(
            {
                "Authorization": f"Bearer {token}",
                "X-Project-Id": project_id,
            }
        )

    checks: list[dict[str, Any]] = []
    for path in ("/health", "/ready", "/metrics"):
        result = _probe(
            base_url,
            path,
            headers=headers,
            timeout=timeout,
            retries=retries,
            ssl_context=ssl_context,
        )
        expected = result.get("status_code") == 200
        if path == "/health":
            expected = expected and result.get("json_status") == "ok"
        elif path == "/ready":
            expected = expected and result.get("json_status") == "ready"
        else:
            expected = expected and result.get("has_autotest_metric") is True
        checks.append({**result, "ok": expected})

    if token and project_id:
        result = _probe(
            base_url,
            "/api/v1/jobs",
            headers=headers,
            timeout=timeout,
            retries=retries,
            ssl_context=ssl_context,
        )
        checks.append({**result, "ok": result.get("status_code") == 200})

    return {
        "url": base_url.rstrip("/"),
        "checks": checks,
        "ok": all(item["ok"] for item in checks),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM-AutoTest 部门部署验收探针")
    parser.add_argument("--url", required=True, help="统一入口地址，例如 http://autotest.intra")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--insecure-tls",
        action="store_true",
        help="仅测试环境显式跳过入口 HTTPS 证书校验（默认严格校验）",
    )
    args = parser.parse_args()
    try:
        result = run_checks(
            args.url,
            token=os.environ.get("AUTOTEST_ACCEPTANCE_TOKEN") or None,
            project_id=os.environ.get("AUTOTEST_ACCEPTANCE_PROJECT_ID") or None,
            timeout=args.timeout,
            retries=args.retries,
            insecure_tls=args.insecure_tls,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.insecure_tls:
        print("warning: 已显式跳过入口 HTTPS 证书校验，仅限测试环境", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
