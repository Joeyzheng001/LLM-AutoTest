"""req_parse 节点测试：全流程成功、LLM 输出坏 JSON/坏结构/Schema 不符、跳序防护。

LLM 用桩注入（不真调模型）；Prompt 用仓库真实 prompts/req_parse.md。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orchestrator.artifacts import ArtifactStore
from orchestrator.engine import Engine
from orchestrator.errors import PipelineError
from orchestrator.llm import LlmResult
from orchestrator.nodes import run_req_parse
from orchestrator.pipeline import NodeStatus
from orchestrator.prompts_loader import PromptSpec

_SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"
_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
_JOB = "job-3f2a1b8c-0000-4000-8000-000000000001"


class _StubLlm:
    """可控 LLM 桩：按预设文本返回。"""

    def __init__(self, text: str) -> None:
        """预设输出文本。"""
        self._text = text
        self.received: list[tuple[str, str]] = []  # (prompt_ref, user_content)

    async def complete(self, node: str, prompt: PromptSpec, user_content: str) -> LlmResult:
        """记录调用并返回预设文本。"""
        self.received.append((prompt.ref, user_content))
        return LlmResult(text=self._text, tokens=42, call_id="LLM-stub-001")


_GOOD_OUTPUT = (
    '{"items": [{"id": "REQ-001", "title": "用户查询", '
    '"description": "按 ID 查询用户", "priority": "P0"}]}'
)

_DOC_CONTENT = "## 1. 用户管理\n功能：按 ID 查询用户。"


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Engine, ArtifactStore]:
    """引擎 + 产物卷（真实 Schema）。"""
    store = ArtifactStore(tmp_path, _SCHEMAS, "orchestrator")
    return Engine(store), store


@pytest.fixture()
def doc(tmp_path: Path) -> Path:
    """临时需求文档（md）。"""
    path = tmp_path / "需求.md"
    path.write_text(_DOC_CONTENT, encoding="utf-8")
    return path


async def test_req_parse_success(env: tuple[Engine, ArtifactStore], doc: Path) -> None:
    """全流程成功：产物落盘、节点 completed、Prompt 取自真实文件、doc 全文入参。"""
    engine, store = env
    state = engine.init_job(_JOB)
    llm = _StubLlm(_GOOD_OUTPUT)

    path = await run_req_parse(engine, state, store, llm, _PROMPTS, doc)

    assert path.is_file()
    payload: dict[str, Any] = store.read_json(_JOB, "requirement_items.json")
    assert payload["job_id"] == _JOB
    assert payload["source_file"] == str(doc)
    assert payload["items"][0]["id"] == "REQ-001"
    assert state.node("req_parse").status is NodeStatus.COMPLETED

    # Prompt 为真实文件（req_parse@1.0），用户消息是文档全文
    prompt_ref, user_content = llm.received[0]
    assert prompt_ref == "req_parse@1.0"
    assert "按 ID 查询用户" in user_content


async def test_req_parse_tolerates_code_fence(env: tuple[Engine, ArtifactStore], doc: Path) -> None:
    """LLM 输出带 ```json 围栏：容错剥离后正常解析。"""
    engine, store = env
    state = engine.init_job(_JOB)
    fenced = f"```json\n{_GOOD_OUTPUT}\n```"
    await run_req_parse(engine, state, store, _StubLlm(fenced), _PROMPTS, doc)
    assert state.node("req_parse").status is NodeStatus.COMPLETED


async def test_req_parse_bad_json_x_llm_02(env: tuple[Engine, ArtifactStore], doc: Path) -> None:
    """LLM 输出不是 JSON → X-LLM-02，不截取可用部分，节点回 pending 并留痕。"""
    engine, store = env
    state = engine.init_job(_JOB)
    with pytest.raises(PipelineError) as excinfo:
        await run_req_parse(
            engine, state, store, _StubLlm("前面一段解释 {items:[...] 后面一段"), _PROMPTS, doc
        )
    assert excinfo.value.code == "X-LLM-02"
    assert state.node("req_parse").status is NodeStatus.PENDING
    assert state.node("req_parse").error is not None
    assert state.node("req_parse").error["code"] == "X-LLM-02"
    # 产物不落盘（失败即失败，无部分产物）
    assert not (store.job_dir(_JOB) / "requirement_items.json").exists()


async def test_req_parse_missing_items_x_llm_02(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """合法 JSON 但缺 items 数组 → X-LLM-02。"""
    engine, store = env
    state = engine.init_job(_JOB)
    with pytest.raises(PipelineError) as excinfo:
        await run_req_parse(engine, state, store, _StubLlm('{"条目": []}'), _PROMPTS, doc)
    assert excinfo.value.code == "X-LLM-02"


async def test_req_parse_schema_violation_x_llm_02(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """items 内容不符 Schema（id 不符 REQ-\\d{3}）→ X-LLM-02（错误码语义归位）。"""
    engine, store = env
    state = engine.init_job(_JOB)
    bad = '{"items": [{"id": "1", "title": "t", "description": "d"}]}'
    with pytest.raises(PipelineError) as excinfo:
        await run_req_parse(engine, state, store, _StubLlm(bad), _PROMPTS, doc)
    assert excinfo.value.code == "X-LLM-02"


async def test_req_parse_missing_doc_x_orch_01(
    env: tuple[Engine, ArtifactStore], tmp_path: Path
) -> None:
    """需求文档不存在 → X-ORCH-01（非 LLM 问题，不归 X-LLM-02）。"""
    engine, store = env
    state = engine.init_job(_JOB)
    with pytest.raises(PipelineError) as excinfo:
        await run_req_parse(
            engine, state, store, _StubLlm(_GOOD_OUTPUT), _PROMPTS, tmp_path / "无.md"
        )
    assert excinfo.value.code == "X-ORCH-01"


async def test_req_parse_blocked_when_out_of_order(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """跳序防护由 Engine 兜底（本节点是首节点，此处验证 check_runnable 已生效）。"""
    engine, store = env
    state = engine.init_job(_JOB)
    # 人为把首节点置为已完成之外的状态不改其可运行性；直接跑通后节点状态被冻结
    await run_req_parse(engine, state, store, _StubLlm(_GOOD_OUTPUT), _PROMPTS, doc)
    with pytest.raises(PipelineError) as excinfo:
        engine.check_runnable(state, "code_review")
    assert excinfo.value.code == "X-ORCH-02"
