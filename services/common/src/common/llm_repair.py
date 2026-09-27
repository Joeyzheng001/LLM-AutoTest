"""LLM JSON 输出的单次受限修复轮。

产物 Schema 仍是最终质量闸门；本模块只在首次输出被解析/业务边界校验拒绝时，
将明确错误回传给同一模型，让其基于完全相同的原始输入重排结构一次。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from common.errors import X_LLM_02, PipelineError
from common.llm import LlmResult
from common.prompts_loader import PromptSpec

T = TypeVar("T")


_REPAIR_CONTRACTS: dict[str, str] = {
    "requirement_diff": (
        '只输出 {"summary":"...","changes":[]}；每个 change 必须引用本批基线/被测版本 SRC 编号，'
        '分别填写 target_source_refs 和 baseline_source_refs。不得生成来源编号或跨版本引用；'
        '顶层不得出现 body/data/result/content。'
    ),
    "requirement_review": (
        '只输出 {"findings":[...]}；每项必须有问题、原因、影响和当前批 claim/source 引用。'
        '不要输出评分、等级、summary、dimension_scores、mindmap 或旧版完整评审对象。'
    ),
    "req_parse": (
        '只输出 {"items":[...]}；每项含 title、source_refs、claims；每个 claim 含 kind、'
        'statement、source_refs 和可选 acceptance_criteria。只引用当前批已有的 SRC ID，不能遗漏来源。'
    ),
    "interface_reconstruct": (
        '只输出 {"interfaces":[...]}，interfaces 必须覆盖当前批次候选；'
        '不要输出 body/data/result/content 或 recon_date。'
    ),
    "code_review": (
        '只输出 {"assessments":[...]}；当前批每个 claim_id 恰好出现一次。'
        '只引用当前批提供的 interface_id/evidence_id，禁止输出旧版 mappings、body/data/result/content。'
    ),
    "testpoint_derive": (
        '只输出 {"hints":[...]}，hints 必须是真正的数组；本批每个输入 testpoint_id 恰好出现'
        '一次，case_split_hints 为带具体条件的字符串数组。禁止输出 cases、category、params、'
        'expected 等用例字段或 body/data/result/content。'
    ),
    "testcase_generate": (
        '只输出 {"cases":[...]}，cases 必须是真正的数组；每个接口覆盖 normal、'
        'boundary、exception，禁止输出 body/data/result/content 或跨节点字段。'
    ),
    "e2e_testcase_generate": (
        '只输出 {"cases":[...]}，步骤只能使用探测到的动作、路径和 selector；'
        'goto/expect_url 不需要 selector，不要输出解释。'
    ),
}


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
            "本节点输出契约："
            f"{_REPAIR_CONTRACTS.get(prompt.name, '严格遵守主 Prompt 和 Schema。')}\n"
            f"校验错误：{exc.message}"
        )
        second = await llm.complete(node, prompt, repair_content)
        return parse_and_validate(second.text)
