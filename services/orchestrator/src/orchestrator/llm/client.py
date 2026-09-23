"""内部 LLM 中转站客户端：三种兼容协议调用 + 留痕 + 超时/重试治理。

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

# C-17：推理模型思考块不属回答正文，
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
    """中转站调用客户端（每 job 一个实例，trace 追加到该 job 目录）。"""

    def __init__(
        self,
        settings: Settings,
        job_id: str,
        trace_path: Path,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_backoff: tuple[float, float] = DEFAULT_RETRY_BACKOFF,
        run_id: str | None = None,
        max_attempts: int = _MAX_ATTEMPTS,
    ) -> None:
        """transport 仅测试注入用（httpx.MockTransport），生产留 None。"""
        self._settings = settings
        self._job_id = job_id
        self._trace_path = trace_path
        self._transport = transport
        self._retry_backoff = retry_backoff
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        self._max_attempts = max_attempts
        # 同一 job 可以多次断点/重跑；run_id 让前端和审计日志能区分每一轮。
        self._run_id = run_id
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
            "protocol": self._settings.llm_protocol,
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
                **({"run_id": self._run_id} if self._run_id else {}),
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
        for attempt in range(self._max_attempts):
            if attempt > 0:
                backoff_index = min(attempt - 1, len(self._retry_backoff) - 1)
                await asyncio.sleep(self._retry_backoff[backoff_index])
            try:
                return await self._request_once(system, user_content)
            except _RetryableError as exc:
                last_error = exc.to_pipeline_error()
        if last_error is None:  # pragma: no cover —— 循环至少执行一次且必赋值
            msg = "unreachable: max_attempts >= 1"
            raise RuntimeError(msg)
        raise last_error

    async def _request_once(self, system: str, user_content: str) -> tuple[str, int | None]:
        """单次 HTTP 调用；网络/超时异常转可重试错误，其余按状态码分类。"""
        endpoint, payload, headers = self._request_spec(system, user_content)
        async with httpx.AsyncClient(
            base_url=self._settings.llm_base_url,
            timeout=self._settings.llm_timeout_s,
            transport=self._transport,
        ) as client:
            try:
                response = await client.post(endpoint, json=payload, headers=headers)
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
            text = _extract_response_text(self._settings.llm_protocol, data)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise PipelineError(
                code=X_LLM_01,
                message=f"LLM 响应结构异常（{self._settings.llm_protocol}）：{exc}",
            ) from exc
        return text, _extract_total_tokens(data)

    def _request_spec(
        self, system: str, user_content: str
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        """按配置协议构造端点、请求体与鉴权头。

        OpenAI Responses 的最小兼容格式只要求 ``input`` 字符串，故将系统提示与
        用户输入明确拼接，避免不同中转实现对 role/content 数组支持不一致。
        """
        api_key = self._settings.llm_api_key
        protocol = self._settings.llm_protocol
        if protocol == "openai-responses":
            payload: dict[str, Any] = {
                "model": self._settings.llm_model_name,
                "input": f"{system}\n\n--- 用户输入 ---\n{user_content}",
                "max_output_tokens": self._settings.llm_max_tokens,
            }
            return (
                "/responses",
                payload,
                {"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
        if protocol == "anthropic-messages":
            payload = {
                "model": self._settings.llm_model_name,
                "max_tokens": self._settings.llm_max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user_content}],
            }
            return "/messages", payload, {"x-api-key": api_key} if api_key else {}

        payload = {
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
        return (
            "/chat/completions",
            payload,
            {"Authorization": f"Bearer {api_key}"} if api_key else {},
        )

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


def _extract_response_text(protocol: str, data: Any) -> str:
    """从三种协议的成功响应中提取非空文本，拒绝静默吞掉结构变化。"""
    if not isinstance(data, dict):
        raise ValueError("响应根节点不是对象")
    if protocol == "openai-chat":
        text = data["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("choices[0].message.content 为空")
        return text
    if protocol == "openai-responses":
        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text
        fragments: list[str] = []
        for output in data.get("output", []):
            if not isinstance(output, dict):
                continue
            for content in output.get("content", []):
                if not isinstance(content, dict):
                    continue
                text = content.get("text")
                if isinstance(text, str) and text:
                    fragments.append(text)
        if fragments:
            return "".join(fragments)
        raise ValueError("缺少 output_text 或 output[].content[].text")
    if protocol == "anthropic-messages":
        fragments = [
            item["text"]
            for item in data.get("content", [])
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
            and item["text"]
        ]
        if fragments:
            return "".join(fragments)
        raise ValueError("缺少 content[].text")
    raise ValueError(f"不支持的协议：{protocol}")


def _extract_total_tokens(data: Any) -> int | None:
    """各网关 usage 命名不同，只使用原始计数，不推估。"""
    if not isinstance(data, dict) or not isinstance(data.get("usage"), dict):
        return None
    usage = data["usage"]
    total = usage.get("total_tokens") or usage.get("total")
    if isinstance(total, int):
        return total
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
        return input_tokens + output_tokens
    return None


def _digest(text: str) -> str:
    """输入/输出摘要（sha256 前 16 位十六进制，留痕校验用）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
