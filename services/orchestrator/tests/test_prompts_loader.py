"""Prompt 加载器测试：元信息块解析与 X-LLM-03 错误路径。"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.errors import PipelineError
from orchestrator.prompts_loader import _NODE_KNOWLEDGE, _parse_meta, load_prompt

_TEMPLATE_LIKE = """---
version: 1.2
spec: F-REQ-01            # 规格编号
owner: 甲                  # 主笔人
changelog:
  - 1.2 | 2026-09-15 | 补充输出示例
  - 1.0 | 2026-09-14 | 初版
sampling:                  # 建议值
  temperature: 0.3
  top_p: 0.9
  seed: 42
---

## System

你是需求解析器。
"""


def test_load_prompt_parses_meta(tmp_path: Path) -> None:
    """元信息块各字段正确解析，注释剥离，正文为块后内容。"""
    (tmp_path / "req_parse.md").write_text(_TEMPLATE_LIKE, encoding="utf-8")
    spec = load_prompt(tmp_path, "req_parse")
    assert spec.version == "1.2"
    assert spec.spec == "F-REQ-01"
    assert spec.owner == "甲"
    assert spec.changelog[0].startswith("1.2 |")
    assert spec.sampling == {"temperature": 0.3, "top_p": 0.9, "seed": 42}
    assert "需求解析器" in spec.body
    assert "version" not in spec.body
    assert spec.ref == "req_parse@1.2"


def test_load_prompt_injects_node_skill_and_memory() -> None:
    """正式 Prompt 包按节点选择知识，不加载无关节点经验。"""
    prompts = Path(__file__).resolve().parents[3] / "prompts"
    spec = load_prompt(prompts, "requirement_review")
    assert "受控知识：skills/requirement-output-stability" in spec.body
    assert spec.knowledge_refs == (
        # 与 prompts/skills/requirement-output-stability.md frontmatter 同步
            "skills/requirement-output-stability@2.0",
            "skills/structured-output-compatibility@1.0",
            "memory/model-output-drift@2.1",
            "skills/requirement-review-output-contract@1.0",
        )


def test_load_prompt_injects_structured_output_skill_for_all_json_nodes() -> None:
    """所有模型 JSON 产物节点加载同一兼容边界，而不是复制零散经验。"""
    prompts = Path(__file__).resolve().parents[3] / "prompts"
    for node in (
        "requirement_diff",
        "requirement_review",
        "req_parse",
        "interface_reconstruct",
        "code_review",
        "testcase_generate",
        "e2e_testcase_generate",
    ):
        spec = load_prompt(prompts, node)
        assert "skills/structured-output-compatibility@1.0" in spec.knowledge_refs


def test_load_prompt_appends_testcase_output_contract_last() -> None:
    """用例生成的唯一顶层结构契约必须位于模型上下文末尾。"""
    prompts = Path(__file__).resolve().parents[3] / "prompts"
    spec = load_prompt(prompts, "testcase_generate")
    assert spec.knowledge_refs[-1] == "skills/testcase-output-contract@1.1"
    assert "顶层只能有一个键：`cases`" in spec.body


def test_load_prompt_injects_failure_triage_memory_only_into_execution_design_nodes() -> None:
    """失败归因经验按需注入，避免全库记忆污染需求解析上下文。"""
    prompts = Path(__file__).resolve().parents[3] / "prompts"
    for node in ("code_review", "testcase_generate", "e2e_testcase_generate"):
        assert "memory/failure-triage@1.0" in load_prompt(prompts, node).knowledge_refs
    assert "memory/failure-triage@1.0" not in load_prompt(prompts, "req_parse").knowledge_refs


def test_all_mapped_knowledge_files_are_versioned_and_have_change_history() -> None:
    """映射到节点的 Skill/记忆必须可审计，避免新增文件后静默失效。"""
    prompts = Path(__file__).resolve().parents[3] / "prompts"
    mapped = sorted({relative for files in _NODE_KNOWLEDGE.values() for relative in files})
    assert mapped
    for relative in mapped:
        path = prompts / relative
        assert path.is_file(), relative
        version, spec, owner, changelog, _sampling, body = _parse_meta(
            path.read_text(encoding="utf-8")
        )
        assert version, relative
        assert spec, relative
        assert owner, relative
        assert changelog, relative
        assert body.strip(), relative


def test_node_knowledge_mapping_has_no_duplicate_entries() -> None:
    """同一节点不能重复注入同一条知识，避免无意义地膨胀上下文。"""
    for node, files in _NODE_KNOWLEDGE.items():
        assert len(files) == len(set(files)), node


@pytest.mark.parametrize(
    ("node", "contract_ref"),
    [
        ("requirement_diff", "skills/requirement-diff-output-contract@1.0"),
        ("requirement_review", "skills/requirement-review-output-contract@1.0"),
        ("req_parse", "skills/requirement-items-output-contract@1.0"),
        ("interface_reconstruct", "skills/interface-output-contract@1.0"),
        ("code_review", "skills/code-review-output-contract@1.0"),
        ("e2e_testcase_generate", "skills/e2e-output-contract@1.0"),
    ],
)
def test_load_prompt_appends_node_specific_output_contract(node: str, contract_ref: str) -> None:
    """每个 JSON 节点都有自己的末尾结构约束，不能仅依赖泛化规则。"""
    prompts = Path(__file__).resolve().parents[3] / "prompts"
    assert load_prompt(prompts, node).knowledge_refs[-1] == contract_ref


def test_load_prompt_missing_file(tmp_path: Path) -> None:
    """文件缺失 → X-LLM-03（配置错误，人工介入）。"""
    with pytest.raises(PipelineError) as excinfo:
        load_prompt(tmp_path, "nonexistent")
    assert excinfo.value.code == "X-LLM-03"
    assert excinfo.value.details == {"prompt": "nonexistent"}


def test_load_prompt_broken_meta(tmp_path: Path) -> None:
    """无元信息块 → X-LLM-03。"""
    (tmp_path / "broken.md").write_text("没有元信息块的裸 Prompt", encoding="utf-8")
    with pytest.raises(PipelineError) as excinfo:
        load_prompt(tmp_path, "broken")
    assert excinfo.value.code == "X-LLM-03"
