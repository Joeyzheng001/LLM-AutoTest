"""结构化 LLM 修复轮的节点专属输出契约测试。"""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.errors import X_LLM_02, PipelineError
from orchestrator.llm import LlmResult
from orchestrator.llm_repair import complete_with_json_repair
from orchestrator.prompts_loader import PromptSpec


class _StubLlm:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.requests: list[str] = []

    async def complete(self, _node: str, _prompt: PromptSpec, user_content: str) -> LlmResult:
        self.requests.append(user_content)
        return LlmResult(text=self.outputs.pop(0), tokens=None, call_id="LLM-test")


def _prompt(name: str) -> PromptSpec:
    return PromptSpec(
        name=name,
        version="1.0",
        spec="test",
        owner="QA",
        changelog=(),
        sampling={},
        body="",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt_name", "expected_hint"),
    [
        ("testcase_generate", '只输出 {"cases":[...]}'),
        ("code_review", '只输出 {"assessments":[...]}'),
        ("req_parse", '只输出 {"items":[...]}'),
    ],
)
async def test_repair_round_adds_node_specific_contract(
    prompt_name: str, expected_hint: str
) -> None:
    llm = _StubLlm(["{}", '{"ok":true}'])

    def parse(text: str) -> dict[str, Any]:
        if text == "{}":
            raise PipelineError(code=X_LLM_02, message="缺少目标数组")
        return {"ok": True}

    result = await complete_with_json_repair(
        llm, prompt_name, _prompt(prompt_name), "原始批次输入", parse
    )

    assert result == {"ok": True}
    assert expected_hint in llm.requests[1]
    assert "校验错误：缺少目标数组" in llm.requests[1]
