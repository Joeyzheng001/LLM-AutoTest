"""LLM 客户端测试：留痕、重试治理、错误分类（MockTransport，不真调模型）。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from common.config import Settings
from common.errors import PipelineError
from common.llm import LlmClient
from common.prompts_loader import PromptSpec

_PROMPT = PromptSpec(
    name="req_parse",
    version="1.0",
    spec="F-REQ-01",
    owner="甲",
    changelog=("1.0 | 2026-09-15 | 初版",),
    sampling={"temperature": 0.2, "top_p": 0.8, "seed": None},
    body="你是需求解析器。",
)


def _settings(protocol: str = "openai-chat") -> Settings:
    """测试用配置（Mock 场景无鉴权）。"""
    return Settings.from_env(
        {
            "JAVA_PARSER_URL": "http://java-parser:8080",
            "LLM_BASE_URL": "http://llm-mock",
            "LLM_TIMEOUT_S": "5",
            "LLM_PROTOCOL": protocol,
            "LLM_API_KEY": "sk-test",
        }
    )


def _ok_response(request: httpx.Request) -> httpx.Response:
    """标准成功响应（含 usage）。"""
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "解析结果"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 128},
        },
    )


async def test_complete_success_writes_trace(tmp_path: Path) -> None:
    """成功调用：返回文本与 token 原始值，留痕字段齐全。"""
    trace = tmp_path / "job-abc12345-0" / "llm_calls.jsonl"
    client = LlmClient(
        _settings(), "job-abc12345-def-6", trace, transport=httpx.MockTransport(_ok_response)
    )
    result = await client.complete("req_parse", _PROMPT, "需求文档内容")
    assert result.text == "解析结果"
    assert result.tokens == 128
    assert result.call_id == "LLM-abc12345-001"

    lines = trace.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["call_id"] == "LLM-abc12345-001"
    assert record["node"] == "req_parse"
    assert record["prompt_file"] == "req_parse@1.0"
    assert record["knowledge_refs"] == []
    assert record["prompt_version"] == "1.0"
    assert record["sampling"]["model"] == "deepseek-reasoner-code"
    assert record["sampling"]["protocol"] == "openai-chat"
    assert record["tokens"] == 128
    assert record["ok"] is True
    assert record["input_digest"] and record["output_digest"]
    assert record["timestamp"].endswith("+00:00")  # UTC ISO-8601
    payload = json.loads((trace.parent / record["payload_file"]).read_text(encoding="utf-8"))
    assert payload == {"system": _PROMPT.body, "user": "需求文档内容", "output": "解析结果"}
    assert (trace.parent / record["payload_file"]).stat().st_mode & 0o077 == 0


async def test_complete_keeps_full_unicode_payload_for_offline_replay(tmp_path: Path) -> None:
    """完整传输与原文留痕一致，大输入和返回不能只保存摘要或截取部分。"""
    user_text = "接口源码与证据\n" * 20000
    output_text = '{"items":[' + ",".join('"完整中文返回"' for _ in range(20000)) + "]}"

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["messages"][-1]["content"] == user_text
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": output_text}, "finish_reason": "stop"}],
            },
        )

    trace = tmp_path / "llm_calls.jsonl"
    client = LlmClient(_settings(), "job-replay", trace, transport=httpx.MockTransport(handler))
    result = await client.complete("code_review", _PROMPT, user_text)
    record = json.loads(trace.read_text(encoding="utf-8"))
    payload = json.loads((trace.parent / record["payload_file"]).read_text(encoding="utf-8"))
    assert result.text == output_text
    assert payload["user"] == user_text
    assert payload["output"] == output_text


async def test_retry_on_5xx_then_success(tmp_path: Path) -> None:
    """5xx 视为暂时性故障：重试 2 次内成功则调用成功。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    trace = tmp_path / "job-abc12345-0" / "llm_calls.jsonl"
    client = LlmClient(
        _settings(),
        "job-abc12345-def-6",
        trace,
        transport=httpx.MockTransport(handler),
        retry_backoff=(0.0, 0.0),
    )
    result = await client.complete("req_parse", _PROMPT, "内容")
    assert result.text == "ok"
    assert len(calls) == 3  # 首发 + 2 次重试
    record = json.loads(trace.read_text(encoding="utf-8").strip())
    assert record["ok"] is True


async def test_retry_exhausted_raises_x_llm_01(tmp_path: Path) -> None:
    """网络失败重试耗尽 → X-LLM-01；失败也留痕（ok=false + error 码）。"""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("connection refused")

    trace = tmp_path / "job-abc12345-0" / "llm_calls.jsonl"
    client = LlmClient(
        _settings(),
        "job-abc12345-def-6",
        trace,
        transport=httpx.MockTransport(handler),
        retry_backoff=(0.0, 0.0),
    )
    with pytest.raises(PipelineError) as excinfo:
        await client.complete("req_parse", _PROMPT, "内容")
    assert excinfo.value.code == "X-LLM-01"
    assert len(attempts) == 3
    record = json.loads(trace.read_text(encoding="utf-8").strip())
    assert record["ok"] is False
    assert record["error"] == "X-LLM-01"
    assert record["output_digest"] is None


async def test_timeout_error_message_has_exception_type(tmp_path: Path) -> None:
    """P-16：httpx 超时异常 str 为空——报错须带异常类型名否则无法排障。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("")  # 真实场景：str(exc) 为空字符串

    trace = tmp_path / "job-abc12345-0" / "llm_calls.jsonl"
    client = LlmClient(
        _settings(),
        "job-abc12345-def-6",
        trace,
        transport=httpx.MockTransport(handler),
        retry_backoff=(0.0, 0.0),
    )
    with pytest.raises(PipelineError) as excinfo:
        await client.complete("req_parse", _PROMPT, "内容")
    assert excinfo.value.code == "X-LLM-01"
    assert "ReadTimeout" in excinfo.value.message  # 类型名在场，不再只剩冒号
    assert "响应超时" in excinfo.value.message


async def test_connect_timeout_explains_gateway_network_boundary(tmp_path: Path) -> None:
    """配置/任务失败须区分“网关不可达”与模型返回慢，且不泄露目标 URL。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("")

    client = LlmClient(
        _settings(),
        "job-connect-timeout",
        tmp_path / "llm_calls.jsonl",
        transport=httpx.MockTransport(handler),
        retry_backoff=(0, 0),
        max_attempts=1,
    )
    with pytest.raises(PipelineError) as excinfo:
        await client.complete("req_parse", _PROMPT, "内容")
    assert excinfo.value.code == "X-LLM-01"
    assert "ConnectTimeout" in excinfo.value.message
    assert "连接超时" in excinfo.value.message
    assert "路由、防火墙或代理策略" in excinfo.value.message
    assert "example.test" not in excinfo.value.message


async def test_429_is_retryable_then_succeeds(tmp_path: Path) -> None:
    """429 是暂时性拥塞而非配置错误：应重试，不该一次判死节点。

    回归背景：429 曾被并入 4xx「重试无意义」分支，一次限流即让 testcase_generate
    节点失败——而该节点本就并发多路打网关，被限流是常态，直接失败把可自愈的拥塞
    固化成任务失败（前端持续展示 X-LLM-01 / HTTP 429）。
    """
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    trace = tmp_path / "job-abc12345-0" / "llm_calls.jsonl"
    client = LlmClient(
        _settings(),
        "job-abc12345-def-6",
        trace,
        transport=httpx.MockTransport(handler),
        retry_backoff=(0.0, 0.0),
    )
    result = await client.complete("req_parse", _PROMPT, "内容")
    assert result.text == "ok"
    assert len(attempts) == 3  # 首发 + 2 次重试
    assert json.loads(trace.read_text(encoding="utf-8").strip())["ok"] is True


async def test_429_exhausted_mentions_throttling(tmp_path: Path) -> None:
    """持续 429 重试耗尽后仍失败，但报错须点明「限流」而非笼统的「被拒绝」。"""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(429, headers={"Retry-After": "0"})

    trace = tmp_path / "job-abc12345-0" / "llm_calls.jsonl"
    client = LlmClient(
        _settings(),
        "job-abc12345-def-6",
        trace,
        transport=httpx.MockTransport(handler),
        retry_backoff=(0.0, 0.0),
    )
    with pytest.raises(PipelineError) as excinfo:
        await client.complete("req_parse", _PROMPT, "内容")
    assert excinfo.value.code == "X-LLM-01"
    assert len(attempts) == 3  # 限流走了完整重试，不是单次判死
    assert "限流" in excinfo.value.message
    assert "429" in excinfo.value.message


def test_parse_retry_after_handles_seconds_dates_and_garbage() -> None:
    """Retry-After 解析：秒数、HTTP 日期、缺失、非法值，且超大值须截断。"""
    from common.llm.client import _MAX_RETRY_AFTER_S, _parse_retry_after

    assert _parse_retry_after("30") == 30.0
    assert _parse_retry_after("0") == 0.0
    # 超大值截断，避免节点长时间挂在 sleep 上
    assert _parse_retry_after("999999") == _MAX_RETRY_AFTER_S
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("not-a-value") is None
    # HTTP 日期形式
    future = _parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert future is not None and future > 0


async def test_4xx_no_retry(tmp_path: Path) -> None:
    """4xx（鉴权/参数错误）重试无意义：单次即失败。"""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401)

    trace = tmp_path / "job-abc12345-0" / "llm_calls.jsonl"
    client = LlmClient(
        _settings(),
        "job-abc12345-def-6",
        trace,
        transport=httpx.MockTransport(handler),
        retry_backoff=(0.0, 0.0),
    )
    with pytest.raises(PipelineError) as excinfo:
        await client.complete("req_parse", _PROMPT, "内容")
    assert excinfo.value.code == "X-LLM-01"
    assert len(attempts) == 1


async def test_non_ascii_api_key_fails_fast_without_http_call(tmp_path: Path) -> None:
    """误把中文提示语粘进密钥框：建连接前即抛 X-LLM-01，不裸抛 UnicodeEncodeError。

    回归背景：httpx 编码请求头时对非 ASCII 值抛 UnicodeEncodeError，该异常不属于
    网络异常族，会穿透重试与错误码分类直达编排层兜底 except——前端只看到笼统的
    「后台运行异常，见服务日志」，节点状态停留在 running 且无重试。
    """
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover —— 不该被调用
        calls.append(1)
        return httpx.Response(200)

    bad = _settings()
    bad = replace(bad, llm_api_key="服务重启导致任务中断；已完成产物保留，可从该节点继续重跑")
    trace = tmp_path / "job-abc12345-0" / "llm_calls.jsonl"
    client = LlmClient(
        bad,
        "job-abc12345-def-6",
        trace,
        transport=httpx.MockTransport(handler),
        retry_backoff=(0.0, 0.0),
    )
    with pytest.raises(PipelineError) as excinfo:
        await client.complete("req_parse", _PROMPT, "内容")
    assert excinfo.value.code == "X-LLM-01"
    # 配置类错误重试无意义：不应发生任何 HTTP 调用
    assert calls == []
    # 密钥内容不得回显（凭据不得进日志/响应）
    assert "服务重启" not in excinfo.value.message
    # 失败仍留痕，便于排障
    record = json.loads(trace.read_text(encoding="utf-8").strip())
    assert record["ok"] is False
    assert record["error"] == "X-LLM-01"


async def test_call_id_continues_after_restart(tmp_path: Path) -> None:
    """进程重启（新实例）后 call_id 序号接续不重置。"""
    trace = tmp_path / "job-abc12345-0" / "llm_calls.jsonl"
    for _ in range(2):
        client = LlmClient(
            _settings(), "job-abc12345-def-6", trace, transport=httpx.MockTransport(_ok_response)
        )
        await client.complete("req_parse", _PROMPT, "内容")
    ids = [json.loads(line)["call_id"] for line in trace.read_text(encoding="utf-8").splitlines()]
    assert ids == ["LLM-abc12345-001", "LLM-abc12345-002"]


async def test_think_blocks_stripped(tmp_path: Path) -> None:
    """C-17：推理模型思考块（<think>…</think>）被剥离；留痕 digest 取原始输出。"""
    raw = '<think>\n思考过程不应进入解析\n</think>\n\n```json\n{"cases": []}\n```'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": raw}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 64},
            },
        )

    trace = tmp_path / "job-abc12345-0" / "llm_calls.jsonl"
    client = LlmClient(
        _settings(), "job-abc12345-def-6", trace, transport=httpx.MockTransport(handler)
    )
    result = await client.complete("req_parse", _PROMPT, "内容")
    # 思考块已剥离，仅剩正文（代码围栏由节点层 strip_code_fence 处理）
    assert result.text == '```json\n{"cases": []}\n```'
    # 诚实红线：留痕 output_digest 取原始输出（含思考块）
    import hashlib

    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    record = json.loads(trace.read_text(encoding="utf-8").strip())
    assert record["output_digest"] == expected


async def test_openai_responses_protocol_extracts_output_text_and_usage(tmp_path: Path) -> None:
    """OpenAI Responses：使用 Bearer、/responses，并读取 output_text 与输入/输出 token。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/responses"
        assert request.headers["authorization"] == "Bearer sk-test"
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-reasoner-code"
        assert isinstance(payload["input"], str)
        return httpx.Response(
            200,
            json={
                "output_text": "Responses 结果",
                "status": "completed",
                "usage": {"input_tokens": 7, "output_tokens": 11},
            },
        )

    client = LlmClient(
        _settings("openai-responses"),
        "job-abc12345-def-6",
        tmp_path / "trace.jsonl",
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete("req_parse", _PROMPT, "需求")
    assert result.text == "Responses 结果"
    assert result.tokens == 18


async def test_anthropic_messages_protocol_extracts_content_and_usage(tmp_path: Path) -> None:
    """Anthropic Messages：使用 x-api-key、/messages，并读取 content[].text。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/messages"
        assert request.headers["x-api-key"] == "sk-test"
        assert "authorization" not in request.headers
        payload = json.loads(request.content)
        assert payload["system"] == _PROMPT.body
        assert payload["messages"] == [{"role": "user", "content": "需求"}]
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "Anthropic 结果"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 13, "output_tokens": 17},
            },
        )

    client = LlmClient(
        _settings("anthropic-messages"),
        "job-abc12345-def-6",
        tmp_path / "trace.jsonl",
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete("req_parse", _PROMPT, "需求")
    assert result.text == "Anthropic 结果"
    assert result.tokens == 30


def test_strip_think_blocks_variants() -> None:
    """无标记原样返回；不闭合标记不静默截断（交由下游解析报 X-LLM-02）。"""
    from common.llm.client import strip_think_blocks

    assert strip_think_blocks("纯正文") == "纯正文"
    assert strip_think_blocks("<think>只有开头不闭合") == "<think>只有开头不闭合"
    assert strip_think_blocks("<think>a</think> 前文 <think>b</think> 后文") == "前文  后文"
    assert strip_think_blocks('<think>{"cases": []}</think>') == '<think>{"cases": []}</think>'


async def test_reasoner_content_falls_back_to_reasoning_content(tmp_path: Path) -> None:
    """推理模型 content 为空时回退 reasoning_content，避免误报内容缺失。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "", "reasoning_content": "推理通道正文"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": 64},
            },
        )

    client = LlmClient(
        _settings(),
        "job-abc12345-def-6",
        tmp_path / "trace.jsonl",
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete("req_parse", _PROMPT, "需求")
    assert result.text == "推理通道正文"


async def test_reasoner_empty_both_channels_still_fails(tmp_path: Path) -> None:
    """content 与 reasoning_content 都为空时仍按结构异常失败（不静默吞）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": None, "reasoning_content": ""}}]},
        )

    client = LlmClient(
        _settings(),
        "job-abc12345-def-6",
        tmp_path / "trace.jsonl",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(PipelineError, match="content 为空"):
        await client.complete("req_parse", _PROMPT, "需求")
