"""F-GEN-05 定向补产与通用补产骨架测试（mock llm，全离线）。

关注骨架契约本身：
- 补产成功 → 目标测试点的用例被合并进结果；
- 补产调用失败/输出损坏 → 原样保留已有用例（增强路径不拖垮批次）；
- 补产跑偏（引用目标外测试点）→ 跑偏条目被剥离；
- F-GEN-05 补产提示词携带维度清单与已有标题（只补缺失维度的依据）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from common.errors import X_LLM_02, PipelineError
from common.llm.client import LlmResult
from executor.nodes.testcase_generate import (
    _recover_cases_with_targeted_prompt,
    _recover_underexpanded_testpoints,
)

_INTERFACES = [
    {
        "id": "IF-1",
        "signature": "POST /api/x",
        "params": [{"name": "fund_code", "in": "body", "type": "string"}],
    }
]


def _case(testpoint_id: str, title: str, category: str = "normal") -> dict[str, Any]:
    return {
        "interface_id": "IF-1",
        "testpoint_id": testpoint_id,
        "category": category,
        "title": title,
        "params": {"fund_code": "000001"},
        "expected": "status=200",
    }


class _StubLlm:
    """按预设文本应答的 llm 桩；记录收到的 user_content 供断言。"""

    def __init__(self, text: str | None = None, error: PipelineError | None = None):
        self._text = text
        self._error = error
        self.last_content: str | None = None

    async def complete(
        self, node: str, prompt: Any, user_content: str, max_attempts: int | None = None
    ) -> LlmResult:
        self.last_content = user_content
        if self._error is not None:
            raise self._error
        return LlmResult(text=self._text or "", tokens=None, call_id="stub")


def test_recover_merges_targeted_additions() -> None:
    """补产成功：目标测试点的新用例并入，原有用例保留。"""
    existing = [_case("TP-1", "已有用例")]
    addition = _case("TP-1", "补产：枚举值2", "boundary")
    stub = _StubLlm(text=json.dumps({"cases": [addition]}, ensure_ascii=False))
    merged = asyncio.run(
        _recover_cases_with_targeted_prompt(
            stub,
            prompt=None,  # 桩不读 prompt
            user_content="CTX",
            interfaces=_INTERFACES,
            cases=existing,
            instruction="补产指令",
            wanted_testpoints={"TP-1"},
            log_label="测试",
        )
    )
    assert len(merged) == 2
    assert any(c["title"] == "补产：枚举值2" for c in merged)


def test_recover_keeps_cases_on_llm_failure() -> None:
    """补产调用抛 X-LLM-02：原样返回已有用例，不中断。"""
    existing = [_case("TP-1", "已有用例")]
    stub = _StubLlm(
        error=PipelineError(code=X_LLM_02, message="输出损坏", node_id="testcase_generate")
    )
    merged = asyncio.run(
        _recover_cases_with_targeted_prompt(
            stub,
            prompt=None,
            user_content="CTX",
            interfaces=_INTERFACES,
            cases=existing,
            instruction="补产指令",
            wanted_testpoints={"TP-1"},
            log_label="测试",
        )
    )
    assert merged == existing


def test_recover_strips_offtarget_additions() -> None:
    """补产跑偏：引用了目标外测试点的条目被剥离，不混入产物。"""
    existing = [_case("TP-1", "已有用例")]
    offtarget = _case("TP-999", "跑偏用例")
    stub = _StubLlm(text=json.dumps({"cases": [offtarget]}, ensure_ascii=False))
    merged = asyncio.run(
        _recover_cases_with_targeted_prompt(
            stub,
            prompt=None,
            user_content="CTX",
            interfaces=_INTERFACES,
            cases=existing,
            instruction="补产指令",
            wanted_testpoints={"TP-1"},
            log_label="测试",
        )
    )
    assert merged == existing


def test_underexpanded_recovery_prompt_carries_hints_and_titles() -> None:
    """F-GEN-05 补产提示词必须携带维度清单与已有用例标题（只补缺失维度的依据）。"""
    existing = [_case("TP-1", "正常路径用例")]
    addition = _case("TP-1", "补产：边界路径", "boundary")
    stub = _StubLlm(text=json.dumps({"cases": [addition]}, ensure_ascii=False))
    points_by_id = {
        "TP-1": {
            "id": "TP-1",
            "source": "REQ",
            "case_split_hints": ["正常路径: 全量数据", "边界路径: 持仓为0", "异常路径: 数据缺失"],
        }
    }
    merged = asyncio.run(
        _recover_underexpanded_testpoints(
            stub,
            prompt=None,
            user_content="CTX",
            interfaces=_INTERFACES,
            cases=existing,
            underexpanded=[("TP-1", 3, 2, 1)],
            points_by_id=points_by_id,
        )
    )
    assert len(merged) == 2
    assert stub.last_content is not None
    # 提示词必须包含维度原文与已有标题，否则模型无从判断「哪些维度已覆盖」
    assert "边界路径: 持仓为0" in stub.last_content
    assert "正常路径用例" in stub.last_content
    assert "F-GEN-05" in stub.last_content
