"""LLM JSON 输出的单次受限修复轮。

产物 Schema 仍是最终质量闸门；本模块只在首次输出被解析/业务边界校验拒绝时，
将明确错误回传给同一模型，让其基于完全相同的原始输入重排结构一次。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from orchestrator.errors import X_LLM_02, PipelineError
from orchestrator.llm import LlmResult
from orchestrator.prompts_loader import PromptSpec

T = TypeVar("T")


class SupportsComplete(Protocol):
    """修复轮所需的最小 LLM 客户端接口。"""

    async def complete(self, node: str, prompt: PromptSpec, user_content: str) -> LlmResult:
        """执行一次模型补全。"""


async def complete_with_json_repair(
    llm: SupportsComplete,
    node: str,
    prompt: PromptSpec,
    user_content: str,
    parse: Callable[[str], T],
    validate: Callable[[T], None] | None = None,
) -> T:
    """执行一次补全；仅对 X-LLM-02 回传校验错误并再尝试一次。"""
    def parse_and_validate(text: str) -> T:
        parsed = parse(text)
        if validate is not None:
            validate(parsed)
        return parsed

    first = await llm.complete(node, prompt, user_content)
    try:
        return parse_and_validate(first.text)
    except PipelineError as exc:
        if exc.code != X_LLM_02:
            raise
        repair_content = (
            f"{user_content}\n\n"
            "上一轮输出未通过机器校验。请仅基于上述原始输入修正 JSON 结构和引用，"
            "不要解释、不要 Markdown、不要遗漏本批条目。\n"
            f"校验错误：{exc.message}"
        )
        second = await llm.complete(node, prompt, repair_content)
        return parse_and_validate(second.text)
