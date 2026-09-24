"""java-parser 服务客户端：异步 scan job 的创建与轮询（契约 v0.1 §/api/v1/scan-jobs）。

口径：
- 契约错误结构 {code, message, node_id?, details}：code 属 X-CODE-* 段（乙），
  客户端原样透传（不翻译、不吞），加 node_id 归位到 code_scan；
- 通信类失败（连不上/超时/HTTP 异常/轮询耗尽）归 X-ORCH-03（docs/12 v1.1）；
- 轮询参数（间隔/总时限）为运维调优项，非 LLM 采样参数——默认值常量 +
  构造器显式注入（测试用小值加速）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx
from common.errors import X_ORCH_03, PipelineError

DEFAULT_TIMEOUT_S = 30.0  # 单次 HTTP 请求超时
DEFAULT_POLL_INTERVAL_S = 2.0  # 轮询间隔
DEFAULT_MAX_WAIT_S = 600.0  # job 总等待上限（超时算 X-ORCH-03）

# 契约 JobStatus：queued / running / completed / failed（终态后两者）


@dataclass(frozen=True)
class RepositoryInput:
    """契约 RepositoryInput：路径 + 分支（07 遗留事项 #1）；基线分支可选（C-09）。

    language（C-13）：java → java-parser；python → orchestrator 内置 AST 扫描
    （to_dict 不含 language——java-parser 契约不变，分流在节点层完成）。
    """

    name: str
    path: str
    branch: str
    baseline_branch: str | None = None
    is_fallback: bool = False
    include_paths: tuple[str, ...] = field(default_factory=tuple)
    language: str = "java"
    # 可选的真实服务入口；源码扫描契约不消费该字段，执行节点按 code_location.repo 路由。
    service_base_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """请求体序列化（可选字段仅在非默认时出现）。"""
        payload: dict[str, Any] = {
            "name": self.name,
            "path": self.path,
            "branch": self.branch,
        }
        if self.baseline_branch:
            payload["baseline_branch"] = self.baseline_branch
        if self.is_fallback:
            payload["is_fallback"] = True
        if self.include_paths:
            payload["include_paths"] = list(self.include_paths)
        return payload


class JavaParserClient:
    """scan-jobs 异步客户端：POST 创建 → GET 轮询至终态。"""

    def __init__(
        self,
        base_url: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        max_wait_s: float = DEFAULT_MAX_WAIT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """transport 仅测试注入（httpx.MockTransport），生产留 None。"""
        self._base_url = base_url
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s
        self._max_wait_s = max_wait_s
        self._transport = transport

    async def scan(self, job_id: str, repositories: list[RepositoryInput]) -> dict[str, Any]:
        """创建扫描 job 并轮询至终态（POST /api/v1/scan-jobs）。

        Returns:
            completed 时的 ScanJob 响应（含 code_inventory）。

        Raises:
            PipelineError: failed 终态（透传契约 X-CODE-* 错误）；或 X-ORCH-03 通信失败。
        """
        body = {
            "job_id": job_id,
            "repositories": [repo.to_dict() for repo in repositories],
        }
        response = await self._request("POST", "/api/v1/scan-jobs", json=body)
        job_id_remote = self._parse_job_id(response)
        return await self._poll("/api/v1/scan-jobs", job_id_remote)

    async def extract(
        self,
        job_id: str,
        repositories: list[RepositoryInput],
        protocols: list[str] | None = None,
    ) -> dict[str, Any]:
        """创建候选提取 job 并轮询至终态（POST /api/v1/extract-jobs，R-REV-02①）。

        Args:
            protocols: 要求识别的协议（默认全部；D2：gRPC 优先）。

        Returns:
            completed 时的 ExtractJob 响应（含 candidates + warnings）。

        Raises:
            PipelineError: 同 scan——透传契约错误或 X-ORCH-03。
        """
        body: dict[str, Any] = {
            "job_id": job_id,
            "repositories": [repo.to_dict() for repo in repositories],
        }
        if protocols:
            body["protocols"] = protocols
        response = await self._request("POST", "/api/v1/extract-jobs", json=body)
        job_id_remote = self._parse_job_id(response)
        return await self._poll("/api/v1/extract-jobs", job_id_remote)

    def _parse_job_id(self, response: httpx.Response) -> str:
        """从 202 受理响应提取 job_id（缺字段宁停不猜）。"""
        data = response.json()
        try:
            return str(data["job_id"])
        except (KeyError, TypeError) as exc:
            raise PipelineError(
                code=X_ORCH_03,
                message=f"java-parser 响应缺 job_id：{data}",
            ) from exc

    async def _poll(self, base_path: str, job_id_remote: str) -> dict[str, Any]:
        """GET {base_path}/{id} 轮询至 completed/failed（两套 job 共用）。"""
        waited = 0.0
        while True:
            response = await self._request("GET", f"{base_path}/{job_id_remote}")
            data: dict[str, Any] = response.json()
            status = str(data.get("status", ""))
            if status == "completed":
                return data
            if status == "failed":
                raise _contract_error(data)
            if status not in ("queued", "running"):
                # 契约只定义四态，表外状态宁停不猜
                raise PipelineError(
                    code=X_ORCH_03,
                    message=f"java-parser 返回未知 job 状态：{status}",
                )
            if waited >= self._max_wait_s:
                raise PipelineError(
                    code=X_ORCH_03,
                    message=f"job 轮询超时（>{self._max_wait_s}s）：{job_id_remote}",
                )
            await asyncio.sleep(self._poll_interval_s)
            waited += self._poll_interval_s

    async def _request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> httpx.Response:
        """单次 HTTP 调用；通信异常统一 X-ORCH-03。"""
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_s,
            transport=self._transport,
        ) as client:
            try:
                response = await client.request(method, path, json=json)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise PipelineError(
                    code=X_ORCH_03,
                    message=f"java-parser 通信失败：{exc}",
                ) from exc
        if response.status_code >= 400:
            # 契约错误体（400/404 携带 {code, message}）：透传其 code
            try:
                error_body = response.json()
            except ValueError:
                error_body = {}
            if isinstance(error_body, dict) and error_body.get("code"):
                raise _contract_error(error_body)
            raise PipelineError(
                code=X_ORCH_03,
                message=f"java-parser HTTP {response.status_code}（无契约错误体）",
            )
        return response


def _contract_error(data: dict[str, Any]) -> PipelineError:
    """契约 Error → PipelineError：code 原样透传，node 归位 code_scan。"""
    return PipelineError(
        code=str(data.get("code", X_ORCH_03)),
        message=str(data.get("message", "java-parser 契约错误体缺 message")),
        node_id="code_scan",
        details=data.get("details") or {},
    )
