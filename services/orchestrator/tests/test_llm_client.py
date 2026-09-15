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


def _settings() -> Settings:
    """测试用配置（Mock 场景无鉴权）。"""
    return Settings.from_env(
        {
            "JAVA_PARSER_URL": "http://java-parser:8080",
            "LLM_BASE_URL": "http://llm-mock",
            "LLM_TIMEOUT_S": "5",
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
    assert record["prompt_version"] == "1.0"
    assert record["sampling"]["model"] == "glm-5.3"
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
