"""GLM 客户端：OpenAI 兼容 chat/completions 调用 + 留痕 + 超时/重试治理。

口径全部来自 docs/12 第三章：
- 每次调用向 llm_calls.jsonl 追加一行留痕（call_id 格式 LLM-<job 短码>-<序号>）；
- 超时算调用失败；仅网络/超时类失败重试，最多 2 次、指数退避 5s/20s；
- 采样参数取 Settings 实际值（模板建议值仅供参考，禁止硬编码）；
- token 数取 API 响应原始值，不做换算修饰。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from orchestrator.config import Settings
from orchestrator.errors import X_LLM_01, PipelineError
from orchestrator.prompts_loader import PromptSpec

# docs/12 §三：重试退避间隔（秒）；测试注入 0 加速
DEFAULT_RETRY_BACKOFF: tuple[float, float] = (5.0, 20.0)
_MAX_ATTEMPTS = 3  # 1 次首发 + 2 次重试

# C-17：推理模型思考块（如 MiniMax-M2 的 `??…??`）不属回答正文，
# 不剥离则下游 JSON 解析节点全部 X-LLM-02。留痕 output_digest 仍取原始输出（诚实红线）。
_THINK_BLOCK_RE = re.compile(r"<think>[\s\S]*?</think>")


def strip_think_blocks(text: str) -> str:
    """剥离推理模型思考块；若正文为空则保留原文供节点提取完整 JSON。"""
    stripped = _THINK_BLOCK_RE.sub("", text).strip()
    return stripped or text.strip()


@dataclass(frozen=True)
class LlmResult:
    """单次调用成功结果。"""

    text: str
    tokens: int | None  # API 未返回 usage 时为 None（如实记录，不估算）
    call_id: str


class LlmClient:
    """GLM-5.3 调用客户端（每 job 一个实例，trace 追加到该 job 目录）。"""

    def __init__(
        self,
        settings: Settings,
        job_id: str,
        trace_path: Path,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_backoff: tuple[float, float] = DEFAULT_RETRY_BACKOFF,
    ) -> None:
        """transport 仅测试注入用（httpx.MockTransport），生产留 None。"""
        self._settings = settings
        self._job_id = job_id
        self._trace_path = trace_path
        self._transport = transport
        self._retry_backoff = retry_backoff
        self._seq = self._count_trace_lines() + 1

    async def complete(self, node: str, prompt: PromptSpec, user_content: str) -> LlmResult:
        """执行一次对话补全并留痕。

        Raises:
            PipelineError: X-LLM-01——重试耗尽仍失败（网络/超时类）。
        """
        call_id = f"LLM-{self._job_short()}-{self._seq:03d}"
        self._seq += 1
        started = time.monotonic()
        system = prompt.body
        sampling: dict[str, Any] = {
            "model": self._settings.llm_model_name,
            "temperature": self._settings.llm_temperature,
            "top_p": self._settings.llm_top_p,
            "seed": self._settings.llm_seed,
        }
        error: PipelineError | None = None
        text: str | None = None
        tokens: int | None = None
        try:
            text, tokens = await self._request(system, user_content)
        except PipelineError as exc:
            error = exc
        duration_ms = int((time.monotonic() - started) * 1000)
        self._append_trace(
            {
                "call_id": call_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "node": node,
                "prompt_file": prompt.ref,
                "prompt_version": prompt.version,
                "knowledge_refs": list(prompt.knowledge_refs),
                "sampling": sampling,
                "input_digest": _digest(system + "\x00" + user_content),
                "output_digest": _digest(text) if text is not None else None,
                "duration_ms": duration_ms,
                "tokens": tokens,
                "ok": error is None,
                # 失败原因（docs/12 字段清单外的补充：失败留痕不含原因则不可排障）
                "error": error.code if error else None,
            }
        )
        if error is not None:
            raise error
        if text is None:  # pragma: no cover —— ok 分支必有文本
            msg = "unreachable: 调用成功但无文本"
            raise RuntimeError(msg)
        # C-17：思考块剥离在留痕之后（digest 取原始输出，返回体取规范化正文）
        return LlmResult(text=strip_think_blocks(text), tokens=tokens, call_id=call_id)

    async def _request(self, system: str, user_content: str) -> tuple[str, int | None]:
        """带重试的 HTTP 调用；仅网络/超时类失败重试（docs/12 §三）。"""
        last_error: PipelineError | None = None
        for attempt in range(_MAX_ATTEMPTS):
            if attempt > 0:
                await asyncio.sleep(self._retry_backoff[attempt - 1])
            try:
                return await self._request_once(system, user_content)
            except _RetryableError as exc:
                last_error = exc.to_pipeline_error()
        if last_error is None:  # pragma: no cover —— 循环至少执行一次且必赋值
            msg = "unreachable: _MAX_ATTEMPTS >= 1"
            raise RuntimeError(msg)
        raise last_error

    async def _request_once(self, system: str, user_content: str) -> tuple[str, int | None]:
        """单次 HTTP 调用；网络/超时异常转可重试错误，其余按状态码分类。"""
        payload: dict[str, Any] = {
            "model": self._settings.llm_model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "temperature": self._settings.llm_temperature,
            "top_p": self._settings.llm_top_p,
            "max_tokens": self._settings.llm_max_tokens,
            **({"seed": self._settings.llm_seed} if self._settings.llm_seed is not None else {}),
        }
        headers = (
            {"Authorization": f"Bearer {self._settings.llm_api_key}"}
            if self._settings.llm_api_key
            else {}
        )
        async with httpx.AsyncClient(
            base_url=self._settings.llm_base_url,
            timeout=self._settings.llm_timeout_s,
            transport=self._transport,
        ) as client:
            try:
                response = await client.post("/chat/completions", json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # 网络类：可重试（X-LLM-01）。httpx 异常的 str 可能为空（如
                # ReadTimeout），须带异常类型名否则报错无法排障（P-16）。
                raise _RetryableError(
                    f"LLM 网络调用失败：{type(exc).__name__}: {exc}"
                ) from exc
        if response.status_code >= 500:
            # 网关 5xx 视为暂时性故障：可重试
            raise _RetryableError(f"LLM 服务端错误：HTTP {response.status_code}")
        if response.status_code != 200:
            # 4xx（鉴权/参数错误）重试无意义：直接失败（非表内场景以 X-LLM-01 记网络失败之外……
            # 遵守错误码总表纪律：LLM 段当前仅有 01/02/03，HTTP 4xx 归并 X-LLM-01 并在 message 注明
            raise PipelineError(
                code=X_LLM_01,
                message=f"LLM 调用被拒绝：HTTP {response.status_code}（重试无意义）",
            )
        data = response.json()
        try:
            text = str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise PipelineError(
                code=X_LLM_01,
                message=f"LLM 响应结构异常：{exc}",
            ) from exc
        usage = data.get("usage") or {}
        tokens = usage.get("total_tokens")
        return text, int(tokens) if isinstance(tokens, int) else None

    def _append_trace(self, record: dict[str, Any]) -> None:
        """追加一行 JSONL 留痕（禁止覆盖历史——文件以追加模式打开）。"""
        self._trace_path.parent.mkdir(parents=True, exist_ok=True)
        with self._trace_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _count_trace_lines(self) -> int:
        """已存在的留痕行数（进程重启后 call_id 序号接续，不重置）。"""
        if not self._trace_path.is_file():
            return 0
        with self._trace_path.open(encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    def _job_short(self) -> str:
        """job 短码：`job-` 前缀后的首段 UUID（样例口径 job-3f2a1b8c-… → 3f2a1b8c）。"""
        rest = self._job_id.removeprefix("job-")
        return (rest.split("-", 1)[0] or "unknown")[:8]


class _RetryableError(Exception):
    """可重试失败的内部载体（仅网络/超时/5xx）。"""

    def to_pipeline_error(self) -> PipelineError:
        """重试耗尽后对外抛出的 X-LLM-01。"""
        return PipelineError(code=X_LLM_01, message=str(self))


def _digest(text: str) -> str:
    """输入/输出摘要（sha256 前 16 位十六进制，留痕校验用）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
