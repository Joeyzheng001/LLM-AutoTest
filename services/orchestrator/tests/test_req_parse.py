"""req_parse 节点测试：需求评审 + 条目提取、失败路径与跳序防护。

LLM 用桩注入（不真调模型）；Prompt 用仓库真实 prompts/req_parse.md。
"""

from __future__ import annotations

import json
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

    def __init__(self, items_text: str, review_text: str | None = None) -> None:
        """预设两个子阶段的输出；未指定评审时使用合法默认值。"""
        self._items_text = items_text
        self._review_text = review_text or _GOOD_REVIEW_OUTPUT
        self.received: list[tuple[str, str]] = []  # (prompt_ref, user_content)

    async def complete(self, node: str, prompt: PromptSpec, user_content: str) -> LlmResult:
        """记录调用并返回预设文本。"""
        self.received.append((prompt.ref, user_content))
        text = self._review_text if prompt.name == "requirement_review" else self._items_text
        return LlmResult(text=text, tokens=42, call_id=f"LLM-stub-{len(self.received):03d}")


class _SequenceLlm:
    """按调用顺序返回输出，用于覆盖 Schema 修复轮。"""

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = outputs
        self.received: list[str] = []

    async def complete(self, node: str, prompt: PromptSpec, user_content: str) -> LlmResult:
        self.received.append(user_content)
        text = self._outputs[len(self.received) - 1]
        return LlmResult(text=text, tokens=42, call_id=f"LLM-stub-{len(self.received):03d}")


_GOOD_OUTPUT = (
    '{"items": [{"id": "REQ-001", "title": "用户查询", '
    '"description": "按 ID 查询用户", "priority": "P0"}]}'
)

_GOOD_REVIEW_OUTPUT = json.dumps(
    {
        "score": 78,
        "quality_grade": "B",
        "summary": "主流程明确，异常和验收边界仍需补充。",
        "dimension_scores": {
            "需求完整性": {"weight": 0.35, "score": 75, "reason": "主流程已描述"},
            "一致性与逻辑性": {"weight": 0.20, "score": 80, "reason": "流程基本闭环"},
            "可测试性": {"weight": 0.25, "score": 75, "reason": "核心输入输出明确"},
            "清晰度与规范性": {"weight": 0.10, "score": 80, "reason": "术语基本统一"},
            "风险与依赖描述": {"weight": 0.10, "score": 80, "reason": "当前无额外依赖"},
        },
        "quality_assessment": {
            "grade": "B",
            "grade_name": "合格",
            "summary_reason": "可开始测试设计",
        },
        "issues": [],
        "readable_issue_points": [],
        "completeness_issues": [],
        "consistency_issues": [],
        "untestable_items": [],
        "risk_flags": [],
        "testable_features": ["用户查询"],
        "mindmap": {"主题": "测试思维导图", "子主题": [{"主题": "用户查询", "子主题": []}]},
    },
    ensure_ascii=False,
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

    path = await run_req_parse(engine, state, store, llm, _PROMPTS, [doc])

    assert path.is_file()
    payload: dict[str, Any] = store.read_json(_JOB, "requirement_items.json")
    assert payload["job_id"] == _JOB
    assert payload["source_file"] == str(doc)
    # C-10：多文档产物 source_files 记全部（单文档时为单元素列表）
    assert payload["source_files"] == [str(doc)]
    assert payload["items"][0]["id"] == "REQ-001"
    assert state.node("req_parse").status is NodeStatus.COMPLETED

    review = store.read_json(_JOB, "requirement_review.json")
    assert review["quality_grade"] == "B"
    assert review["source_files"] == [str(doc)]
    # 两个 Prompt 均为真实文件，且使用同一份已文本化的需求文档。
    assert [ref for ref, _content in llm.received] == ["requirement_review@1.5", "req_parse@1.0"]
    assert all("按 ID 查询用户" in content for _ref, content in llm.received)


async def test_req_parse_uses_version_diff_as_downstream_scope(
    env: tuple[Engine, ArtifactStore], tmp_path: Path
) -> None:
    """有基线时先产出差异，评审与条目提取只能接收差异范围而不是原文全文。"""
    engine, store = env
    state = engine.init_job(_JOB)
    baseline = tmp_path / "baseline.md"
    target = tmp_path / "target.md"
    baseline.write_text("旧：只能按 ID 查询用户", encoding="utf-8")
    target.write_text("新：可按 ID 或手机号查询用户", encoding="utf-8")
    diff = json.dumps(
        {
            "summary": "新增手机号查询条件",
            "changes": [
                {
                    "change_type": "modified",
                    "title": "用户查询条件",
                    "target_description": "支持手机号查询",
                    "baseline_description": "仅支持 ID 查询",
                    "change_summary": "新增手机号查询",
                    "source_location": "用户查询",
                }
            ],
        },
        ensure_ascii=False,
    )
    llm = _SequenceLlm([diff, _GOOD_REVIEW_OUTPUT, _GOOD_OUTPUT])

    await run_req_parse(
        engine,
        state,
        store,
        llm,
        _PROMPTS,
        [target],
        document_pairs=[{"target_path": str(target), "baseline_path": str(baseline)}],
    )

    stored_diff = store.read_json(_JOB, "requirement_diff.json")
    assert stored_diff["changes"][0]["id"] == "DIF-001"
    assert llm.received[0].find("基线版本") >= 0
    assert "需求版本差异分析" in llm.received[1]
    assert "只支持 ID 查询用户" not in llm.received[1]


async def test_req_parse_tolerates_code_fence(env: tuple[Engine, ArtifactStore], doc: Path) -> None:
    """LLM 输出带 ```json 围栏：容错剥离后正常解析。"""
    engine, store = env
    state = engine.init_job(_JOB)
    fenced = f"```json\n{_GOOD_OUTPUT}\n```"
    await run_req_parse(engine, state, store, _StubLlm(fenced), _PROMPTS, [doc])
    assert state.node("req_parse").status is NodeStatus.COMPLETED


async def test_req_parse_extracts_complete_json_from_think_block(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """推理模型把完整 JSON 放入 think 块时，仍可严格提取该对象。"""
    engine, store = env
    state = engine.init_job(_JOB)
    review = f"<think>分析过程\n{_GOOD_REVIEW_OUTPUT}\n</think>"
    await run_req_parse(engine, state, store, _StubLlm(_GOOD_OUTPUT, review), _PROMPTS, [doc])
    assert store.read_json(_JOB, "requirement_review.json")["quality_grade"] == "B"


async def test_req_parse_normalizes_unambiguous_review_display_variants(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """兼容 MiniMax 的维度数组和思维导图叶子字符串，落盘仍符合严格 Schema。"""
    engine, store = env
    state = engine.init_job(_JOB)
    review = json.loads(_GOOD_REVIEW_OUTPUT)
    review["dimension_scores"] = [
        {"dimension": dimension, **score} for dimension, score in review["dimension_scores"].items()
    ]
    review["mindmap"] = {"主题": "测试思维导图", "子主题": ["用户查询"]}

    llm = _StubLlm(_GOOD_OUTPUT, json.dumps(review, ensure_ascii=False))
    await run_req_parse(engine, state, store, llm, _PROMPTS, [doc])

    stored = store.read_json(_JOB, "requirement_review.json")
    assert stored["dimension_scores"]["需求完整性"]["score"] == 75
    assert stored["mindmap"]["子主题"] == [{"主题": "用户查询", "子主题": []}]


async def test_req_parse_repairs_schema_rejected_review_once(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """首轮缺契约字段时回传校验错误；第二轮合规输出可继续执行。"""
    engine, store = env
    state = engine.init_job(_JOB)
    llm = _SequenceLlm(["{}", _GOOD_REVIEW_OUTPUT, _GOOD_OUTPUT])

    await run_req_parse(engine, state, store, llm, _PROMPTS, [doc])

    assert state.node("req_parse").status is NodeStatus.COMPLETED
    assert len(llm.received) == 3
    assert "上一轮输出未通过机器校验" in llm.received[1]


async def test_req_parse_normalizes_real_model_review_aliases(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """兼容真实模型已出现的中文维度简称、理由别名和可读问题对象。"""
    engine, store = env
    state = engine.init_job(_JOB)
    review = json.loads(_GOOD_REVIEW_OUTPUT)
    review["总体评价"] = review.pop("summary")
    review["dimension_scores"]["一致性"] = review["dimension_scores"].pop("一致性与逻辑性")
    review["dimension_scores"]["清晰度"] = review["dimension_scores"].pop("清晰度与规范性")
    review["dimension_scores"]["风险与依赖"] = review["dimension_scores"].pop("风险与依赖描述")
    testability = review["dimension_scores"]["可测试性"]
    testability["说明"] = testability.pop("reason")
    review["readable_issue_points"] = [{"question": "请补充异常码定义。"}]
    review["testable_features"] = [
        {
            "主题": "FS-EP0-1 用户登录",
            "子主题": [
                {"主题": "用户名密码登录成功", "子主题": []},
                {"主题": "登录失败/锁定/Session过期", "子主题": []},
            ],
        }
    ]

    await run_req_parse(
        engine,
        state,
        store,
        _StubLlm(_GOOD_OUTPUT, json.dumps(review, ensure_ascii=False)),
        _PROMPTS,
        [doc],
    )

    stored = store.read_json(_JOB, "requirement_review.json")
    assert stored["summary"] == "主流程明确，异常和验收边界仍需补充。"
    assert stored["dimension_scores"]["可测试性"]["reason"] == "核心输入输出明确"
    assert stored["readable_issue_points"] == ["请补充异常码定义。"]
    assert stored["testable_features"] == [
        "FS-EP0-1 用户登录：用户名密码登录成功；登录失败/锁定/Session过期"
    ]


async def test_req_parse_drops_dimension_diagnostic_suffix(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """模型的 ``<维度>_error`` 自诊断不能污染受固定键约束的评分对象。"""
    engine, store = env
    state = engine.init_job(_JOB)
    review = json.loads(_GOOD_REVIEW_OUTPUT)
    review["dimension_scores"]["风险与依赖描述_error"] = "临时诊断信息"

    await run_req_parse(
        engine,
        state,
        store,
        _StubLlm(_GOOD_OUTPUT, json.dumps(review, ensure_ascii=False)),
        _PROMPTS,
        [doc],
    )

    dimensions = store.read_json(_JOB, "requirement_review.json")["dimension_scores"]
    assert "风险与依赖描述_error" not in dimensions
    assert dimensions["风险与依赖描述"]["score"] == 80


async def test_req_parse_moves_nested_quality_assessment_to_root(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """模型错置根级评估字段时，无损上提，不能留在固定维度对象中。"""
    engine, store = env
    state = engine.init_job(_JOB)
    review = json.loads(_GOOD_REVIEW_OUTPUT)
    review["dimension_scores"]["quality_assessment"] = review.pop("quality_assessment")

    await run_req_parse(
        engine,
        state,
        store,
        _StubLlm(_GOOD_OUTPUT, json.dumps(review, ensure_ascii=False)),
        _PROMPTS,
        [doc],
    )

    stored = store.read_json(_JOB, "requirement_review.json")
    assert stored["quality_assessment"]["grade"] == stored["quality_grade"]
    assert "quality_assessment" not in stored["dimension_scores"]


async def test_req_parse_drops_nested_quality_grade_and_caps_feature_list(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """真实模型的“质量评级”错位及超长可测功能清单不应阻断整份评审。"""
    engine, store = env
    state = engine.init_job(_JOB)
    review = json.loads(_GOOD_REVIEW_OUTPUT)
    review["dimension_scores"]["质量评级"] = {"grade": "B", "grade_name": "良好"}
    review["testable_features"] = [f"功能 {index}" for index in range(35)]

    await run_req_parse(
        engine,
        state,
        store,
        _StubLlm(_GOOD_OUTPUT, json.dumps(review, ensure_ascii=False)),
        _PROMPTS,
        [doc],
    )

    stored = store.read_json(_JOB, "requirement_review.json")
    assert "质量评级" not in stored["dimension_scores"]
    assert len(stored["testable_features"]) == 30


async def test_req_parse_normalizes_issue_reason_alias_or_missing(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    engine, store = env
    state = engine.init_job(_JOB)
    review = json.loads(_GOOD_REVIEW_OUTPUT)
    review["issues"] = [
        {
            "type": "歧义",
            "priority": "中",
            "location": "登录",
            "question": "有效期是多少",
            "说明": "未规定时长",
            "impact": "无法设计超时用例",
        },
        {
            "type": "建议",
            "priority": "低",
            "location": "全局",
            "question": "补充错误码",
            "impact": "断言不清晰",
        },
    ]
    await run_req_parse(
        engine,
        state,
        store,
        _StubLlm(_GOOD_OUTPUT, json.dumps(review, ensure_ascii=False)),
        _PROMPTS,
        [doc],
    )
    issues = store.read_json(_JOB, "requirement_review.json")["issues"]
    assert issues[0]["reason"] == "未规定时长"
    assert issues[1]["reason"] == "模型未提供原因，需人工补充"


async def test_req_parse_moves_root_dimensions_and_derives_score_grade(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    engine, store = env
    state = engine.init_job(_JOB)
    review = json.loads(_GOOD_REVIEW_OUTPUT)
    for name in list(review["dimension_scores"]):
        review[name] = review["dimension_scores"].pop(name)
    review.pop("dimension_scores")
    review.pop("score")
    review.pop("quality_grade")
    review["quality_assessment"].pop("grade")
    await run_req_parse(
        engine,
        state,
        store,
        _StubLlm(_GOOD_OUTPUT, json.dumps(review, ensure_ascii=False)),
        _PROMPTS,
        [doc],
    )
    stored = store.read_json(_JOB, "requirement_review.json")
    assert stored["score"] == 77
    assert stored["quality_grade"] == "B"
    assert stored["quality_assessment"]["grade"] == "B"


async def test_req_parse_repairs_dimension_object_misplaced_at_root(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """MiniMax 误回单个 dimension 时，修复轮收到明确重建指令并恢复完整评审。"""
    engine, store = env
    state = engine.init_job(_JOB)
    bad_dimension = '{"weight": 0.35, "score": 75, "reason": "主流程已描述"}'
    llm = _SequenceLlm([bad_dimension, _GOOD_REVIEW_OUTPUT, _GOOD_OUTPUT])

    await run_req_parse(engine, state, store, llm, _PROMPTS, [doc])

    assert store.read_json(_JOB, "requirement_review.json")["quality_grade"] == "B"
    assert "单个 dimension 评分对象" in llm.received[1]


async def test_req_parse_bad_json_x_llm_02(env: tuple[Engine, ArtifactStore], doc: Path) -> None:
    """LLM 输出不是 JSON → X-LLM-02，不截取可用部分，节点回 pending 并留痕。"""
    engine, store = env
    state = engine.init_job(_JOB)
    with pytest.raises(PipelineError) as excinfo:
        await run_req_parse(
            engine, state, store, _StubLlm("前面一段解释 {items:[...] 后面一段"), _PROMPTS, [doc]
        )
    assert excinfo.value.code == "X-LLM-02"
    assert state.node("req_parse").status is NodeStatus.PENDING
    assert state.node("req_parse").error is not None
    assert state.node("req_parse").error["code"] == "X-LLM-02"
    # 产物不落盘（失败即失败，无部分产物）
    assert not (store.job_dir(_JOB) / "requirement_items.json").exists()


async def test_req_parse_invalid_review_x_llm_02(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """评审缺少契约字段时不得降级为部分产物，节点回 pending。"""
    engine, store = env
    state = engine.init_job(_JOB)
    with pytest.raises(PipelineError) as excinfo:
        await run_req_parse(engine, state, store, _StubLlm(_GOOD_OUTPUT, "{}"), _PROMPTS, [doc])
    assert excinfo.value.code == "X-LLM-02"
    assert state.node("req_parse").status is NodeStatus.PENDING
    assert not (store.job_dir(_JOB) / "requirement_review.json").exists()
    assert not (store.job_dir(_JOB) / "requirement_items.json").exists()


async def test_req_parse_rejects_inconsistent_review_grade(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """总等级与质量结论等级不一致时，Schema 拒绝该评审结果。"""
    engine, store = env
    state = engine.init_job(_JOB)
    inconsistent = _GOOD_REVIEW_OUTPUT.replace('"grade": "B"', '"grade": "C"')
    with pytest.raises(PipelineError) as excinfo:
        await run_req_parse(
            engine, state, store, _StubLlm(_GOOD_OUTPUT, inconsistent), _PROMPTS, [doc]
        )
    assert excinfo.value.code == "X-LLM-02"
    assert not (store.job_dir(_JOB) / "requirement_review.json").exists()


async def test_req_parse_missing_items_x_llm_02(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """合法 JSON 但缺 items 数组 → X-LLM-02。"""
    engine, store = env
    state = engine.init_job(_JOB)
    with pytest.raises(PipelineError) as excinfo:
        await run_req_parse(engine, state, store, _StubLlm('{"条目": []}'), _PROMPTS, [doc])
    assert excinfo.value.code == "X-LLM-02"


async def test_req_parse_schema_violation_x_llm_02(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """items 内容不符 Schema（id 不符 REQ-\\d{3}）→ X-LLM-02（错误码语义归位）。"""
    engine, store = env
    state = engine.init_job(_JOB)
    bad = '{"items": [{"id": "1", "title": "t"}]}'
    with pytest.raises(PipelineError) as excinfo:
        await run_req_parse(engine, state, store, _StubLlm(bad), _PROMPTS, [doc])
    assert excinfo.value.code == "X-LLM-02"


async def test_req_parse_multi_docs_merged_sections(
    env: tuple[Engine, ArtifactStore], doc: Path, tmp_path: Path
) -> None:
    """C-10：多文档分节拼接——两份文档均入 user_content，产物 source_files 记两份。"""
    engine, store = env
    state = engine.init_job(_JOB)
    doc_b = tmp_path / "订单需求.md"
    doc_b.write_text("## 2. 订单\n功能：按单号查询订单。", encoding="utf-8")
    llm = _StubLlm(_GOOD_OUTPUT)

    await run_req_parse(engine, state, store, llm, _PROMPTS, [doc, doc_b])

    payload = store.read_json(_JOB, "requirement_items.json")
    assert payload["source_files"] == [str(doc), str(doc_b)]
    # 两个子阶段均使用同一文本化输入，两份文档均带分节标题。
    assert len(llm.received) == 2
    for _prompt_ref, user_content in llm.received:
        assert "需求文档 1：需求.md" in user_content
        assert "需求文档 2：订单需求.md" in user_content
        assert "按 ID 查询用户" in user_content
        assert "按单号查询订单" in user_content


async def test_req_parse_missing_doc_x_orch_01(
    env: tuple[Engine, ArtifactStore], tmp_path: Path
) -> None:
    """需求文档不存在 → X-ORCH-01（非 LLM 问题，不归 X-LLM-02）。"""
    engine, store = env
    state = engine.init_job(_JOB)
    with pytest.raises(PipelineError) as excinfo:
        await run_req_parse(
            engine, state, store, _StubLlm(_GOOD_OUTPUT), _PROMPTS, [tmp_path / "无.md"]
        )
    assert excinfo.value.code == "X-ORCH-01"


async def test_req_parse_blocked_when_out_of_order(
    env: tuple[Engine, ArtifactStore], doc: Path
) -> None:
    """跳序防护由 Engine 兜底（本节点是首节点，此处验证 check_runnable 已生效）。"""
    engine, store = env
    state = engine.init_job(_JOB)
    # 人为把首节点置为已完成之外的状态不改其可运行性；直接跑通后节点状态被冻结
    await run_req_parse(engine, state, store, _StubLlm(_GOOD_OUTPUT), _PROMPTS, [doc])
    with pytest.raises(PipelineError) as excinfo:
        engine.check_runnable(state, "code_review")
    assert excinfo.value.code == "X-ORCH-02"
