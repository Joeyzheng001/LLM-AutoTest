"""code_review 节点测试：四视图全链路 + 程序化派生一致性 + 覆盖率/虚构校验。

覆盖：Claim 状态汇总、partial→risks 派生、missing→missing_features 派生、
覆盖率缺口/虚构 REQ/虚构 IF/非法 status（X-LLM-02）、前置产物缺失（X-ORCH-01）、跳序。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from common.artifacts import ArtifactStore
from common.engine import Engine
from common.errors import X_ORCH_02, PipelineError
from common.llm import LlmResult
from common.pipeline import NodeStatus
from common.prompts_loader import PromptSpec

from helpers import change_set_payload, requirement_review_payload
from orchestrator.nodes import run_code_review
from orchestrator.nodes.code_review import (
    _content_tokens,
    _select_evidence_for_claims,
    _select_evidence_with_llm,
    _select_interfaces_for_claims,
)

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
    store.write(_JOB, "change_set.json", change_set_payload(_JOB))
    engine.mark_completed(state, "interface_reconstruct")
    return engine, store


async def test_code_review_three_views(env: tuple[Engine, ArtifactStore]) -> None:
    """四视图成功：映射表 + partial→风险派生 + missing→遗漏派生；LLM 谎报位置被忽略。"""
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


@pytest.mark.parametrize(
    "wrapped",
    [
        json.dumps(json.loads(_LLM_GOOD)["mappings"], ensure_ascii=False),
        '{"data": ' + _LLM_GOOD + "}",
        '{"评审映射": ' + _LLM_GOOD.split('"mappings": ', 1)[1],
    ],
)
async def test_code_review_accepts_safe_mapping_container_variants(
    env: tuple[Engine, ArtifactStore], wrapped: str
) -> None:
    """模型常见裸数组/包装不应导致降级；内容仍须经同一覆盖率校验。"""
    engine, store = env
    state = engine.open_job(_JOB)

    await run_code_review(engine, state, store, _StubLlm(wrapped), _PROMPTS)

    assert len(store.read_json(_JOB, "review_mapping.json")["mappings"]) == 3


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
            "matched_via": "definition",
            "covered_by": ["REQ-001"],
        },
        {
            "interface_id": "IF-GRPC-001",
            "repo": "order-center",
            "file": "OrderGrpc.java",
            "change": "added",
            "matched_via": "definition",
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
            "matched_via": "definition",
            "covered_by": [],
        }
    ]
    assert payload["removed_files"] == []
    assert payload["unmatched_changed_files"] == []

async def test_change_scope_matches_related_files_and_shrinks_unmatched(
    env: tuple[Engine, ArtifactStore],
) -> None:
    """P0③：接口定义文件没改、但依赖（Service/DTO）被改 → 仍进 change_scope。

    此前只看 code_location.file，这类接口会整个漏出 change_scope，而被改的 Service
    又全部落进 unmatched_changed_files——两头都不对（评审 §5）。
    """
    engine, store = env
    state = engine.open_job(_JOB)
    inventory = store.read_json(_JOB, "code_inventory.json")
    # 只改 UserService.java（接口定义文件 UserController.java 未改）
    inventory["repositories"][0]["baseline_branch"] = "main"
    inventory["repositories"][0]["changed_files"] = {
        "added": [],
        "modified": ["UserService.java"],
        "removed": [],
    }
    store.write(_JOB, "code_inventory.json", inventory)
    # 接口带上 related_files（java-parser 静态解析产物，已随 interfaces.json 透传）
    store.write(
        _JOB,
        "interfaces.json",
        {
            "schema_version": "1.0",
            "job_id": _JOB,
            "interfaces": [
                {
                    "id": "IF-REST-001",
                    "protocol": "rest",
                    "signature": {"method": "GET", "path": "/api/v1/users", "summary": "查用户"},
                    "code_location": {
                        "repo": "user-service",
                        "file": "UserController.java",
                        "start_line": 1,
                        "end_line": 5,
                    },
                    "is_fallback": False,
                    "related_files": [
                        {
                            "repo": "user-service",
                            "file": "UserController.java",
                            "role": "controller",
                        },
                        {"repo": "user-service", "file": "UserService.java", "role": "service"},
                    ],
                    "change_evidence": {"level": "callee"},
                }
            ],
        },
    )
    store.write(_JOB, "change_set.json", change_set_payload(_JOB))
    # 本任务只还原出一个接口：LLM 桩同步去掉对 IF-GRPC-001 的引用（虚构接口即 X-LLM-02）
    llm_single = _LLM_GOOD.replace('"interface_id": "IF-GRPC-001",\n      ', "")

    await run_code_review(engine, state, store, _StubLlm(llm_single), _PROMPTS)

    payload = store.read_json(_JOB, "review_mapping.json")
    assert payload["change_scope"] == [
        {
            "interface_id": "IF-REST-001",
            "repo": "user-service",
            "file": "UserController.java",
            # 定义文件没改 → change 取关联文件的变更类型，来源由 matched_via 说清
            "change": "modified",
            "matched_via": "related_file",
            "related_changed_files": [
                {"file": "UserService.java", "role": "service", "change": "modified"}
            ],
            "evidence_level": "callee",
            "covered_by": ["REQ-001"],
        }
    ]
    # 被关联文件命中的变更文件不再算 unmatched
    assert payload["unmatched_changed_files"] == []


async def test_code_review_schema_accepts_recursive_call_graph_and_evidence_roles(
    env: tuple[Engine, ArtifactStore], tmp_path: Path
) -> None:
    """递归调用链与证据 role 都属于程序派生字段，必须能通过最终产物 Schema。"""
    engine, store = env
    for name in ("UserController.java", "UserService.java"):
        (tmp_path / name).write_text("\n".join([f"class {name[:-5]} {{"] * 50), encoding="utf-8")

    inventory = store.read_json(_JOB, "code_inventory.json")
    inventory["repositories"][0]["path"] = str(tmp_path)
    inventory["repositories"][0]["files"] = [
        {"repo": "user-service", "module": "", "path": "UserController.java"},
        {"repo": "user-service", "module": "", "path": "UserService.java"},
    ]
    store.write(_JOB, "code_inventory.json", inventory)

    interfaces = store.read_json(_JOB, "interfaces.json")
    interfaces["interfaces"][0]["code_spans"] = [
        {
            "file": "UserService.java",
            "role": "service",
            "kind": "callee",
            "start_line": 1,
            "end_line": 5,
            "depth": 1,
            "call_line": 32,
        }
    ]
    interfaces["interfaces"][0]["call_graph"] = {
        "status": "complete",
        "resolved_calls": 1,
        "resolved_nodes": 1,
        "unresolved_calls": 0,
        "max_depth": 1,
    }
    store.write(_JOB, "interfaces.json", interfaces)

    await run_code_review(engine, engine.open_job(_JOB), store, _StubLlm(_LLM_GOOD), _PROMPTS)

    payload = store.read_json(_JOB, "review_mapping.json")
    chain = payload["call_chains"][0]
    assert chain["confidence"] == "static_recursive"
    assert chain["callees"][0]["role"] == "service"
    assert any(item.get("role") == "service" for item in payload["evidence_catalog"])


def test_evidence_gate_downgrades_session_requirement_without_direct_evidence() -> None:
    from orchestrator.nodes.code_review import (
        _apply_deterministic_gates,
        _derive_deterministic_checks,
        _derive_risks,
    )

    requirements = [
        {"id": "REQ-001", "title": "登录 Session 有效期", "description": "会话有效期 30 分钟"}
    ]
    checks = _derive_deterministic_checks(requirements, [])
    mappings = _apply_deterministic_gates(
        [{"requirement_id": "REQ-001", "status": "implemented", "confidence": 0.9}], checks
    )

    assert mappings[0]["status"] == "undetermined"
    assert mappings[0]["confidence"] == 0.45
    assert "确定性证据门槛" in mappings[0]["evidence"]["note"]
    risks = _derive_risks(mappings, checks)
    assert risks[0]["kind"] == "evidence_gap"


def test_collect_code_evidence_is_scoped_and_redacts_values(tmp_path: Path) -> None:
    from orchestrator.nodes.code_review import _collect_code_evidence

    source = tmp_path / "settings.py"
    source.write_text(
        "SESSION_COOKIE_AGE = 1800\nAPI_KEY = 'should-not-leak'\n", encoding="utf-8"
    )
    inventory = {
        "repositories": [
            {"name": "demo", "path": str(tmp_path), "files": [{"path": "settings.py"}]}
        ]
    }

    evidence = _collect_code_evidence(inventory)

    assert evidence[0]["category"] == "session"
    assert evidence[0]["file"] == "settings.py"
    assert "should-not-leak" not in evidence[0]["excerpt"]


def test_collect_code_evidence_includes_config_not_listed_by_python_scan(tmp_path: Path) -> None:
    from orchestrator.nodes.code_review import _collect_code_evidence

    (tmp_path / "application.yml").write_text("jwt:\n  ttl: 1800\n", encoding="utf-8")
    inventory = {
        "repositories": [
            {"name": "demo", "path": str(tmp_path), "files": [{"path": "views.py"}]}
        ]
    }

    evidence = _collect_code_evidence(inventory)

    assert any(item["category"] == "session" for item in evidence)


def test_collect_code_evidence_includes_recursive_interface_spans(tmp_path: Path) -> None:
    from orchestrator.nodes.code_review import _collect_code_evidence

    (tmp_path / "Controller.java").write_text("class Controller {}\n", encoding="utf-8")
    (tmp_path / "Service.java").write_text(
        "class Service { String load() { return \"ok\"; } }\n", encoding="utf-8"
    )
    inventory = {
        "repositories": [
            {
                "name": "demo",
                "path": str(tmp_path),
                "files": [{"path": "Controller.java"}, {"path": "Service.java"}],
            }
        ]
    }
    interfaces = [
        {
            "id": "IF-REST-001",
            "code_location": {
                "repo": "demo",
                "file": "Controller.java",
                "start_line": 1,
                "end_line": 1,
            },
            "code_spans": [
                {
                    "file": "Controller.java",
                    "role": "controller",
                    "kind": "body",
                    "start_line": 1,
                    "end_line": 1,
                },
                {
                    "file": "Service.java",
                    "role": "service",
                    "kind": "callee",
                    "start_line": 1,
                    "end_line": 1,
                    "depth": 1,
                },
            ],
        }
    ]

    evidence = _collect_code_evidence(inventory, interfaces)

    assert any(
        item["category"] == "call_chain_implementation"
        and item["file"] == "Service.java"
        and item["interface_ids"] == ["IF-REST-001"]
        for item in evidence
    )


def test_requirement_with_implemented_and_undetermined_claims_is_partial() -> None:
    from orchestrator.nodes.code_review import _aggregate_claim_assessments

    requirements = [
        {
            "id": "REQ-001",
            "title": "自动审批配置",
            "claims": [
                {"claim_id": "CLM-001", "statement": "支持查询"},
                {"claim_id": "CLM-002", "statement": "校验取值"},
            ],
        }
    ]
    assessments = [
        {
            "claim_id": "CLM-001",
            "status": "implemented",
            "confidence": 0.9,
            "interface_ids": ["IF-REST-001"],
            "evidence_refs": ["EVD-0001"],
            "met_conditions": ["查询接口存在"],
            "unmet_conditions": [],
            "verification_needed": [],
        },
        {
            "claim_id": "CLM-002",
            "status": "undetermined",
            "confidence": 0.4,
            "interface_ids": ["IF-REST-001"],
            "evidence_refs": [],
            "met_conditions": [],
            "unmet_conditions": [],
            "verification_needed": ["需要确认枚举值"],
        },
    ]

    mappings = _aggregate_claim_assessments(requirements, assessments)

    assert mappings[0]["status"] == "partial"
    assert mappings[0]["confidence"] == 0.65
    assert mappings[0]["claim_ids"] == ["CLM-001", "CLM-002"]
    assert mappings[0]["claim_counts"] == {
        "total": 2,
        "implemented": 1,
        "partial": 0,
        "missing": 0,
        "undetermined": 1,
    }


def test_missing_view_contains_claim_statement() -> None:
    from orchestrator.nodes.code_review import _aggregate_claim_assessments, _derive_missing

    requirements = [
        {
            "id": "REQ-001",
            "title": "自动审批配置",
            "claims": [{"claim_id": "CLM-001", "statement": "支持按业务类型配置审批规则"}],
        }
    ]
    mappings = _aggregate_claim_assessments(
        requirements,
        [
            {
                "claim_id": "CLM-001",
                "status": "missing",
                "confidence": 0.9,
                "interface_ids": [],
                "evidence_refs": [],
                "met_conditions": [],
                "unmet_conditions": ["未找到对应审批规则配置入口"],
                "verification_needed": [],
            }
        ],
    )

    assert _derive_missing(mappings, requirements) == [
        {
            "requirement_id": "REQ-001",
            "title": "自动审批配置",
            "claim_id": "CLM-001",
            "claim_statement": "支持按业务类型配置审批规则",
            "interface_ids": [],
            "evidence_refs": [],
            "unmet_conditions": ["未找到对应审批规则配置入口"],
        }
    ]


def test_call_chain_and_incremental_findings_are_static(tmp_path: Path) -> None:
    from orchestrator.nodes.code_review import _derive_call_chains, _derive_incremental_findings

    (tmp_path / "UserController.java").write_text(
        "class UserController { void get() { userService.find(); } }", encoding="utf-8"
    )
    inventory = {"repositories": [{"name": "demo", "path": str(tmp_path), "files": []}]}
    interfaces = [
        {"id": "IF-REST-001", "code_location": {"repo": "demo", "file": "UserController.java"}}
    ]
    chains = _derive_call_chains(inventory, interfaces)
    findings = _derive_incremental_findings(
        [
            {
                "interface_id": "IF-REST-001",
                "repo": "demo",
                "file": "UserController.java",
                "covered_by": [],
            }
        ],
        [],
        [],
        [],
    )

    assert chains[0]["callees"] == ["userService"]
    assert findings[0]["kind"] == "changed_interface_uncovered"
    assert findings[0]["severity"] == "P1"


def test_partial_authorization_gap_is_release_blocking() -> None:
    from orchestrator.nodes.code_review import _derive_risks

    risks = _derive_risks(
        [{"requirement_id": "REQ-001", "status": "partial", "confidence": 0.7}],
        [
            {
                "requirement_id": "REQ-001",
                "category": "authorization",
                "status": "needs_verification",
            }
        ],
    )

    assert risks[0]["severity"] == "P0"
    assert "阻断发布" in risks[0]["action"]


def test_select_interfaces_full_recall_when_few_interfaces() -> None:
    """接口不超过阈值时全量返回：语义召回不因字面不重叠漏召接口。"""
    claims = [
        {
            "requirement_title": "自动审批设置",
            "statement": "配置审批规则供下游取数调用",
            "acceptance_criteria": [],
        }
    ]
    interfaces = [
        {"id": "IF-001", "signature": {"summary": "风险审批试算接口"}, "params": []},
        {"id": "IF-002", "signature": {"summary": "公共邮件发送接口"}, "params": []},
    ]
    result = _select_interfaces_for_claims(claims, interfaces)
    # 字面几乎零重叠，但接口少 → 全量交给 LLM 语义判断，不盲给前 N 个
    assert [item["id"] for item in result] == ["IF-001", "IF-002"]


def test_select_interfaces_lexical_prefilter_when_many_interfaces() -> None:
    """接口超过阈值时退化为字面重叠预筛，控制上下文规模。"""
    claims = [
        {
            "requirement_title": "用户查询",
            "statement": "按 ID 查询用户",
            "acceptance_criteria": [],
        }
    ]
    interfaces = [
        {
            "id": f"IF-{index:03d}",
            "signature": {"summary": "用户查询" if index == 0 else "无关接口"},
            "params": [],
        }
        for index in range(45)
    ]
    result = _select_interfaces_for_claims(claims, interfaces)
    assert len(result) <= 40
    assert result[0]["id"] == "IF-000"


_EVIDENCE_SAMPLE: list[dict[str, Any]] = [
    {
        "evidence_id": "EVD-0001",
        "category": "interface_implementation",
        "repo": "riskwkb",
        "file": "src/main/java/RiskApprovalRuleServiceImpl.java",
        "start_line": 10,
        "end_line": 12,
        "excerpt": "public RiskApprovalRule query(String businessType) {",
        "interface_ids": ["IF-REST-006"],
    },
    {
        "evidence_id": "EVD-0002",
        "category": "cache",
        "repo": "riskwkb",
        "file": "src/main/java/RedisCacheManager.java",
        "start_line": 3,
        "end_line": 5,
        "excerpt": "redisTemplate()",
        "interface_ids": [],
    },
]
_APPROVAL_CLAIM: list[dict[str, Any]] = [
    {
        "claim_id": "CLM-0001",
        "requirement_title": "自动审批设置",
        "statement": "审批规则列表支持按业务类型搜索",
        "acceptance_criteria": [],
    }
]


class _EvidenceSelectStub:
    """只回答证据召回调用的桩（按 Prompt 名分流）。"""

    def __init__(self, selection: str) -> None:
        self._selection = selection
        self.calls: list[str] = []

    async def complete(
        self, node: str, prompt: PromptSpec, user_content: str
    ) -> LlmResult:
        """记录被调用的 prompt 名并返回预设文本。"""
        self.calls.append(prompt.name)
        return LlmResult(text=self._selection, tokens=16, call_id="LLM-ev-001")


def test_content_tokens_split_camel_case_identifiers() -> None:
    """词法层：标识符按 camelCase 拆开，中文 claim 才可能命中 Java 类名。"""
    assert {"risk", "approval", "rule", "service", "impl"} <= _content_tokens(
        "RiskApprovalRuleServiceImpl"
    )


def test_lexical_recall_finds_evidence_after_camel_split() -> None:
    """词法层：拆词 + 别名表后，「审批规则」类 claim 能召回 RiskApprovalRule* 证据。"""
    chosen = _select_evidence_for_claims(_APPROVAL_CLAIM, _EVIDENCE_SAMPLE)
    assert [item["evidence_id"] for item in chosen] == ["EVD-0001"]


async def test_semantic_recall_adopts_llm_picked_evidence() -> None:
    """语义层：采纳模型给出的编号，虚构与重复编号一律忽略。"""
    llm = _EvidenceSelectStub('{"evidence_ids": ["EVD-0002", "EVD-9999", "EVD-0002"]}')
    chosen = await _select_evidence_with_llm(
        llm, _PROMPTS, _APPROVAL_CLAIM, _EVIDENCE_SAMPLE, set()
    )
    assert chosen is not None
    assert [item["evidence_id"] for item in chosen] == ["EVD-0002"]
    assert llm.calls == ["code_review_evidence_select"]


async def test_semantic_recall_falls_back_when_llm_output_unusable() -> None:
    """语义层：非 JSON / 缺字段 / 全虚构编号一律返回 None，交给词面召回兜底。"""
    for bad in ("不是 JSON", '{"items": []}', '{"evidence_ids": ["EVD-9999"]}'):
        assert (
            await _select_evidence_with_llm(
                _EvidenceSelectStub(bad), _PROMPTS, _APPROVAL_CLAIM, _EVIDENCE_SAMPLE, set()
            )
            is None
        )
    # 目录为空时直接返回空数组，不调用模型
    llm = _EvidenceSelectStub('{"evidence_ids": []}')
    assert await _select_evidence_with_llm(llm, _PROMPTS, _APPROVAL_CLAIM, [], set()) == []
    assert llm.calls == []


async def test_semantic_recall_falls_back_on_llm_failure() -> None:
    """语义层：调用失败退回词面召回，不让评审因召回失败而中断。"""

    class _Boom:
        async def complete(
            self, node: str, prompt: PromptSpec, user_content: str
        ) -> LlmResult:
            """恒抛网络类失败。"""
            raise PipelineError(code="X-LLM-01", message="网关超时", node_id=node)

    assert (
        await _select_evidence_with_llm(
            _Boom(), _PROMPTS, _APPROVAL_CLAIM, _EVIDENCE_SAMPLE, set()
        )
        is None
    )


def test_scan_coverage_reports_truncation_and_integrity_gate() -> None:
    """P0②：接口证据/调用链截断与完整性门禁都要进 scan_coverage，并让 complete 变 false。

    此前的漏洞：接口证据被 140 条上限截断、调用链被 120 条上限截断时，`complete` 仍是 true，
    下游据此把「模型没看到」读成「不存在」。
    """
    from orchestrator.nodes.code_review import (
        _MAX_CALL_CHAINS,
        _MAX_INTERFACE_EVIDENCE,
        _scan_coverage,
    )

    inventory = {"repositories": [{"name": "r", "status": "ok", "files": [{"path": "A.java"}]}]}
    over = [{"id": f"IF-REST-{index:03d}"} for index in range(1, _MAX_INTERFACE_EVIDENCE + 2)]

    coverage = _scan_coverage(inventory, [], over, {"complete": True, "reasons": []})
    assert coverage["interface_evidence_truncated"] is True
    assert coverage["call_chain_truncated"] is True
    assert coverage["interface_evidence_limit"] == _MAX_INTERFACE_EVIDENCE
    assert coverage["call_chain_limit"] == _MAX_CALL_CHAINS
    assert coverage["complete"] is False

    # 未触顶 + 门禁通过 + 完整性记录齐备 → 才算完整
    ok_evidence = [
        {"category": "interface_implementation", "interface_ids": [item["id"]]}
        for item in over[:10]
    ]
    ok = _scan_coverage(inventory, ok_evidence, over[:10], {"complete": True, "reasons": []})
    assert ok["interface_evidence_truncated"] is False
    assert ok["interface_evidence_missing"] == []
    assert ok["call_chain_truncated"] is False
    assert ok["complete"] is True

    # 完整性门禁未通过 → complete=false，原因照抄不改写
    bad = _scan_coverage(
        inventory, [], over[:10], {"complete": False, "reasons": ["r 计数不守恒"]}
    )
    assert bad["complete"] is False
    assert bad["scan_integrity_complete"] is False
    assert bad["scan_integrity_reasons"] == ["r 计数不守恒"]

    # 找不到完整性记录 → 不假装完整
    missing = _scan_coverage(inventory, [], over[:10], None)
    assert missing["complete"] is False
    assert missing["scan_integrity_complete"] is False


def test_scan_coverage_rejects_missing_interface_evidence() -> None:
    """即使接口总数未触发上限，只要有接口没有源码证据也不能报告 complete。"""
    from orchestrator.nodes.code_review import _scan_coverage

    inventory = {"repositories": [{"name": "r", "status": "ok", "files": []}]}
    interfaces = [
        {"id": "IF-REST-001"},
        {"id": "IF-REST-002"},
    ]
    coverage = _scan_coverage(
        inventory,
        [{"category": "interface_implementation", "interface_ids": ["IF-REST-001"]}],
        interfaces,
        {"complete": True, "reasons": []},
    )

    assert coverage["complete"] is False
    assert coverage["interface_evidence_missing"] == ["IF-REST-002"]
