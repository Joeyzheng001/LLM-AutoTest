"""Prompt 加载器测试：元信息块解析与 X-LLM-03 错误路径。"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.errors import PipelineError
from orchestrator.prompts_loader import load_prompt

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
        "memory/model-output-drift@2.0",
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
