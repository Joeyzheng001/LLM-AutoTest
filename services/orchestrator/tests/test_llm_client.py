"""LLM 客户端测试：留痕、重试治理、错误分类（MockTransport，不真调模型）。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from orchestrator.config import Settings
from orchestrator.errors import PipelineError
from orchestrator.llm import LlmClient
from orchestrator.prompts_loader import PromptSpec

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
            "choices": [{"message": {"content": "解析结果"}}],
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


async def test_retry_on_5xx_then_success(tmp_path: Path) -> None:
    """5xx 视为暂时性故障：重试 2 次内成功则调用成功。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

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
            200, json={"choices": [{"message": {"content": raw}}], "usage": {"total_tokens": 64}}
        )

    trace = tmp_path / "job-abc12345-0" / "llm_calls.jsonl"
    client = LlmClient(
        _settings(), "job-abc12345-def-6", trace, transport=httpx.MockTransport(handler)
    )
    result = await client.complete("req_parse", _PROMPT, "内容")
    # 思考块已剥离，仅剩正文（代码围栏由节点层 _strip_code_fence 处理）
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
    from orchestrator.llm.client import strip_think_blocks

    assert strip_think_blocks("纯正文") == "纯正文"
    assert strip_think_blocks("<think>只有开头不闭合") == "<think>只有开头不闭合"
    assert strip_think_blocks("<think>a</think> 前文 <think>b</think> 后文") == "前文  后文"
    assert strip_think_blocks('<think>{"cases": []}</think>') == '<think>{"cases": []}</think>'
