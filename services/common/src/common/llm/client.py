"""内部 LLM 中转站客户端：三种兼容协议调用 + 留痕 + 超时/重试治理。

口径全部来自 docs/12 第三章：
- 每次调用向 llm_calls.jsonl 追加一行留痕（call_id 格式 LLM-<job 短码>-<序号>）；
- 超时算调用失败；仅网络/超时/5xx/429 类失败重试，最多 2 次、指数退避 5s/20s
  （429 带 Retry-After 时以网关指示为准）；
- 采样参数取 Settings 实际值（模板建议值仅供参考，禁止硬编码）；
- token 数取 API 响应原始值，不做换算修饰。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from common.config import Settings
from common.errors import X_LLM_01, X_LLM_02, PipelineError
from common.prompts_loader import PromptSpec

# docs/12 §三：重试退避间隔（秒）；测试注入 0 加速
DEFAULT_RETRY_BACKOFF: tuple[float, float] = (5.0, 20.0)
_MAX_ATTEMPTS = 3  # 1 次首发 + 2 次重试
# 429 的 Retry-After 上限：防止网关给出异常大值把节点长时间挂在 sleep 上
_MAX_RETRY_AFTER_S = 60.0
_CONTINUE_INSTRUCTION = (
    "上一条 assistant 内容因单次输出长度限制而中断。请从它的最后一个字符之后继续，"
    "让新内容可以直接拼接到该内容后成为原始任务的完整输出。不要重述、删改、总结或跳过前文，"
    "不要添加续接说明、代码围栏或其它标记。"
)

# C-17：推理模型思考块不属回答正文，
# 不剥离则下游 JSON 解析节点全部 X-LLM-02。留痕 output_digest 仍取原始输出（诚实红线）。
_THINK_BLOCK_RE = re.compile(r"<think>[\s\S]*?</think>")

# HTTP 头值必须是可 ASCII 编码的可打印字符。任务配置里若把中文提示语误粘进
# API Key（如套餐到期提示、服务重启说明），httpx 会在编码请求头时抛
# UnicodeEncodeError——它不属于 httpx 网络异常族，会穿透 LlmClient 的重试与错误码
# 分类，直达编排层兜底 except，前端只看到笼统的「后台运行异常」。此处提前拦截。
_HEADER_UNSAFE_RE = re.compile(r"[^\x21-\x7e]")


def strip_think_blocks(text: str) -> str:
    """剥离推理模型思考块；若正文为空则保留原文供节点提取完整 JSON。"""
    stripped = _THINK_BLOCK_RE.sub("", text).strip()
    return stripped or text.strip()


def _parse_retry_after(raw: str | None) -> float | None:
    """解析 ``Retry-After`` 头为秒数；无法识别时返回 None（退回本地退避表）。

    支持两种标准形式：秒数（``"30"``）和 HTTP 日期（``"Wed, 21 Oct 2026 07:28:00 GMT"``）。
    上限截断到 ``_MAX_RETRY_AFTER_S``：恶意或异常的超大值（如 ``"999999"``）会把
    节点挂在 sleep 上，反而比失败更糟。
    """
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    seconds: float | None = None
    if value.isdigit():
        seconds = float(value)
    else:
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if target is None:  # pragma: no cover —— 解析失败按无值处理
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        seconds = (target - datetime.now(UTC)).total_seconds()
    if seconds is None:
        return None
    return max(0.0, min(seconds, _MAX_RETRY_AFTER_S))


def ensure_header_safe_api_key(api_key: str) -> None:
    """校验 API Key 可作 HTTP 头值；不可用时抛 X-LLM-01，且不回显密钥内容。

    密钥含非 ASCII 是确定性配置错误，重试无意义——与 4xx 分支同类，故直接抛
    ``PipelineError`` 而非可重试载体，避免白等退避与重复计费。
    """
    if api_key and _HEADER_UNSAFE_RE.search(api_key):
        raise PipelineError(
            code=X_LLM_01,
            message=(
                "LLM 中转站 API Key 含非 ASCII 或不可见字符（常见于误粘贴中文提示语），"
                "无法作为 HTTP 请求头发送；请在任务配置中重新填写中转站密钥"
            ),
        )


def describe_transport_error(exc: httpx.TimeoutException | httpx.TransportError) -> str:
    """将底层网络异常转成可行动、且不泄露地址或凭据的中文说明。

    ``str(exc)`` 在不少超时场景为空；仅展示类型名又不足以让使用者区分网关
    不可达、网关处理缓慢和本机证书问题。这里刻意不拼接 URL、请求头或响应正文，避免
    错误消息进入任务事件/页面后扩大内部拓扑和凭据暴露面。
    """
    exception_name = type(exc).__name__
    detail = str(exc).strip()
    suffix = f"：{detail}" if detail else ""

    if isinstance(exc, httpx.ConnectTimeout):
        return (
            f"LLM 中转站连接超时（{exception_name}）：部署服务器无法建立到网关的 HTTPS 连接，"
            "请检查服务器到网关的路由、防火墙或代理策略"
            f"{suffix}"
        )
    if isinstance(exc, httpx.ReadTimeout):
        return (
            f"LLM 中转站响应超时（{exception_name}）：连接已发出但未在时限内返回，"
            "请检查网关负载/限流，或改用更快模型后重试"
            f"{suffix}"
        )
    if isinstance(exc, httpx.WriteTimeout):
        return f"LLM 中转站请求发送超时（{exception_name}）：请检查服务器出口网络或代理策略{suffix}"
    if isinstance(exc, httpx.ProxyError):
        return f"LLM 中转站代理连接失败（{exception_name}）：请检查代理地址、凭据和网络策略{suffix}"
    if isinstance(exc, httpx.ConnectError):
        return (
            f"LLM 中转站连接失败（{exception_name}）：请检查网关地址、DNS、证书链和防火墙策略"
            f"{suffix}"
        )
    if isinstance(exc, httpx.RemoteProtocolError):
        return (
            f"LLM 中转站协议异常（{exception_name}）：请确认所选 OpenAI/Anthropic 协议与网关一致"
            f"{suffix}"
        )
    return f"LLM 网络调用失败（{exception_name}）{suffix}"


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

    async def complete(
        self,
        node: str,
        prompt: PromptSpec,
        user_content: str,
        max_attempts: int | None = None,
    ) -> LlmResult:
        """执行一次对话补全并留痕。

        Args:
            max_attempts: 覆盖本实例的重试上限；None 表示沿用实例默认值。
                供「失败可优雅退化」的子调用收窄最坏等待——例如测试点拆分维度补全
                失败即退回模板 hints，非正确性问题，却会按默认 3 次尝试白等
                3×超时+25s 退避。docs/12 §三 口径是「最多 2 次重试」（上限），
                取更小值仍在该口径内。

        Raises:
            PipelineError: 网络/超时类重试耗尽，或响应无法确认完整。
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
        finish_reason: str | None = None
        continuation_rounds = 0
        try:
            text, tokens, finish_reason, continuation_rounds = await self._request(
                system, user_content, max_attempts
            )
        except PipelineError as exc:
            error = exc
        duration_ms = int((time.monotonic() - started) * 1000)
        payload_file = self._persist_payload(call_id, system, user_content, text)
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
                "input_chars": len(system) + len(user_content),
                "output_digest": _digest(text) if text is not None else None,
                "output_chars": len(text) if text is not None else None,
                "payload_file": payload_file,
                "finish_reason": finish_reason,
                "continuation_rounds": continuation_rounds,
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

    async def _request(
        self, system: str, user_content: str, max_attempts: int | None = None
    ) -> tuple[str, int | None, str | None, int]:
        """完整获取响应；遇到输出 token 上限时续接并无损拼回正文。"""
        attempts = self._max_attempts if max_attempts is None else max_attempts
        if attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        output_parts: list[str] = []
        total_tokens = 0
        usage_complete = True
        continuation_rounds = 0
        finish_reason: str | None = None
        while True:
            partial_output = "".join(output_parts)
            text, tokens, finish_reason = await self._request_with_retries(
                system, user_content, partial_output if continuation_rounds else None, attempts
            )
            if tokens is None:
                usage_complete = False
            else:
                total_tokens += tokens
            if _is_output_length_limited(finish_reason):
                delta = _continuation_delta(partial_output, text)
                if not delta:
                    raise PipelineError(
                        code=X_LLM_02,
                        message="LLM 输出达到单次上限且续接没有新增内容，无法组成完整响应",
                    )
                output_parts.append(delta)
                continuation_rounds += 1
                continue
            if not _is_response_complete(self._settings.llm_protocol, finish_reason):
                raise PipelineError(
                    code=X_LLM_02,
                    message=(
                        "LLM 响应没有提供可确认的正常结束标记"
                        f"（protocol={self._settings.llm_protocol}, finish_reason={finish_reason!r}）"
                    ),
                )
            final_delta = _continuation_delta(partial_output, text) if continuation_rounds else text
            if continuation_rounds and not final_delta:
                raise PipelineError(
                    code=X_LLM_02,
                    message="LLM 续接响应没有新增内容，无法确认返回正文完整",
                )
            output_parts.append(final_delta)
            complete_text = "".join(output_parts)
            return (
                complete_text,
                total_tokens if usage_complete else None,
                finish_reason,
                continuation_rounds,
            )

    async def _request_with_retries(
        self,
        system: str,
        user_content: str,
        partial_output: str | None,
        attempts: int,
    ) -> tuple[str, int | None, str | None]:
        """单个完整或续接请求的网络重试。"""
        last_error: PipelineError | None = None
        retry_after_s: float | None = None
        for attempt in range(attempts):
            if attempt > 0:
                backoff_index = min(attempt - 1, len(self._retry_backoff) - 1)
                delay = max(self._retry_backoff[backoff_index], retry_after_s or 0.0)
                await asyncio.sleep(delay)
            try:
                return await self._request_once(system, user_content, partial_output)
            except _RetryableError as exc:
                last_error = exc.to_pipeline_error()
                retry_after_s = exc.retry_after_s
        if last_error is None:  # pragma: no cover
            raise RuntimeError("unreachable: max_attempts >= 1")
        raise last_error

    async def _request_once(
        self, system: str, user_content: str, partial_output: str | None = None
    ) -> tuple[str, int | None, str | None]:
        """单次 HTTP 调用；网络/超时异常转可重试错误，其余按状态码分类。"""
        # 配置类错误在建连接前拦下：非 ASCII 密钥会让 httpx 抛 UnicodeEncodeError，
        # 该异常不在下方 except 的捕获范围内，会绕过节点级重试直达编排层兜底。
        ensure_header_safe_api_key(self._settings.llm_api_key)
        endpoint, payload, headers = self._request_spec(system, user_content, partial_output)
        async with httpx.AsyncClient(
            base_url=self._settings.llm_base_url,
            timeout=self._settings.llm_timeout_s,
            transport=self._transport,
        ) as client:
            try:
                response = await client.post(endpoint, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # 网络类：可重试（X-LLM-01）。底层异常可能无 message，故以稳定分类和
                # 可行动提示输出；禁止带 URL、请求头或正文，避免泄露内部拓扑/凭据。
                raise _RetryableError(describe_transport_error(exc)) from exc
        if response.status_code >= 500:
            # 网关 5xx 视为暂时性故障：可重试
            raise _RetryableError(f"LLM 服务端错误：HTTP {response.status_code}")
        if response.status_code == 429:
            # 限流是暂时性拥塞，不是配置错误：可重试。此前归入 4xx「重试无意义」分支，
            # 一次 429 即判死节点——而 testcase_generate 等节点是并发多路打网关，
            # 被限流是常态，直接失败会把可自愈的拥塞固化成任务失败。
            raise _RetryableError(
                "LLM 中转站限流：HTTP 429（已等待后重试）",
                retry_after_s=_parse_retry_after(response.headers.get("Retry-After")),
            )
        if response.status_code != 200:
            # 4xx（鉴权/参数错误）重试无意义：直接失败（非表内场景以 X-LLM-01 记网络失败之外……
            # 遵守错误码总表纪律：LLM 段当前仅有 01/02/03，HTTP 4xx 归并 X-LLM-01 并在 message 注明
            raise PipelineError(
                code=X_LLM_01,
                message=f"LLM 调用被拒绝：HTTP {response.status_code}（重试无意义）",
            )
        try:
            data = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _RetryableError("LLM 响应体不完整或无法解析，正在重试完整请求") from exc
        try:
            text = _extract_response_text(self._settings.llm_protocol, data)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise PipelineError(
                code=X_LLM_01,
                message=f"LLM 响应结构异常（{self._settings.llm_protocol}）：{exc}",
            ) from exc
        return (
            text,
            _extract_total_tokens(data),
            _extract_finish_reason(self._settings.llm_protocol, data),
        )

    def _request_spec(
        self, system: str, user_content: str, partial_output: str | None = None
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        """按配置协议构造端点、请求体与鉴权头。

        OpenAI Responses 的最小兼容格式只要求 ``input`` 字符串，故将系统提示与
        用户输入明确拼接，避免不同中转实现对 role/content 数组支持不一致。
        """
        api_key = self._settings.llm_api_key
        protocol = self._settings.llm_protocol
        if protocol == "openai-responses":
            full_input = f"{system}\n\n--- 用户输入 ---\n{user_content}"
            if partial_output is not None:
                full_input += (
                    f"\n\n--- 已生成的 assistant 内容（续接位置在末尾）---\n{partial_output}"
                    f"\n\n--- 续接要求 ---\n{_CONTINUE_INSTRUCTION}"
                )
            payload: dict[str, Any] = {
                "model": self._settings.llm_model_name,
                "input": full_input,
                "max_output_tokens": self._settings.llm_max_tokens,
            }
            return (
                "/responses",
                payload,
                {"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
        if protocol == "anthropic-messages":
            anthropic_messages: list[dict[str, str]] = [{"role": "user", "content": user_content}]
            if partial_output is not None:
                anthropic_messages.extend(
                    [
                        {"role": "assistant", "content": partial_output},
                        {"role": "user", "content": _CONTINUE_INSTRUCTION},
                    ]
                )
            payload = {
                "model": self._settings.llm_model_name,
                "max_tokens": self._settings.llm_max_tokens,
                "system": system,
                "messages": anthropic_messages,
            }
            return "/messages", payload, {"x-api-key": api_key} if api_key else {}

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        if partial_output is not None:
            messages.extend(
                [
                    {"role": "assistant", "content": partial_output},
                    {"role": "user", "content": _CONTINUE_INSTRUCTION},
                ]
            )
        payload = {
            "model": self._settings.llm_model_name,
            "messages": messages,
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

    def _persist_payload(
        self, call_id: str, system: str, user_content: str, output: str | None
    ) -> str:
        """保存完整输入和原始正文供离线复现；不保存 HTTP 鉴权头。"""
        directory = self._trace_path.parent / "llm_payloads"
        directory.mkdir(parents=True, exist_ok=True)
        # 临时文件以 0600 权限创建；即使写完正文后进程在写摘要前退出，
        # 下一次调用复用序号也不会覆盖这一份原始记录。
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=f"{call_id}-",
            suffix=".json",
            delete=False,
        ) as output_file:
            json.dump(
                {"system": system, "user": user_content, "output": output},
                output_file,
                ensure_ascii=False,
            )
            path = Path(output_file.name)
        return path.relative_to(self._trace_path.parent).as_posix()

    def _append_trace(self, record: dict[str, Any]) -> None:
        """追加一行 JSONL 留痕（禁止覆盖历史——文件以追加模式打开）。"""
        self._trace_path.parent.mkdir(parents=True, exist_ok=True)
        with self._trace_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _count_trace_lines(self) -> int:
        """接续已分配的最大序号；并行任务取消留下的空号不能导致调用编号复用。"""
        if not self._trace_path.is_file():
            return 0
        count = 0
        maximum = 0
        with self._trace_path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                count += 1
                try:
                    maximum = max(maximum, int(json.loads(line)["call_id"].rsplit("-", 1)[1]))
                except (ValueError, KeyError, TypeError):
                    continue
        return max(count, maximum)

    def _job_short(self) -> str:
        """job 短码：`job-` 前缀后的首段 UUID（样例口径 job-3f2a1b8c-… → 3f2a1b8c）。"""
        rest = self._job_id.removeprefix("job-")
        return (rest.split("-", 1)[0] or "unknown")[:8]


class _RetryableError(Exception):
    """可重试失败的内部载体（网络/超时/5xx/429）。

    ``retry_after_s`` 为服务端明确要求的退避秒数（``Retry-After`` 头）。有值时优先于
    本地退避表——限流场景下按网关指示等待比盲目指数退避更快恢复，也避免继续加压。
    """

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s

    def to_pipeline_error(self) -> PipelineError:
        """重试耗尽后对外抛出的 X-LLM-01。"""
        return PipelineError(code=X_LLM_01, message=str(self))


def _extract_response_text(protocol: str, data: Any) -> str:
    """从三种协议的成功响应中提取非空文本，拒绝静默吞掉结构变化。"""
    if not isinstance(data, dict):
        raise ValueError("响应根节点不是对象")
    if protocol == "openai-chat":
        message = data["choices"][0]["message"]
        text = message.get("content")
        if isinstance(text, str) and text.strip():
            return text
        if isinstance(text, list):
            text_blocks = [
                str(item["text"])
                for item in text
                if isinstance(item, dict)
                and item.get("type") in {"text", "output_text"}
                and isinstance(item.get("text"), str)
            ]
            if text_blocks:
                return "".join(text_blocks)
        # C-17：DeepSeek Reasoner 等推理模型可能把正文放在 reasoning_content，
        # content 为空（思考通道与正文通道分离）；此时回退 reasoning_content，
        # 避免把可用正文当成失败丢弃。仍以非空为下限，不静默吞掉结构变化。
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning
        raise ValueError("choices[0].message.content 为空")
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


def _extract_finish_reason(protocol: str, data: Any) -> str | None:
    """读取各协议完成标记；调用端要求该标记确认响应正常结束。"""
    if not isinstance(data, dict):
        return None
    if protocol == "openai-chat":
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            reason = choices[0].get("finish_reason")
            return str(reason) if reason is not None else None
    elif protocol == "openai-responses":
        status = data.get("status")
        details = data.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        if status == "incomplete":
            return f"incomplete:{reason or 'unknown'}"
        if status is not None:
            return str(status)
    elif protocol == "anthropic-messages":
        reason = data.get("stop_reason")
        return str(reason) if reason is not None else None
    return None


def _is_output_length_limited(reason: str | None) -> bool:
    """识别模型因单次输出 token 上限停止的协议标记。"""
    normalized = (reason or "").lower()
    return normalized in {"length", "max_tokens", "max_output_tokens"} or normalized in {
        "incomplete:max_output_tokens",
        "incomplete:max_tokens",
    }


def _is_response_complete(protocol: str, reason: str | None) -> bool:
    """仅接受协议明确标记为正常结束的响应；未知状态不能作为完整返回。"""
    if protocol == "openai-chat":
        return reason == "stop"
    if protocol == "openai-responses":
        return reason == "completed"
    if protocol == "anthropic-messages":
        return reason in {"end_turn", "stop_sequence"}
    return False


def _continuation_delta(previous: str, current: str) -> str:
    """剔除续接模型重复的边界文本，其余字符原样保留并拼接。"""
    if not previous:
        return current
    if current.startswith(previous):
        return current[len(previous) :]
    # 不猜测并删除部分重叠：边界重复可能与原文相同字符，误删会造成不可见截断。
    return current


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
