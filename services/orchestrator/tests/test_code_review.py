"""code_review 节点测试：三视图全链路 + 程序化派生一致性 + 覆盖率/虚构校验。

覆盖：四态成功、partial→risks 派生、missing→missing_features 派生、
覆盖率缺口/虚构 REQ/虚构 IF/非法 status（X-LLM-02）、前置产物缺失（X-ORCH-01）、跳序。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from helpers import requirement_review_payload
from orchestrator.artifacts import ArtifactStore
from orchestrator.engine import Engine
from orchestrator.errors import X_ORCH_02, PipelineError
from orchestrator.llm import LlmResult
from orchestrator.nodes import run_code_review
from orchestrator.pipeline import NodeStatus
from orchestrator.prompts_loader import PromptSpec

_SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"
_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
_JOB = "job-3f2a1b8c-0000-4000-8000-000000000001"

_REQUIREMENTS: list[dict[str, Any]] = [
    {"id": "REQ-001", "title": "用户查询", "description": "按 ID 查询用户"},
    {"id": "REQ-002", "title": "订单导出", "description": "导出订单报表"},
    {"id": "REQ-003", "title": "批量删除", "description": "批量删除用户"},
]

_INTERFACES: list[dict[str, Any]] = [
    {
        "id": "IF-REST-001",
        "protocol": "rest",
        "signature": {"method": "GET", "path": "/api/v1/users", "summary": "查用户"},
        "code_location": {
            "repo": "user-service",
            "file": "UserController.java",
            "start_line": 30,
            "end_line": 45,
        },
        "is_fallback": False,
    },
    {
        "id": "IF-GRPC-001",
        "protocol": "grpc",
        "signature": {"service_method": "GetOrder", "summary": "查询订单"},
        "code_location": {
            "repo": "order-center",
            "file": "OrderGrpc.java",
            "start_line": 10,
            "end_line": 20,
        },
        "is_fallback": False,
    },
]

_LLM_GOOD = '''{
  "mappings": [
    {
      "requirement_id": "REQ-001",
      "interface_id": "IF-REST-001",
      "status": "implemented",
      "confidence": 0.9,
      "evidence": {"note": "GET /api/v1/users 覆盖按 ID 查询"}
    },
    {
      "requirement_id": "REQ-002",
      "interface_id": "IF-GRPC-001",
      "status": "partial",
      "confidence": 0.6,
      "evidence": {"note": "仅查询，无导出端点"}
    },
    {
      "requirement_id": "REQ-003",
      "status": "missing",
      "confidence": 0.8
    }
  ]
}'''

# 覆盖率缺口版本：REQ-003 无映射条目
_LLM_COVERAGE_GAP = '''{
  "mappings": [
    {
      "requirement_id": "REQ-001",
      "interface_id": "IF-REST-001",
      "status": "implemented",
      "confidence": 0.9
    },
    {
      "requirement_id": "REQ-002",
      "interface_id": "IF-GRPC-001",
      "status": "partial",
      "confidence": 0.6
    }
  ]
}'''


class _StubLlm:
    """可控 LLM 桩。"""

    def __init__(self, text: str) -> None:
        """预设输出文本。"""
        self._text = text
        self.received: list[tuple[str, str]] = []

    async def complete(self, node: str, prompt: PromptSpec, user_content: str) -> LlmResult:
        """记录调用并返回预设文本。"""
        self.received.append((prompt.ref, user_content))
        return LlmResult(text=self._text, tokens=88, call_id="LLM-stub-003")


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Engine, ArtifactStore]:
    """引擎 + 产物卷 + 前三节点已完成的前置状态。"""
    store = ArtifactStore(tmp_path, _SCHEMAS, "orchestrator")
    engine = Engine(store)
    state = engine.init_job(_JOB)
    store.write(_JOB, "requirement_review.json", requirement_review_payload(_JOB))
    for node_name, filename, payload in (
        (
            "req_parse",
            "requirement_items.json",
            {"schema_version": "1.0", "job_id": _JOB, "items": _REQUIREMENTS},
        ),
        ("code_scan", "code_inventory.json", None),  # 占位：见下方补写
        ("interface_reconstruct", "interfaces.json", None),
    ):
        if payload is not None:
            engine.mark_running(state, node_name)
            store.write(_JOB, filename, payload)
            engine.mark_completed(state, node_name)
    # code_scan / interface_reconstruct 两产物（合法即可）
    engine.mark_running(state, "code_scan")
    store.write(
        _JOB,
        "code_inventory.json",
        {
            "schema_version": "1.0",
            "job_id": _JOB,
            "repositories": [
                {
                    "name": "user-service",
                    "path": "/data/repos/user-service",
                    "branch": "release/v2.3",
                    "is_fallback": False,
                    "status": "ok",
                    "file_count": 1,
                },
                {
                    "name": "order-center",
                    "path": "/data/repos/order-center",
                    "branch": "master",
                    "is_fallback": False,
                    "status": "ok",
                    "file_count": 1,
                },
            ],
        },
    )
    engine.mark_completed(state, "code_scan")
    engine.mark_running(state, "interface_reconstruct")
    store.write(
        _JOB,
        "interface_candidates.json",
        {
            "schema_version": "1.0",
            "job_id": _JOB,
            "candidates": [
                {
                    "candidate_id": "CAND-001",
                    "protocol": "rest",
                    "signature": "GET /api/v1/users",
                    "code_location": _INTERFACES[0]["code_location"],
                    "is_fallback": False,
                },
                {
                    "candidate_id": "CAND-002",
                    "protocol": "grpc",
                    "signature": "OrderService/GetOrder",
                    "code_location": _INTERFACES[1]["code_location"],
                    "is_fallback": False,
                },
            ],
        },
    )
    store.write(
        _JOB,
        "interfaces.json",
        {"schema_version": "1.0", "job_id": _JOB, "interfaces": _INTERFACES},
    )
    engine.mark_completed(state, "interface_reconstruct")
    return engine, store


async def test_code_review_three_views(env: tuple[Engine, ArtifactStore]) -> None:
    """三视图成功：映射表 + partial→风险派生 + missing→遗漏派生；LLM 谎报位置被忽略。"""
    engine, store = env
    state = engine.open_job(_JOB)
    llm = _StubLlm(_LLM_GOOD)

    path = await run_code_review(engine, state, store, llm, _PROMPTS)

    assert path.is_file()
    payload = store.read_json(_JOB, "review_mapping.json")
    # 视图一：三条映射，覆盖率 100%
    assert len(payload["mappings"]) == 3
    m1 = payload["mappings"][0]
    assert m1["status"] == "implemented"
    # evidence.code_location 从接口回填（LLM 不输出该字段，由系统回填接口位置）
    assert m1["evidence"]["code_location"]["repo"] == "user-service"
    # 视图二：partial 自动进风险清单（R-REVW-04），含判定依据
    assert [r["requirement_id"] for r in payload["risks"]] == ["REQ-002"]
    assert "部分实现" in payload["risks"][0]["description"]
    assert "仅查询，无导出端点" in payload["risks"][0]["description"]
    # 视图三：missing 提取，title 从需求回填（程序派生）
    assert payload["missing_features"] == [
        {"requirement_id": "REQ-003", "title": "批量删除"}
    ]
    assert state.node("code_review").status is NodeStatus.COMPLETED
    # LLM 入参含双视图（需求 + 接口）
    _, user_content = llm.received[0]
    assert "REQ-001" in user_content and "IF-REST-001" in user_content


async def test_code_review_coverage_gap(env: tuple[Engine, ArtifactStore]) -> None:
    """缺一条需求映射：X-LLM-02（R-REVW-01 覆盖率 100%）。"""
    engine, store = env
    state = engine.open_job(_JOB)
    bad = _LLM_COVERAGE_GAP

    with pytest.raises(PipelineError) as err:
        await run_code_review(engine, state, store, _StubLlm(bad), _PROMPTS)

    assert err.value.code == "X-LLM-02"
    assert "REQ-003" in err.value.message


async def test_code_review_fabricated_requirement(env: tuple[Engine, ArtifactStore]) -> None:
    """虚构 REQ：X-LLM-02。"""
    engine, store = env
    state = engine.open_job(_JOB)
    bad = _LLM_GOOD.replace('"REQ-001"', '"REQ-999"')

    with pytest.raises(PipelineError) as err:
        await run_code_review(engine, state, store, _StubLlm(bad), _PROMPTS)

    assert err.value.code == "X-LLM-02"


async def test_code_review_fabricated_interface(env: tuple[Engine, ArtifactStore]) -> None:
    """虚构 IF：X-LLM-02。"""
    engine, store = env
    state = engine.open_job(_JOB)
    bad = _LLM_GOOD.replace('"IF-REST-001"', '"IF-REST-999"')

    with pytest.raises(PipelineError) as err:
        await run_code_review(engine, state, store, _StubLlm(bad), _PROMPTS)

    assert err.value.code == "X-LLM-02"


async def test_code_review_invalid_status(env: tuple[Engine, ArtifactStore]) -> None:
    """四态之外 status：X-LLM-02。"""
    engine, store = env
    state = engine.open_job(_JOB)
    bad = _LLM_GOOD.replace('"implemented"', '"probably"')

    with pytest.raises(PipelineError) as err:
        await run_code_review(engine, state, store, _StubLlm(bad), _PROMPTS)

    assert err.value.code == "X-LLM-02"


async def test_code_review_missing_prereq(env: tuple[Engine, ArtifactStore]) -> None:
    """前置产物缺失：X-ORCH-01（状态 completed 但文件没了——对账前直接执行）。"""
    engine, store = env
    state = engine.open_job(_JOB)
    (store.job_dir(_JOB) / "interfaces.json").unlink()

    with pytest.raises(PipelineError) as err:
        await run_code_review(engine, state, store, _StubLlm(_LLM_GOOD), _PROMPTS)

    assert err.value.code == "X-ORCH-01"


async def test_code_review_skip_order_guard(env: tuple[Engine, ArtifactStore]) -> None:
    """interface_reconstruct 未完成：X-ORCH-02。"""
    engine, store = env
    state = engine.open_job(_JOB)
    state.node("interface_reconstruct").status = NodeStatus.PENDING

    with pytest.raises(PipelineError) as err:
        await run_code_review(engine, state, store, _StubLlm(_LLM_GOOD), _PROMPTS)

    assert err.value.code == X_ORCH_02


async def test_code_review_change_scope_without_baseline(env: tuple[Engine, ArtifactStore]) -> None:
    """C-09 向后兼容：未配基线分支（无 changed_files 键）→ 视图四为空数组。"""
    engine, store = env
    state = engine.open_job(_JOB)

    await run_code_review(engine, state, store, _StubLlm(_LLM_GOOD), _PROMPTS)

    payload = store.read_json(_JOB, "review_mapping.json")
    assert payload["change_scope"] == []
    assert payload["removed_files"] == []
    assert payload["unmatched_changed_files"] == []


async def test_code_review_change_scope_derivation(env: tuple[Engine, ArtifactStore]) -> None:
    """C-09 视图四：变更文件 × 接口文件关联 + covered_by 回填 + removed 单列。"""
    engine, store = env
    state = engine.open_job(_JOB)
    inventory = store.read_json(_JOB, "code_inventory.json")
    inventory["repositories"][0]["baseline_branch"] = "main"
    inventory["repositories"][0]["changed_files"] = {
        "added": [],
        "modified": ["UserController.java", "UserService.java"],
        "removed": ["LegacyController.java"],
    }
    inventory["repositories"][1]["baseline_branch"] = "main"
    inventory["repositories"][1]["changed_files"] = {
        "added": ["OrderGrpc.java"],
        "modified": [],
        "removed": [],
    }
    store.write(_JOB, "code_inventory.json", inventory)

    await run_code_review(engine, state, store, _StubLlm(_LLM_GOOD), _PROMPTS)

    payload = store.read_json(_JOB, "review_mapping.json")
    # 变更接口两条：UserController（modified，REQ-001 覆盖）与 OrderGrpc（added，REQ-002 覆盖）
    assert payload["change_scope"] == [
        {
            "interface_id": "IF-REST-001",
            "repo": "user-service",
            "file": "UserController.java",
            "change": "modified",
            "covered_by": ["REQ-001"],
        },
        {
            "interface_id": "IF-GRPC-001",
            "repo": "order-center",
            "file": "OrderGrpc.java",
            "change": "added",
            "covered_by": ["REQ-002"],
        },
    ]
    assert payload["removed_files"] == [
        {"repo": "user-service", "file": "LegacyController.java"}
    ]
    # UserService.java 变更但无接口定义命中（业务逻辑层变更口径，DEF-001 同型）
    assert payload["unmatched_changed_files"] == [
        {"repo": "user-service", "file": "UserService.java", "change": "modified"}
    ]


async def test_code_review_change_scope_uncovered(env: tuple[Engine, ArtifactStore]) -> None:
    """C-09：变更接口未被任何需求映射 → covered_by 为空（变更风险信号）。"""
    engine, store = env
    state = engine.open_job(_JOB)
    inventory = store.read_json(_JOB, "code_inventory.json")
    inventory["repositories"][1]["baseline_branch"] = "main"
    inventory["repositories"][1]["changed_files"] = {
        "added": [],
        "modified": ["OrderGrpc.java"],
        "removed": [],
    }
    store.write(_JOB, "code_inventory.json", inventory)
    # REQ-002 不映射任何接口（undetermined）→ OrderGrpc 变更无需求覆盖
    llm_uncovered = _LLM_GOOD.replace(
        '"interface_id": "IF-GRPC-001",\n      ', ""
    ).replace('"status": "partial",', '"status": "undetermined",')

    await run_code_review(engine, state, store, _StubLlm(llm_uncovered), _PROMPTS)

    payload = store.read_json(_JOB, "review_mapping.json")
    assert payload["change_scope"] == [
        {
            "interface_id": "IF-GRPC-001",
            "repo": "order-center",
            "file": "OrderGrpc.java",
            "change": "modified",
            "covered_by": [],
        }
    ]
    assert payload["removed_files"] == []
    assert payload["unmatched_changed_files"] == []
