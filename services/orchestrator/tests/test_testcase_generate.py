"""testcase_generate 节点测试：用例生成全链路 + 参数一致性两轮校验 + 脚本渲染。

覆盖：成功渲染（TC 编号/py_compile/low_confidence）、R-GEN-02 违规重生成、
二次仍违规放弃（X-GEN-02）、零用例接口告警（X-GEN-01）、虚构接口（X-LLM-02）、跳序。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import executor.nodes.testcase_generate as testcase_generate_module
import pytest
from common.artifacts import ArtifactStore
from common.engine import Engine
from common.errors import X_ORCH_02, PipelineError
from common.llm import LlmResult
from common.pipeline import NodeStatus
from common.prompts_loader import PromptSpec
from executor.nodes import run_testcase_generate
from executor.nodes.testcase_generate import (
    _complete_cases_with_coverage_recovery,
    _derive_testpoints,
    _enrich_field_descriptions,
    _enrich_testpoint_hints,
    _filter_oracle_violations,
    _filter_param_violations,
    _parse_e2e_cases,
    _parse_llm_cases,
    _parse_testpoint_hints,
    _summarize_violations,
    _synthesize_fallback_cases,
    _validate_batch_coverage,
    _validated_review_test_oracles,
)

from helpers import change_set_payload, requirement_review_payload

_SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"
_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
_JOB = "job-3f2a1b8c-0000-4000-8000-000000000001"


def _observed_request_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    interface = {
        **_INTERFACES[0], "signature": {"method": "POST", "path": "/trial", "summary": "试算"},
        "params": [{"name": "param", "type": "JSONObject", "in": "body", "required": True}],
    }
    source = 'StringUtils.isBlank(param.getString("accountNo"))'
    runtime = (
        '请求：POST /trial\n请求 JSON：{"accountNo":""}\nHTTP status=200\n'
        'Content-Type: application/json\n响应 JSON：{"code":9}\n重复观测次数：2'
    )
    review = {
        "schema_version": "2.2", "job_id": _JOB, "mappings": [],
        "test_oracles": [{
            "oracle_id": "ORC-IF-REST-001-001", "interface_id": "IF-REST-001",
            "category": "exception", "scenario": "账户为空时拒绝请求",
            "preconditions": [{"path": "param.accountNo", "op": "equals", "value": ""}],
            "expected_status": "status=200", "assertions": [{"path": "code", "op": "equals", "value": 9}],
            "evidence_refs": ["EVD-0001", "EVD-0002"],
            "evidence_quotes": [
                {"target": "preconditions[0]", "evidence_ref": "EVD-0001", "quote": source},
                {"target": "expected_status", "evidence_ref": "EVD-0002", "quote": "HTTP status=200"},
                {"target": "assertions[0]", "evidence_ref": "EVD-0002", "quote": '"code":9'},
            ],
        }],
        "evidence_catalog": [
            {"evidence_id": ref, "category": category, "role": role,
             "interface_ids": ["IF-REST-001"], "repo": "test", "file": "Trial.java",
             "start_line": 1, "end_line": 1, "excerpt": excerpt, "excerpt_truncated": False}
            for ref, category, role, excerpt in [
                ("EVD-0001", "interface_implementation", "controller", source),
                ("EVD-0002", "runtime_observation", "runtime_contract", runtime),
            ]
        ],
    }
    return interface, review


def test_observed_contract_rejects_variants_and_false_requirement_trace() -> None:
    interface, review = _observed_request_contract()
    oracles, warnings = _validated_review_test_oracles(review, [interface])
    assert not warnings
    base = {
        "interface_id": interface["id"], "category": "exception",
        "oracle_id": oracles[0]["oracle_id"], "title": "验证另一个业务分支",
        "requirement_id": "REQ-001", "claim_id": "CLM-0001", "testpoint_id": "TP-001",
        "params": {"param": {"accountNo": ""}},
    }
    valid, _ = _filter_oracle_violations([base], [interface], oracles)
    assert valid[0]["title"] == "账户为空时拒绝请求"
    assert not {"requirement_id", "claim_id", "testpoint_id"}.intersection(valid[0])
    variant = {**base, "params": {"param": {"accountNo": "", "BusinType": "H3"}}}
    rejected, issues = _filter_oracle_violations([variant], [interface], oracles)
    assert rejected[0]["oracle_status"] == "unverified"
    assert "精确请求" in issues[0]["message"]


async def test_runtime_only_generation_never_asks_llm_to_invent_variants(env) -> None:
    engine, store, te_store = env
    interface, review = _observed_request_contract()
    store.write(_JOB, "interfaces.json", {"schema_version": "1.0", "job_id": _JOB, "interfaces": [interface]})
    store.write(_JOB, "review_mapping.json", review)
    # 仅有运行观测时，任何 LLM 调用都会使本测试失败。
    class NoLlm:
        async def complete(self, *args, **kwargs):
            raise AssertionError("运行观测不应展开或补产")
    await run_testcase_generate(engine, engine.open_job(_JOB), te_store, NoLlm(), _PROMPTS)
    payload = te_store.read_json(_JOB, "testcases.json")
    assert len(payload["cases"]) == 1
    case = payload["cases"][0]
    assert case["params"] == {"param": {"accountNo": ""}}
    assert case["title"] == "账户为空时拒绝请求"
    assert "requirement_id" not in case and "testpoint_id" not in case


def test_generated_success_case_uses_dto_enum_code(tmp_path: Path) -> None:
    dto = tmp_path / "AutoApprovalAddRequest.java"
    dto.write_text(
        'class AutoApprovalAddRequest {\n'
        '    @ApiModelProperty(value = "业务类型：01一级投标 02存单申购")\n'
        '    private String businessType;\n'
        '    @ApiModelProperty(value = "风控系统：01O32 02合规中心")\n'
        '    private String riskSystem;\n'
        '    @ApiModelProperty(value = "债券类型：01非利率债（含地方债） '
        '02非利率债（不含地方债）")\n'
        '    private java.util.List<String> bondTypes;\n'
        '}\n',
        encoding="utf-8",
    )
    interface = {
        "id": "IF-REST-005",
        "code_location": {"repo": "riskwkb"},
        "params": [{
            "name": "request", "in": "body", "type": "AutoApprovalAddRequest",
            "fields": [
                {"name": "businessType", "description": "业务类型", "source_refs": [
                    {"file": dto.name, "line": 2, "end_line": 3}
                ]},
                {"name": "riskSystem", "description": "风控系统", "source_refs": [
                    {"file": dto.name, "line": 4, "end_line": 5}
                ]},
                {"name": "bondTypes", "description": "债券类型", "source_refs": [
                    {"file": dto.name, "line": 6, "end_line": 8}
                ]},
            ],
        }],
    }
    enriched = _enrich_field_descriptions([interface], {"riskwkb": tmp_path})
    assert "01一级投标" in enriched[0]["params"][0]["fields"][0]["description"]
    cases = [{
        "interface_id": "IF-REST-005", "category": "normal", "expected": "status=200",
        "params": {"request": {
            "businessType": "一级投标", "riskSystem": "O32",
            "bondTypes": ["非利率债（含地方债）", "非利率债（不含地方债）"],
        }},
    }, {
        "interface_id": "IF-REST-005", "category": "exception", "expected": "status_class=4xx",
        "params": {"request": {"businessType": "一级投标", "riskSystem": "O32"}},
    }]
    valid, violations = _filter_param_violations(cases, enriched)
    assert not violations
    assert valid[0]["params"]["request"] == {
        "businessType": "01", "riskSystem": "01", "bondTypes": ["01", "02"]
    }
    assert valid[1]["params"]["request"] == {"businessType": "一级投标", "riskSystem": "O32"}
    invalid, enum_violations = _filter_param_violations([{
        "interface_id": "IF-REST-005", "category": "normal", "expected": "status=200",
        "params": {"request": {"businessType": "01", "riskSystem": "01", "bondTypes": ["国债"]}},
    }], enriched)
    assert not invalid
    assert "body.bondTypes" in enum_violations[0]["message"]

_INTERFACES: list[dict[str, Any]] = [
    {
        "id": "IF-REST-001",
        "protocol": "rest",
        "signature": {"method": "GET", "path": "/api/v1/users", "summary": "查用户"},
        "params": [{"name": "id", "type": "long", "required": True, "in": "query"}],
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
        "params": [
            {"name": "order_id", "type": "unknown", "required": True, "default": "ORDER-000"}
        ],
        "code_location": {
            "repo": "order-center",
            "file": "OrderGrpc.java",
            "start_line": 10,
            "end_line": 20,
        },
        "is_fallback": False,
    },
]

_LLM_GOOD = """{
  "cases": [
    {"interface_id": "IF-REST-001", "requirement_id": "REQ-001", "category": "normal",
     "title": "正常按 ID 查询用户", "params": {"id": 1},
     "expected": "返回 UserDTO 且 id=1"},
    {"interface_id": "IF-REST-001", "category": "boundary",
     "title": "id=0 边界查询", "params": {"id": 0},
     "expected": "返回空列表或 404"},
    {"interface_id": "IF-REST-001", "category": "exception",
     "title": "id 为负数", "params": {"id": -1},
     "expected": "错误码 INVALID_PARAM"},
    {"interface_id": "IF-GRPC-001", "requirement_id": "REQ-002", "category": "normal",
     "title": "正常查询订单", "params": {"order_id": "ORDER-001"},
     "expected": "返回订单详情含 status 字段"},
    {"interface_id": "IF-GRPC-001", "category": "boundary",
     "title": "order_id 为空串", "params": {"order_id": ""},
     "expected": "返回错误 NOT_FOUND"},
    {"interface_id": "IF-GRPC-001", "category": "exception",
     "title": "order_id 非法字符", "params": {"order_id": "@@@@"},
     "expected": "错误码 INVALID"}
  ]
}"""


class _StubLlm:
    """多轮可控 LLM 桩：按调用序返回预设文本。

    拆分维度补全（testpoint_derive）与用例生成共用本节点，但走独立的调用通道：
    `received` 只记录用例生成调用，保持既有断言的语义不被辅助调用打乱。
    """

    def __init__(self, outputs: list[str], hints_output: str = '{"hints":[]}') -> None:
        """预设各轮用例生成输出，以及拆分维度补全的输出。"""
        self._outputs = outputs
        self._hints_output = hints_output
        self.received: list[str] = []
        self.hints_received: list[str] = []
        # 记录两类调用实际携带的重试上限：拆分维度补全应被收窄，用例生成应保持默认。
        self.hints_attempts: list[int | None] = []
        self.case_attempts: list[int | None] = []

    async def complete(
        self,
        node: str,
        prompt: PromptSpec,
        user_content: str,
        max_attempts: int | None = None,
    ) -> LlmResult:
        """按 Prompt 名分流：拆分维度补全不消耗用例生成预设序列。"""
        if prompt.name == "testpoint_derive":
            self.hints_received.append(user_content)
            self.hints_attempts.append(max_attempts)
            return LlmResult(
                text=self._hints_output,
                tokens=64,
                call_id=f"LLM-hint-{len(self.hints_received):03d}",
            )
        self.received.append(user_content)
        self.case_attempts.append(max_attempts)
        text = self._outputs[min(len(self.received), len(self._outputs)) - 1]
        # 主流程已要求模型引用源码 oracle；旧桩的三类用例补上本测试的固定契约。
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("cases"), list):
            context, _ = json.JSONDecoder().raw_decode(user_content)
            for case in payload["cases"]:
                category = case.get("category")
                if category in ("normal", "boundary", "exception"):
                    index = ("normal", "boundary", "exception").index(category) + 1
                    case["oracle_id"] = f"ORC-{case.get('interface_id')}-{index:03d}"
                    case["expected"] = "status=200"
                    case["assertions"] = [{"path": "code", "op": "equals", "value": index}]
                points = [
                    point for point in context.get("testpoints", [])
                    if point.get("interface_id") == case.get("interface_id")
                ]
                if points:
                    point = points[-1] if category == "exception" else points[0]
                    case["testpoint_id"] = point["id"]
                    case["requirement_id"] = point["requirement_id"]
                    if point.get("claim_id"):
                        case["claim_id"] = point["claim_id"]
            text = json.dumps(payload)
        return LlmResult(text=text, tokens=128, call_id=f"LLM-stub-{len(self.received):03d}")


class _FailingLlm:
    """恒定失败的 LLM 桩：验证辅助调用失败不会拖垮节点。"""

    async def complete(
        self,
        node: str,
        prompt: PromptSpec,
        user_content: str,
        max_attempts: int | None = None,
    ) -> LlmResult:
        """抛出网络类失败（X-LLM-01 口径）。"""
        raise PipelineError(code="X-LLM-01", message="网关超时", node_id=node)


@pytest.fixture()
def env(
    tmp_path: Path,
) -> tuple[Engine, ArtifactStore, ArtifactStore]:
    """引擎 + orchestrator 产物卷（前置）+ test-executor 产物卷（节点写权）。"""
    store = ArtifactStore(tmp_path, _SCHEMAS, "orchestrator")
    te_store = ArtifactStore(tmp_path, _SCHEMAS, "test-executor")
    engine = Engine(store)
    state = engine.init_job(_JOB)
    (store.job_dir(_JOB) / "job_input.json").write_text("{}", encoding="utf-8")
    store.write(_JOB, "requirement_review.json", requirement_review_payload(_JOB))
    payloads: list[tuple[str, str, dict[str, Any] | None]] = [
        (
            "req_parse",
            "requirement_items.json",
            {
                "schema_version": "1.0",
                "job_id": _JOB,
                "items": [
                    {"id": "REQ-001", "title": "用户查询", "description": "按 ID 查询用户"},
                    {"id": "REQ-002", "title": "订单查询", "description": "按单号查询订单"},
                ],
            },
        ),
        (
            "code_scan",
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
        ),
        (
            "interface_reconstruct",
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
        ),
        (
            "interface_reconstruct",
            "interfaces.json",
            {"schema_version": "1.0", "job_id": _JOB, "interfaces": _INTERFACES},
        ),
        (
            "code_review",
            "review_mapping.json",
            {
                "schema_version": "1.0",
                "job_id": _JOB,
                "mappings": [
                    {
                        "requirement_id": "REQ-001",
                        "interface_id": "IF-REST-001",
                        "status": "implemented",
                        "confidence": 0.9,
                    },
                    {
                        "requirement_id": "REQ-002",
                        "interface_id": "IF-GRPC-001",
                        "status": "partial",
                        "confidence": 0.6,
                    },
                ],
                "risks": [
                    {
                        "requirement_id": "REQ-002",
                        "interface_id": "IF-GRPC-001",
                        "description": "部分实现",
                    }
                ],
                "missing_features": [],
                "test_oracles": [
                    {
                        "oracle_id": f"ORC-{interface_id}-{index:03d}",
                        "interface_id": interface_id, "category": category,
                        "scenario": f"测试契约 {category}", "preconditions": [],
                        "expected_status": "status=200",
                        "assertions": [{"path": "code", "op": "equals", "value": index}],
                        "evidence_refs": [f"EVD-{interface_index:04d}"],
                        "evidence_quotes": [
                            {"target": "expected_status", "evidence_ref": f"EVD-{interface_index:04d}",
                             "quote": "status(200)"},
                            {"target": "assertions[0]", "evidence_ref": f"EVD-{interface_index:04d}",
                             "quote": f'return Map.of("code", {index});'},
                        ],
                    }
                    for interface_index, interface_id in enumerate(("IF-REST-001", "IF-GRPC-001"), 1)
                    for index, category in enumerate(("normal", "boundary", "exception"), 1)
                ],
                "evidence_catalog": [
                    {
                        "evidence_id": f"EVD-{index:04d}", "category": "interface_implementation",
                        "repo": "test-source", "file": "Contract.java", "start_line": 1, "end_line": 4,
                        "interface_ids": [interface_id], "excerpt_truncated": False,
                        "excerpt": 'status(200); return Map.of("code", 1); return Map.of("code", 2); return Map.of("code", 3);',
                    }
                    for index, interface_id in enumerate(("IF-REST-001", "IF-GRPC-001"), 1)
                ],
            },
        ),
    ]
    # 双产物节点（interface_reconstruct）：两产物都写完才能置完成
    for node_name, filename, payload in payloads:
        engine.mark_running(state, node_name)
        store.write(_JOB, filename, payload)
    engine.mark_completed(state, "req_parse")
    engine.mark_completed(state, "code_scan")
    store.write(_JOB, "change_set.json", change_set_payload(_JOB))
    engine.mark_completed(state, "interface_reconstruct")
    engine.mark_completed(state, "code_review")
    return engine, store, te_store


async def test_testcase_generate_success(env: tuple[Engine, ArtifactStore, ArtifactStore]) -> None:
    """成功：TC 编号、脚本渲染 py_compile 全过、unknown 参数标低置信度、completed。"""
    engine, store, te_store = env
    state = engine.open_job(_JOB)

    path = await run_testcase_generate(engine, state, te_store, _StubLlm([_LLM_GOOD]), _PROMPTS)

    assert path.is_file()
    payload = store.read_json(_JOB, "testcases.json")
    cases = payload["cases"]
    assert [c["id"] for c in cases] == [f"TC-{i:03d}" for i in range(1, 7)]
    # X-GEN-03：unknown 参数接口（IF-GRPC-001）用例标低置信度（代码判定）
    for case in cases:
        if case["interface_id"] == "IF-GRPC-001":
            assert case["low_confidence"] is True
        else:
            assert "low_confidence" not in case
    # 脚本渲染 + 可加载（R-GEN-03）
    for case in cases:
        script = store.job_dir(_JOB) / case["script_path"]
        assert script.is_file()
        assert case["loadable"] is True
        source = script.read_text(encoding="utf-8")
        assert f"def test_{case['id'].replace('-', '_').lower()}(" in source
    # 无告警（两接口均有用例、参数全合规）
    assert "warnings" not in payload
    assert state.node("testcase_generate").status is NodeStatus.COMPLETED


def _read_param_violations(te_store: ArtifactStore) -> dict[str, Any]:
    """读取参数违规明细（内部排查文件）；不存在即视为本节点无参数违规。"""
    path = te_store.job_dir(_JOB) / "case_param_violations.json"
    if not path.exists():
        return {"abandoned_interfaces": [], "rounds": []}
    return json.loads(path.read_text(encoding="utf-8"))


async def test_testcase_generate_param_retry(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """R-GEN-02：一轮违规丢弃，二轮修复通过——无放弃告警。"""
    engine, store, te_store = env
    state = engine.open_job(_JOB)
    # 第一轮：REST 有清单外参数 pageSize；二轮修复
    bad_round = _LLM_GOOD.replace('"params": {"id": 1}', '"params": {"id": 1, "pageSize": 20}')
    llm = _StubLlm([bad_round, _LLM_GOOD])

    await run_testcase_generate(engine, state, te_store, llm, _PROMPTS)

    assert len(llm.received) == 2  # 重生成确实发生
    assert "违规" in llm.received[1] or "参数名" in llm.received[1]
    payload = store.read_json(_JOB, "testcases.json")
    # 无 X-GEN-02 放弃告警
    assert not any("X-GEN-02" in w for w in payload.get("warnings", []))
    # 明细仍留痕：第二轮空违规说明模型自愈，与「被放弃」区分开
    detail = _read_param_violations(te_store)
    assert detail["abandoned_interfaces"] == []
    assert [item["round"] for item in detail["rounds"]] == [1, 2]
    assert detail["rounds"][0]["violations"]
    assert detail["rounds"][1]["violations"] == []
    assert "pageSize" in detail["rounds"][0]["violations"][0]["message"]


def test_summarize_violations_dedupes_and_scopes_to_interface() -> None:
    """告警原因按接口过滤、分句去重：同一接口两轮重复的违规不该写成两遍。"""
    violations = [
        {"interface_id": "IF-REST-001", "params": {}, "message": "清单外参数：pageSize"},
        {
            "interface_id": "IF-REST-001",
            "params": {},
            "message": "清单外参数：pageSize；缺少必填参数：id",
        },
        {"interface_id": "IF-GRPC-001", "params": {}, "message": "缺少必填参数：order_id"},
    ]
    assert (
        _summarize_violations(violations, "IF-REST-001")
        == "清单外参数：pageSize；缺少必填参数：id"
    )
    assert _summarize_violations(violations, "IF-GRPC-001") == "缺少必填参数：order_id"
    assert _summarize_violations(violations, "IF-REST-999") == "（无明细）"


def test_summarize_violations_truncates_long_message() -> None:
    """超长原因必须截断：告警是给人看的，不该被单条 message 撑爆。"""
    violations = [
        {"interface_id": "IF-REST-001", "params": {}, "message": "清单外参数：" + "x" * 500}
    ]
    summary = _summarize_violations(violations, "IF-REST-001")
    assert len(summary) == 301
    assert summary.endswith("…")


def test_filter_rejects_semantic_placeholder_for_unknown_body() -> None:
    """unknown REST body 的文字标签不能伪装成可执行请求。"""
    interfaces = [
        {
            "id": "IF-REST-001",
            "params": [{"name": "body", "type": "unknown", "in": "body"}],
        }
    ]
    bad = [{"interface_id": "IF-REST-001", "params": {"body": "valid_payload"}}]
    valid, violations = _filter_param_violations(bad, interfaces)
    assert valid == []
    assert "禁止语义占位符" in violations[0]["message"]

    concrete = [{"interface_id": "IF-REST-001", "params": {"body": {"id": 1}}}]
    valid, violations = _filter_param_violations(concrete, interfaces)
    assert valid == concrete
    assert violations == []


def test_filter_rejects_missing_required_for_success_and_materializes_declared_default() -> None:
    """成功场景不能漏必填参数；接口显式 default 可确定性补入。"""
    interfaces = [
        {
            "id": "IF-REST-001",
            "params": [
                {"name": "fund_code", "type": "string", "required": True, "in": "query"},
                {
                    "name": "page_size",
                    "type": "integer",
                    "required": True,
                    "in": "query",
                    "default": 20,
                },
            ],
        }
    ]
    cases = [
        {
            "interface_id": "IF-REST-001",
            "category": "normal",
            "expected": "status=200",
            "params": {},
        },
    ]
    valid, violations = _filter_param_violations(cases, interfaces)
    assert valid == []
    assert "fund_code" in violations[0]["message"]


def test_filter_allows_missing_required_for_explicit_client_error() -> None:
    """异常场景可故意省略必填字段，以验证后端的 4xx 校验。"""
    interfaces = [
        {
            "id": "IF-REST-001",
            "params": [{"name": "fund_code", "type": "string", "required": True}],
        }
    ]
    cases = [
        {
            "interface_id": "IF-REST-001",
            "category": "exception",
            "expected": "status_class=4xx",
            "params": {},
        },
    ]
    valid, violations = _filter_param_violations(cases, interfaces)
    assert len(valid) == 1
    assert violations == []

    # gRPC/非 REST 容器的 unknown 标量字段仍是合法参数，不能被误拦截。
    grpc = [{"id": "IF-GRPC-001", "params": [{"name": "order_id", "type": "unknown"}]}]
    valid, violations = _filter_param_violations(
        [{"interface_id": "IF-GRPC-001", "params": {"order_id": "ORDER-001"}}], grpc
    )
    assert len(valid) == 1
    assert violations == []


@pytest.mark.parametrize(
    "container",
    [
        '{"test_cases": [{"interface_id": "IF-REST-001", "category": "normal"}]}',
        '{"response": {"case_list": [{"interface_id": "IF-REST-001", "category": "normal"}]}}',
        (
            '{"content": {"testcases": "[{\\"interface_id\\": '
            '\\"IF-REST-001\\", \\"category\\": \\"normal\\"}]"}}'
        ),
        (
            '{"body": "{\\"cases\\": [{\\"interface_id\\": '
            '\\"IF-REST-001\\", \\"category\\": \\"normal\\"}]}"}'
        ),
    ],
)
def test_parse_llm_cases_accepts_known_model_container_variants(container: str) -> None:
    """容器兼容只放宽结构，不放宽接口 ID/category 校验。"""
    cases = _parse_llm_cases(container, [_INTERFACES[0]])
    assert cases == [{"interface_id": "IF-REST-001", "category": "normal"}]


def test_parse_llm_cases_accepts_complete_single_case_object() -> None:
    """单条完整用例对象可无损提升为 cases 数组。"""
    text = (
        '{"interface_id":"IF-REST-001","category":"normal",'
        '"title":"查询用户","params":{"id":1},"auth_mode":"inherit",'
        '"expected":"status=200"}'
    )
    cases = _parse_llm_cases(text, [_INTERFACES[0]])
    assert len(cases) == 1
    assert cases[0]["params"] == {"id": 1}


def test_batch_coverage_requires_all_interfaces_and_categories() -> None:
    """单条完整对象可解析，但不能绕过批次覆盖门禁。"""
    with pytest.raises(PipelineError) as excinfo:
        _validate_batch_coverage(
            [
                {
                    "interface_id": "IF-REST-001",
                    "category": "normal",
                    "title": "查询用户",
                    "params": {"id": 1},
                    "expected": "status=200",
                }
            ],
            _INTERFACES,
        )
    assert excinfo.value.code == "X-LLM-02"
    assert "IF-GRPC-001" in excinfo.value.message


async def test_testcase_generate_abandon_interface(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """X-GEN-02：二轮仍违规 → 该接口放弃记告警；另一接口用例保留。"""
    engine, store, te_store = env
    state = engine.open_job(_JOB)
    # 两轮 REST 用例都带清单外参数
    bad = _LLM_GOOD.replace('"params": {"id": 1}', '"params": {"id": 1, "pageSize": 20}')
    llm = _StubLlm([bad, bad])

    await run_testcase_generate(engine, state, te_store, llm, _PROMPTS)

    payload = store.read_json(_JOB, "testcases.json")
    assert any("X-GEN-02" in w and "IF-REST-001" in w for w in payload["warnings"])
    # X-GEN-02：整接口放弃（非部分保留）——REST 无用例，仅 GRPC 保留
    assert all(c["interface_id"] == "IF-GRPC-001" for c in payload["cases"])
    assert len(payload["cases"]) == 3
    # 告警必须带具体原因（原先只有接口号，排查时无法区分违规类型）
    target = next(w for w in payload["warnings"] if "IF-REST-001" in w and "X-GEN-02" in w)
    assert "清单外参数" in target and "pageSize" in target
    # 明细落盘：含两轮记录与归一化后的入参快照
    detail = _read_param_violations(te_store)
    assert detail["abandoned_interfaces"] == ["IF-REST-001"]
    assert [item["round"] for item in detail["rounds"]] == [1, 2]
    assert detail["rounds"][0]["violations"][0]["params"].get("pageSize") == 20


async def test_testcase_generate_missing_interface_does_not_invent_required_input(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """模型漏产且源码预期未提供必填 id 时，明确报告缺口，不能编造输入。"""
    engine, _store, te_store = env
    state = engine.open_job(_JOB)
    # 只产 GRPC 用例（REST 漏产）
    grpc_only = """{
      "cases": [
        {"interface_id": "IF-GRPC-001", "requirement_id": "REQ-002", "category": "normal",
         "title": "正常查询订单", "params": {"order_id": "ORDER-001"},
         "expected": "返回订单详情含 status 字段"},
        {"interface_id": "IF-GRPC-001", "category": "boundary",
         "title": "order_id 为空串", "params": {"order_id": ""},
         "expected": "返回错误 NOT_FOUND"},
        {"interface_id": "IF-GRPC-001", "category": "exception",
         "title": "order_id 非法字符", "params": {"order_id": "@@@@"},
         "expected": "错误码 INVALID"}
      ]
    }"""

    await run_testcase_generate(engine, state, te_store, _StubLlm([grpc_only]), _PROMPTS)

    payload = te_store.read_json(_JOB, "testcases.json")
    cases = payload["cases"]
    rest = [c for c in cases if c["interface_id"] == "IF-REST-001"]
    assert not rest
    assert any("IF-REST-001" in warning for warning in payload["warnings"])


async def test_testcase_generate_repairs_cross_node_recon_payload(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """接口还原字段误串（recon_date）时，节点隔离修复轮应恢复 cases。"""
    engine, store, te_store = env
    drift = '{"recon_date":"2026-09-18T00:00:00Z","interfaces":[]}'
    llm = _StubLlm([drift, drift, _LLM_GOOD])

    await run_testcase_generate(engine, engine.open_job(_JOB), te_store, llm, _PROMPTS)

    assert len(llm.received) == 3
    assert len(store.read_json(_JOB, "testcases.json")["cases"]) == 6


async def test_testcase_generate_repairs_empty_object(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """空对象不是可接受降级结果，应进入节点专用修复轮。"""
    engine, store, te_store = env
    llm = _StubLlm(["{}", "{}", _LLM_GOOD])

    await run_testcase_generate(engine, engine.open_job(_JOB), te_store, llm, _PROMPTS)

    assert len(llm.received) == 3
    assert len(store.read_json(_JOB, "testcases.json")["cases"]) == 6


async def test_testcase_generate_all_abandoned(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """全部接口用例失败：X-GEN-02 节点失败。"""
    engine, _store, te_store = env
    state = engine.open_job(_JOB)
    # 两轮所有用例都带清单外参数（两接口全类目违规）
    bad = (
        _LLM_GOOD.replace('"params": {"id": 1}', '"params": {"id": 1, "pageSize": 20}')
        .replace('"params": {"id": 0}', '"params": {"id": 0, "pageSize": 20}')
        .replace('"params": {"id": -1}', '"params": {"id": -1, "pageSize": 20}')
        .replace('"params": {"order_id": "ORDER-001"}', '"params": {"order_id": "X", "extra": 1}')
        .replace('"params": {"order_id": ""}', '"params": {"order_id": "", "extra": 1}')
        .replace('"params": {"order_id": "@@@@"}', '"params": {"order_id": "@", "extra": 1}')
    )
    llm = _StubLlm([bad, bad])

    with pytest.raises(PipelineError) as err:
        await run_testcase_generate(engine, state, te_store, llm, _PROMPTS)

    assert err.value.code == "X-GEN-02"
    assert state.node("testcase_generate").status is NodeStatus.PENDING
    # 失败路径更要有明细：检查点会被成功路径清除，明细只留检查点等于「看不见」
    detail = _read_param_violations(te_store)
    assert set(detail["abandoned_interfaces"]) == {"IF-REST-001", "IF-GRPC-001"}
    assert len(detail["rounds"]) == 2


async def test_testcase_generate_fabricated_interface(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """虚构 interface_id：X-LLM-02。"""
    engine, _store, te_store = env
    state = engine.open_job(_JOB)
    bad = _LLM_GOOD.replace('"IF-REST-001"', '"IF-REST-999"')

    with pytest.raises(PipelineError) as err:
        await run_testcase_generate(engine, state, te_store, _StubLlm([bad]), _PROMPTS)

    assert err.value.code == "X-LLM-02"


async def test_testcase_generate_auth_context(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """C-17：auth 凭据进生成上下文（登录类正向用例可真实通过）。"""
    engine, _store, te_store = env
    state = engine.open_job(_JOB)
    llm = _StubLlm([_LLM_GOOD])

    await run_testcase_generate(
        engine,
        state,
        te_store,
        llm,
        _PROMPTS,
        auth={
            "login_url": "https://svc/api/auth/login/",
            "username": "admin",
            "password": "admin123",
            "insecure_tls": True,
        },
    )

    import json as _json

    context = _json.loads(llm.received[0])
    assert context["auth"] == {
        "login_url": "https://svc/api/auth/login/",
        "username": "admin",
        "password": "admin123",
    }  # insecure_tls 不进 LLM 上下文（与用例设计无关）


async def test_testcase_generate_no_auth_context(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """无 auth：上下文不含该键（旧行为不变）。"""
    engine, _store, te_store = env
    state = engine.open_job(_JOB)
    llm = _StubLlm([_LLM_GOOD])

    await run_testcase_generate(engine, state, te_store, llm, _PROMPTS)

    import json as _json

    context = _json.loads(llm.received[0])
    assert "auth" not in context


def test_e2e_expect_url_does_not_require_selector() -> None:
    """expect_url 是页面级断言，Prompt 约定 selector 为空，不能被元素定位校验拒绝。"""
    cases, dropped = _parse_e2e_cases(
        '{"cases":[{"requirement_id":"REQ-001","title":"登录",'
        '"expected":"跳转首页","steps":['
        '{"action":"goto","value":"/login"},'
        '{"action":"expect_url","selector":"","value":"/home"}]}]}'
    )
    assert dropped == []
    assert cases[0]["steps"][-1] == {"action": "expect_url", "value": "/home"}


def test_parse_e2e_cases_drops_only_offending_case() -> None:
    """单条用例违规只丢该条并记原因，其余用例保留——不让一条坏用例打断整节点。

    实测场景：`goto: /home`（base_url 的路径，不在探测到的路由里）曾让整个
    testcase_generate 以 X-LLM-02 失败，连带 4 个下游节点降级。
    """
    text = json.dumps(
        {
            "cases": [
                {
                    "requirement_id": "REQ-001",
                    "title": "正常登录",
                    "expected": "进入首页",
                    "steps": [{"action": "goto", "value": "/login"}],
                },
                {
                    "requirement_id": "REQ-002",
                    "title": "路由未探测",
                    "expected": "进入首页",
                    "steps": [{"action": "goto", "value": "/home"}],
                },
                {
                    "requirement_id": "REQ-003",
                    "title": "正常登录二",
                    "expected": "进入首页",
                    "steps": [{"action": "goto", "value": "/login"}],
                },
            ]
        }
    )
    cases, dropped = _parse_e2e_cases(text, allowed_paths={"/login"})
    assert [c["title"] for c in cases] == ["正常登录", "正常登录二"]
    assert len(dropped) == 1
    assert "路由未探测" in dropped[0] and "/home" in dropped[0]


def test_parse_e2e_cases_drops_bad_selector_and_action_and_fields() -> None:
    """selector/动作/必填字段三类单条违规都走「丢弃该条」而非抛错。"""
    text = json.dumps(
        {
            "cases": [
                {
                    "requirement_id": "REQ-001",
                    "title": "selector 未探测",
                    "expected": "x",
                    "steps": [{"action": "click", "selector": "#ghost"}],
                },
                {
                    "requirement_id": "REQ-002",
                    "title": "危险动作",
                    "expected": "x",
                    "steps": [{"action": "eval", "value": "alert(1)"}],
                },
                {
                    "requirement_id": "REQ-003",
                    "title": "缺预期",
                    "steps": [{"action": "goto", "value": "/login"}],
                },
                {"requirement_id": "REQ-004", "title": "缺步骤", "expected": "x"},
            ]
        }
    )
    cases, dropped = _parse_e2e_cases(text, allowed_selectors={"#login"}, allowed_paths={"/login"})
    assert cases == []
    assert len(dropped) == 4
    assert any("selector" in d for d in dropped)
    assert any("浏览器动作" in d for d in dropped)
    assert any("expected" in d for d in dropped)
    assert any("缺少步骤" in d for d in dropped)


def test_parse_e2e_cases_still_rejects_broken_container() -> None:
    """整体结构错误仍抛 X-LLM-02（交修复轮），与单条违规的处置区分开。"""
    for text in ('不能解析', '{"data": {"foo": 1}}', '"纯字符串"'):
        with pytest.raises(PipelineError) as err:
            _parse_e2e_cases(text)
        assert err.value.code == "X-LLM-02"


def test_parse_llm_cases_accepts_registered_container_forms() -> None:
    """drift 登记的容器形态（裸数组/别名/包装键）仅解包容器，条目仍走硬校验。"""
    entry = (
        '{"interface_id": "IF-REST-001", "category": "normal", '
        '"title": "正常查询", "params": {"id": 1}, "expected": "返回用户"}'
    )
    # 契约原生形态
    assert _parse_llm_cases(f'{{"cases": [{entry}]}}', _INTERFACES)[0]["title"] == "正常查询"
    # 裸数组：把「只输出 cases 数组」误解为直接输出数组
    assert _parse_llm_cases(f'[{entry}]', _INTERFACES)[0]["title"] == "正常查询"
    # 已登记别名 testcases / items / results
    for alias in ("testcases", "items", "results"):
        assert _parse_llm_cases(f'{{"{alias}": [{entry}]}}', _INTERFACES)[0]["title"] == "正常查询"
    # 已登记包装键 data / result / output 嵌套
    for wrapper in ("data", "result", "output"):
        text = f'{{"{wrapper}": {{"cases": [{entry}]}}}}'
        assert _parse_llm_cases(text, _INTERFACES)[0]["title"] == "正常查询"


def test_parse_llm_cases_rejects_unregistered_forms() -> None:
    """无法无歧义定位用例数组时 X-LLM-02（宁停不猜，禁止拿半份数据下游硬凑）。"""
    for text in (
        '{"data": {"foo": "没有数组"}}',  # 包装键内无已登记容器
        '{"cases": "不是数组"}',  # 容器名对但类型错
        '"纯字符串"',
        '{}',
    ):
        with pytest.raises(PipelineError) as err:
            _parse_llm_cases(text, _INTERFACES)
        assert err.value.code == "X-LLM-02"
        assert "缺少 cases 数组" in err.value.message


async def test_testcase_generate_skip_order_guard(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """code_review 未完成：X-ORCH-02。"""
    engine, _store, te_store = env
    state = engine.open_job(_JOB)
    state.node("code_review").status = NodeStatus.PENDING

    with pytest.raises(PipelineError) as err:
        await run_testcase_generate(engine, state, te_store, _StubLlm([_LLM_GOOD]), _PROMPTS)

    assert err.value.code == X_ORCH_02


def test_synthesize_fallback_cases_covers_missing_categories() -> None:
    """程序兜底：对缺失的 normal/boundary/exception 生成三类确定性用例。"""
    interface: dict[str, Any] = {
        "id": "IF-REST-001",
        "params": [
            {
                "name": "request",
                "type": "MailSendRequest",
                "in": "body",
                "required": True,
                "fields": [{"name": "subject", "type": "String"}],
            }
        ],
    }
    cases = _synthesize_fallback_cases(
        [(interface, ["normal", "boundary", "exception"])]
    )
    assert {c["category"] for c in cases} == {"normal", "boundary", "exception"}
    assert all(c["synthesized"] is True for c in cases)
    assert all(c["interface_id"] == "IF-REST-001" for c in cases)
    # 参数名合规（只用接口定义的 body 参数名，不引入清单外参数）
    for case in cases:
        assert set(case["params"]) <= {"request"}
    # normal 预期 2xx，boundary/exception 预期 4xx
    expected_by_category = {c["category"]: c["expected"] for c in cases}
    assert expected_by_category["normal"] == "status_class=2xx"
    assert expected_by_category["boundary"] == "status_class=4xx"
    assert expected_by_category["exception"] == "status_class=4xx"


def test_synthesize_fallback_cases_preserves_path_param_on_exception() -> None:
    """exception 兜底缺 body 时保留 path 参数占位，避免缺必填 path 参数。"""
    interface: dict[str, Any] = {
        "id": "IF-REST-005",
        "params": [
            {"name": "code", "type": "String", "in": "path", "required": True},
            {"name": "param", "type": "JSONObject", "in": "body", "required": True},
        ],
    }
    cases = _synthesize_fallback_cases([(interface, ["exception"])])
    assert len(cases) == 1
    case = cases[0]
    assert case["params"] == {"code": "test"}
    assert case["expected"] == "status_class=4xx"


def test_synthesize_fallback_cases_uses_simple_param_without_body() -> None:
    """无 body 参数时，用 query/path 简单参数兜底 normal/boundary/exception。"""
    interface: dict[str, Any] = {
        "id": "IF-REST-001",
        "params": [{"name": "id", "type": "long", "in": "query", "required": True}],
    }
    cases = _synthesize_fallback_cases([(interface, ["normal", "boundary", "exception"])])
    assert {c["category"] for c in cases} == {"normal", "boundary", "exception"}
    by_category = {c["category"]: c for c in cases}
    # normal：id 给默认值 1；boundary：id 给边界值 -1；exception：缺必填 id
    assert by_category["normal"]["params"] == {"id": 1}
    assert by_category["boundary"]["params"] == {"id": -1}
    assert by_category["exception"]["params"] == {}


def test_synthesize_fallback_normal_uses_semantic_defaults_for_all_fields() -> None:
    """normal 兜底给所有字段填语义化默认值（非只填第一个字段的裸默认值）。"""
    interface: dict[str, Any] = {
        "id": "IF-REST-001",
        "params": [
            {
                "name": "request",
                "type": "MailSendRequest",
                "in": "body",
                "required": True,
                "fields": [
                    {"name": "requestId", "type": "String"},
                    {"name": "to", "type": "List<String>"},
                    {"name": "subject", "type": "String"},
                ],
            }
        ],
    }
    cases = _synthesize_fallback_cases([(interface, ["normal"])])
    assert len(cases) == 1
    body = cases[0]["params"]["request"]
    assert body == {
        "requestId": "test-request-001",
        "to": ["test@example.com"],
        "subject": "test",
    }


def test_derive_testpoints_iterates_all_mapped_interfaces() -> None:
    """一个需求映射多个接口时，逐个接口派生主流程测试点（C-12）。"""
    requirements: list[dict[str, Any]] = [
        {"id": "REQ-001", "title": "自动审批接口", "description": ""}
    ]
    interfaces: list[dict[str, Any]] = [
        {"id": "IF-REST-001"},
        {"id": "IF-REST-002"},
        {"id": "IF-REST-003"},
    ]
    mappings: list[dict[str, Any]] = [
        {
            "requirement_id": "REQ-001",
            "status": "implemented",
            "interface_ids": ["IF-REST-001", "IF-REST-002"],
        }
    ]
    points = _derive_testpoints(requirements, interfaces, mappings)
    main = [p for p in points if p.get("source") == "REQ"]
    assert {p["interface_id"] for p in main} == {"IF-REST-001", "IF-REST-002"}


def test_derive_testpoints_ignores_fabricated_interface_ids() -> None:
    """映射里的接口 id 不在接口清单时跳过，不伪造接口归属。"""
    requirements: list[dict[str, Any]] = [{"id": "REQ-001", "title": "X", "description": ""}]
    interfaces: list[dict[str, Any]] = [{"id": "IF-REST-001"}]
    mappings: list[dict[str, Any]] = [
        {"requirement_id": "REQ-001", "status": "missing", "interface_ids": ["IF-REAL-999"]}
    ]
    points = _derive_testpoints(requirements, interfaces, mappings)
    # 无有效接口 → 落入「尚未映射到接口」需求级风险点，而非伪造接口
    assert all(p.get("interface_id") is None for p in points)
    assert any(p.get("source") == "RISK" for p in points)


async def test_coverage_recovery_falls_back_when_repair_undecodable() -> None:
    """修复轮输出不可解析时，用首轮用例 + 程序兜底补齐覆盖，而非直接降级。"""
    from common.prompts_loader import PromptSpec

    interfaces: list[dict[str, Any]] = [
        {
            "id": "IF-REST-001",
            "params": [
                {
                    "name": "param",
                    "type": "JSONObject",
                    "in": "body",
                    "required": True,
                    "fields": [{"name": "serialNo", "type": "String"}],
                }
            ],
        },
        {
            "id": "IF-REST-002",
            "params": [{"name": "file", "type": "MultipartFile", "in": "body", "required": True}],
        },
    ]
    # 首轮只覆盖 IF-REST-001 的 normal；修复轮返回不可解析文本
    first = (
        '{"cases": [{"interface_id": "IF-REST-001", "category": "normal",'
        ' "title": "x", "params": {}, "expected": "status_class=2xx"}]}'
    )
    llm = _StubLlm([first, "不是 JSON"])
    prompt = PromptSpec(
        name="testcase_generate",
        version="1.0",
        spec="F-GEN-01",
        owner="丙",
        changelog=(),
        sampling={},
        body="",
    )
    cases = await _complete_cases_with_coverage_recovery(llm, prompt, "{}", interfaces)
    # 首轮 normal + 兜底补齐 boundary/exception + IF-REST-002 三类
    _validate_batch_coverage(cases, interfaces)
    assert {c["category"] for c in cases if c["interface_id"] == "IF-REST-001"} == {
        "normal",
        "boundary",
        "exception",
    }


async def test_coverage_recovery_falls_back_when_first_round_parses_to_empty() -> None:
    """首轮「可解析但零用例」也必须走程序兜底，不能因空列表为假值而跳过补齐。

    线上实测（demo 证据、IF-REST-003 独占一批）：首轮返回空 cases、修复轮结构损坏，
    节点直接以 X-LLM-02 失败——覆盖门禁的确定性兜底被 `if first_cases:` 的假值判定漏掉。
    """
    interfaces = [
        {
            "id": "IF-REST-003",
            "params": [{"name": "id", "type": "Long", "in": "query", "required": True}],
        },
    ]
    llm = _StubLlm(['{"cases": []}', "不是 JSON"])
    prompt = PromptSpec(
        name="testcase_generate",
        version="1.0",
        spec="F-GEN-01",
        owner="丙",
        changelog=(),
        sampling={},
        body="",
    )
    cases = await _complete_cases_with_coverage_recovery(llm, prompt, "{}", interfaces)
    _validate_batch_coverage(cases, interfaces)
    assert {c["category"] for c in cases} == {"normal", "boundary", "exception"}
    assert all(c.get("synthesized") for c in cases)


def test_synthesize_fallback_covers_all_required_body_params() -> None:
    """多 body 参数接口（response + request 都必填）兜底须补齐全部必填参数。"""
    from common.case_validation import blocking_parameter_messages

    interface: dict[str, Any] = {
        "id": "IF-REST-011",
        "params": [
            {"name": "response", "type": "HttpServletResponse", "in": "body", "required": True},
            {
                "name": "request",
                "type": "ComplianceInfoQueryRequest",
                "in": "body",
                "required": True,
                "fields": [{"name": "tagLv1", "type": "String"}],
            },
        ],
    }
    cases = _synthesize_fallback_cases(
        [(interface, ["normal", "boundary", "exception"])]
    )
    by_category = {c["category"]: c for c in cases}
    # normal 必须同时含 response 和 request，避免缺必填被判违规
    assert set(by_category["normal"]["params"].keys()) == {"response", "request"}
    assert blocking_parameter_messages(by_category["normal"], interface) == []


_TP_POINTS: list[dict[str, Any]] = [
    {
        "id": "TP-001",
        "requirement_id": "REQ-001",
        "interface_id": "IF-REST-001",
        "source": "REQ",
        "source_ref": "需求 REQ-001 ↔ 接口 IF-REST-001",
        "scenario": "验证“查询用户”主业务流程",
        "case_split_hints": ["正常主流程", "输入边界与空值", "异常/权限/业务拒绝"],
    },
    {
        "id": "TP-002",
        "requirement_id": "REQ-002",
        "source": "RISK",
        "source_ref": "需求 REQ-002 无接口映射",
        "scenario": "需求“订单查询”尚未映射到接口",
        "case_split_hints": ["端到端主路径", "用户可见反馈", "实现与需求的可追溯性"],
    },
]

_TP_REQS: list[dict[str, Any]] = [
    {"id": "REQ-001", "title": "查询用户", "description": "按 ID 查询用户"},
    {"id": "REQ-002", "title": "订单查询", "description": "按单号查询订单"},
]


def test_parse_testpoint_hints_rejects_unknown_and_duplicate_ids() -> None:
    """只接受本批已有编号；改号、重复、空数组一律丢弃，交由模板 hints 兜底。"""
    batch = [{"testpoint_id": "TP-001"}, {"testpoint_id": "TP-002"}]
    text = """{"hints": [
      {"testpoint_id": "TP-001", "case_split_hints": ["等于边界: id=0"]},
      {"testpoint_id": "TP-001", "case_split_hints": ["重复条目应被忽略"]},
      {"testpoint_id": "TP-999", "case_split_hints": ["虚构编号应被忽略"]},
      {"testpoint_id": "TP-002", "case_split_hints": []},
      {"testpoint_id": "TP-003", "case_split_hints": ["输入外编号"]}
    ]}"""
    assert _parse_testpoint_hints(text, batch) == {"TP-001": ["等于边界: id=0"]}


def test_parse_testpoint_hints_tolerates_code_fence_and_bad_json() -> None:
    """围栏输出可解析；非 JSON 输出返回空字典而不是抛异常。"""
    batch = [{"testpoint_id": "TP-001"}]
    fenced = (
        '```json\n{"hints":[{"testpoint_id":"TP-001",'
        '"case_split_hints":["超长: 超过 1024 字符"]}]}\n```'
    )
    assert _parse_testpoint_hints(fenced, batch) == {"TP-001": ["超长: 超过 1024 字符"]}
    assert _parse_testpoint_hints("抱歉，我无法完成", batch) == {}


async def test_enrich_testpoint_hints_replaces_template_hints() -> None:
    """模型给出专属维度时替换模板 hints，并携带需求原文与接口字段作为依据。"""
    llm = _StubLlm(
        [],
        hints_output=(
            '{"hints":[{"testpoint_id":"TP-001","case_split_hints":'
            '["等于边界: id=0","异常: id 为负数"]}]}'
        ),
    )
    points = await _enrich_testpoint_hints(llm, _PROMPTS, _TP_POINTS, _TP_REQS, _INTERFACES)
    assert points[0]["case_split_hints"] == ["等于边界: id=0", "异常: id 为负数"]
    # 模型未覆盖的测试点保留模板 hints，不产生空数组
    assert points[1]["case_split_hints"] == _TP_POINTS[1]["case_split_hints"]
    # 上下文含需求描述与接口参数，供模型判断具体拆分方向
    context = json.loads(llm.hints_received[0])
    assert context["items"][0]["requirement"]["description"] == "按 ID 查询用户"
    assert context["items"][0]["interface"]["params"][0]["name"] == "id"
    # interface 为 null 的未映射测试点不应伪造接口
    assert context["items"][1]["interface"] is None


async def test_enrich_testpoint_hints_keeps_template_on_llm_failure() -> None:
    """调用失败不抛出：整体沿用模板 hints，绝不让辅助调用拖垮用例生成。"""
    points = await _enrich_testpoint_hints(
        _FailingLlm(), _PROMPTS, _TP_POINTS, _TP_REQS, _INTERFACES
    )
    assert points == _TP_POINTS


async def test_enrich_testpoint_hints_skips_when_prompt_missing(tmp_path: Path) -> None:
    """Prompt 缺失只告警并保留模板 hints，不做成节点失败。"""
    points = await _enrich_testpoint_hints(
        _StubLlm([]), tmp_path, _TP_POINTS, _TP_REQS, _INTERFACES
    )
    assert points == _TP_POINTS


class _ConcurrencyProbeLlm:
    """记录拆分维度补全的并发峰值，用于验证批次是并发而非串行。"""

    def __init__(self, delay: float = 0.02) -> None:
        self._delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self.batch_count = 0
        self.attempts: list[int | None] = []

    async def complete(
        self,
        node: str,
        prompt: PromptSpec,
        user_content: str,
        max_attempts: int | None = None,
    ) -> LlmResult:
        """记录本次调用的并发状态，并为批内每个测试点回一条专属 hint。"""
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.batch_count += 1
        self.attempts.append(max_attempts)
        try:
            await asyncio.sleep(self._delay)
        finally:
            self.in_flight -= 1
        items = json.loads(user_content)["items"]
        return LlmResult(
            text=json.dumps(
                {
                    "hints": [
                        {
                            "testpoint_id": item["testpoint_id"],
                            "case_split_hints": [f"专属维度:{item['testpoint_id']}"],
                        }
                        for item in items
                    ]
                },
                ensure_ascii=False,
            ),
            tokens=32,
            call_id=f"LLM-probe-{self.batch_count:03d}",
        )


async def test_enrich_testpoint_hints_narrows_retry_budget() -> None:
    """拆分维度补全失败可优雅退化，故用收窄后的重试上限，不再按默认口径白等。"""
    llm = _StubLlm([])
    await _enrich_testpoint_hints(llm, _PROMPTS, _TP_POINTS, _TP_REQS, _INTERFACES)
    assert llm.hints_attempts == [testcase_generate_module._TP_HINTS_MAX_ATTEMPTS]
    # docs/12 §三 的口径是「最多 2 次重试」（即 3 次尝试）；收窄后必须仍落在该上限内，
    # 且确实小于默认值，否则本次收窄失去意义。
    assert testcase_generate_module._TP_HINTS_MAX_ATTEMPTS < 3


async def test_enrich_testpoint_hints_runs_batches_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批次有界并发：每批独占一个测试点时应并行执行，并发峰值不超上限。"""
    monkeypatch.setattr(testcase_generate_module, "_TP_HINTS_MAX_CHARS", 1)
    monkeypatch.setattr(testcase_generate_module, "_TP_HINTS_CONCURRENCY", 3)
    points = [{**_TP_POINTS[0], "id": f"TP-{index:03d}"} for index in range(1, 9)]
    probe = _ConcurrencyProbeLlm()
    enriched = await _enrich_testpoint_hints(probe, _PROMPTS, points, _TP_REQS, _INTERFACES)
    # 预算 1 字符 → 每个测试点独占一批；并发不丢批，8 批要全部发出
    assert probe.batch_count == 8
    assert probe.max_in_flight == 3
    assert [point["case_split_hints"] for point in enriched] == [
        [f"专属维度:TP-{index:03d}"] for index in range(1, 9)
    ]


class _PartiallyBrokenLlm:
    """第 N 批抛非 PipelineError，其余批正常返回。"""

    def __init__(self, fail_on: int) -> None:
        self._fail_on = fail_on
        self.calls = 0

    async def complete(
        self,
        node: str,
        prompt: PromptSpec,
        user_content: str,
        max_attempts: int | None = None,
    ) -> LlmResult:
        """首批正常返回，指定批抛底层异常（模拟 httpx 未包装的异常）。"""
        self.calls += 1
        if self.calls == self._fail_on:
            raise RuntimeError("httpx 层未包装的异常")
        items = json.loads(user_content)["items"]
        return LlmResult(
            text=json.dumps(
                {
                    "hints": [
                        {
                            "testpoint_id": item["testpoint_id"],
                            "case_split_hints": [f"维度:{item['testpoint_id']}"],
                        }
                        for item in items
                    ]
                },
                ensure_ascii=False,
            ),
            tokens=8,
            call_id=f"LLM-part-{self.calls:03d}",
        )


async def test_enrich_testpoint_hints_swallows_unexpected_exception() -> None:
    """非 PipelineError 也只退化为模板 hints——辅助调用绝不上抛拖垮节点。"""
    points = await _enrich_testpoint_hints(
        _FailingLlm(), _PROMPTS, _TP_POINTS, _TP_REQS, _INTERFACES
    )
    assert points == _TP_POINTS


async def test_enrich_testpoint_hints_isolates_unexpected_failure_to_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单批抛非预期异常时，仅该批退回模板 hints，其余批已补全的成果必须保住。

    回归护栏：`asyncio.gather` 默认不隔离失败，漏兜一批会让其他批的 hints 一起丢。
    """
    monkeypatch.setattr(testcase_generate_module, "_TP_HINTS_MAX_CHARS", 1)
    points = [{**_TP_POINTS[0], "id": f"TP-{index:03d}"} for index in range(1, 4)]
    llm = _PartiallyBrokenLlm(fail_on=2)
    enriched = await _enrich_testpoint_hints(llm, _PROMPTS, points, _TP_REQS, _INTERFACES)
    assert llm.calls == 3  # 三批都尝试过，异常没有中断调度
    assert enriched[0]["case_split_hints"] == ["维度:TP-001"]
    assert enriched[1]["case_split_hints"] == points[1]["case_split_hints"]  # 退回模板
    assert enriched[2]["case_split_hints"] == ["维度:TP-003"]


def test_parse_llm_cases_expands_long_marker() -> None:
    """超长字符串记号由程序展开：模型只写 __LONG_<n>__，不再逐字符手写 1025 个字符。"""
    raw = json.dumps(
        {
            "cases": [
                {
                    "interface_id": "IF-REST-001",
                    "category": "boundary",
                    "title": "超长 name 边界",
                    "params": {"body": {"name": "__LONG_1025__"}, "tags": ["__LONG_8__", "keep"]},
                    "expected": "status_class=4xx",
                }
            ]
        },
        ensure_ascii=False,
    )
    cases = _parse_llm_cases(raw, _INTERFACES)
    params = cases[0]["params"]
    assert params["body"]["name"] == "x" * 1025
    assert len(params["body"]["name"]) == 1025
    # 递归到数组元素；非记号字符串原样保留
    assert params["tags"] == ["x" * 8, "keep"]


def test_expand_long_markers_leaves_out_of_range_and_partial_forms() -> None:
    """越界长度、零长度、以及「记号只是子串」三种形态都原样保留，宁可不展开也不误伤。"""
    expand = testcase_generate_module._expand_long_markers
    over = f"__LONG_{testcase_generate_module._LONG_MARKER_MAX + 1}__"
    assert expand(over) == over
    assert expand("__LONG_0__") == "__LONG_0__"
    assert expand("前缀 __LONG_10__") == "前缀 __LONG_10__"
    assert expand("__LONG_ABC__") == "__LONG_ABC__"
    # 非字符串原样返回；嵌套结构继续递归
    assert expand({"a": [1, None, True, "__LONG_3__"]}) == {"a": [1, None, True, "xxx"]}
    assert expand(1025) == 1025


def test_implementation_excerpt_keeps_complete_large_method(tmp_path: Path) -> None:
    """长方法超过旧行数和字符上限时，尾部仍完整传入。"""
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "C.java").write_text(
        "\n".join(f"line{i}" + "x" * 100 for i in range(1, 121)), encoding="utf-8"
    )
    excerpt = testcase_generate_module._implementation_excerpt(
        {
            "code_location": {
                "repo": "svc",
                "file": "C.java",
                "start_line": 10,
                "end_line": 120,
            }
        },
        {"svc": repo},
    )
    assert excerpt is not None
    assert excerpt["excerpt"].startswith("line10")
    assert excerpt["truncated"] is False
    assert len(excerpt["excerpt"]) > 8_000
    assert excerpt["excerpt"].endswith("line120" + "x" * 100)
    assert excerpt["end_line"] == 120


def test_implementation_excerpt_rejects_escape_and_missing(tmp_path: Path) -> None:
    """越出仓库根 / 文件不存在 / 仓库未知一律不注入——宁缺不猜。"""
    repo = tmp_path / "svc"
    repo.mkdir()
    (tmp_path / "outside.java").write_text("secret", encoding="utf-8")
    excerpt = testcase_generate_module._implementation_excerpt
    assert (
        excerpt(
            {"code_location": {"repo": "svc", "file": "../outside.java", "start_line": 1}},
            {"svc": repo},
        )
        is None
    )
    assert (
        excerpt(
            {"code_location": {"repo": "svc", "file": "nope.java", "start_line": 1}},
            {"svc": repo},
        )
        is None
    )
    assert (
        excerpt(
            {"code_location": {"repo": "other", "file": "C.java", "start_line": 1}},
            {"svc": repo},
        )
        is None
    )
    # 仓库根没配（code_inventory 缺该仓库）时不注入
    assert excerpt({"code_location": {"repo": "svc", "file": "C.java"}}, {}) is None


def test_implementation_excerpt_redacts_secret_values(tmp_path: Path) -> None:
    """源码里的示例口令先脱敏再交给模型（与 code_review 证据采集同一口径）。"""
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "C.java").write_text('String password = "p@ssw0rd";\n', encoding="utf-8")
    excerpt = testcase_generate_module._implementation_excerpt(
        {"code_location": {"repo": "svc", "file": "C.java", "start_line": 1, "end_line": 1}},
        {"svc": repo},
    )
    assert excerpt is not None
    assert "p@ssw0rd" not in excerpt["excerpt"]
    assert "<redacted>" in excerpt["excerpt"]


def test_build_batch_context_injects_implementation_without_mutating(
    tmp_path: Path,
) -> None:
    """批上下文按接口注入实现片段；读不到的不带该字段，且不改动调用方的接口对象。"""
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "C.java").write_text("class C {\n  void m() {}\n}\n", encoding="utf-8")
    interfaces: list[dict[str, Any]] = [
        {
            "id": "IF-REST-001",
            "code_location": {"repo": "svc", "file": "C.java", "start_line": 1, "end_line": 2},
        },
        {
            "id": "IF-REST-002",
            "code_location": {
                "repo": "svc",
                "file": "Missing.java",
                "start_line": 1,
                "end_line": 2,
            },
        },
    ]
    context = json.loads(
        testcase_generate_module._build_batch_context(
            interfaces, [], [], [], None, 1, 1, {"svc": repo}
        )
    )
    first, second = context["interfaces"]
    assert "void m()" in first["implementation"]["excerpt"]
    assert first["implementation"]["truncated"] is False
    assert "implementation" not in second
    # 原接口对象不被就地修改（同一批 interfaces 还要用于指纹与覆盖判断）
    assert "implementation" not in interfaces[0]
    # 未传 repo_roots 时不注入（老调用方行为不变）
    plain = json.loads(
        testcase_generate_module._build_batch_context(interfaces, [], [], [], None, 1, 1)
    )
    assert all("implementation" not in item for item in plain["interfaces"])


def test_runtime_injected_params_are_neither_required_nor_defaulted() -> None:
    """框架注入的容器参数既不算缺必填，也不补默认值（补出来的占位串会被当请求参数发出去）。"""
    from common.case_validation import (
        inspect_case_parameters,
        is_runtime_injected_param,
        materialize_declared_defaults,
    )

    interface = {
        "id": "IF-REST-042",
        "params": [
            {
                "name": "response",
                "type": "HttpServletResponse",
                "required": True,
                "in": "unknown",
                "default": "unknown",
            },
            {"name": "query", "type": "ContactAuditQuery", "required": True, "in": "body"},
        ],
    }
    # 类型按简单名匹配，容忍包名前缀；业务类型不能被误判
    assert is_runtime_injected_param({"type": "javax.servlet.http.HttpServletResponse"}) is True
    assert is_runtime_injected_param({"type": "HttpServletRequest"}) is True
    assert is_runtime_injected_param({"type": "MultipartFile"}) is False
    assert is_runtime_injected_param({"type": "ContactAuditQuery"}) is False
    assert is_runtime_injected_param({}) is False

    case = {
        "interface_id": "IF-REST-042",
        "category": "normal",
        "params": {"query": {"combCode": "X"}},
        "expected": "status=200",
    }
    normalized = materialize_declared_defaults(case, interface)
    assert "response" not in normalized["params"]  # 不补 "unknown" 占位值
    assert inspect_case_parameters(normalized, interface).missing_required == ()  # 不算缺必填
    # 业务必填参数照旧要查
    assert inspect_case_parameters({"params": {}}, interface).missing_required == ("query",)


def test_implementation_excerpt_prefers_changed_lines(tmp_path: Path) -> None:
    """#52：有 change_evidence 时，注入的是**本次实际改动的代码**，不是整个方法体。"""
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "C.java").write_text("\n".join(f"line{i}" for i in range(1, 41)), encoding="utf-8")

    excerpt = testcase_generate_module._implementation_excerpt(
        {
            "code_location": {
                "repo": "svc",
                "file": "C.java",
                "start_line": 1,
                "end_line": 40,
            },
            "change_evidence": {
                "level": "body",
                "changed_snippets": [
                    {
                        "file": "C.java",
                        "start_line": 12,
                        "end_line": 13,
                        "text": "if (id == null) {\n    throw new IllegalArgumentException();\n}",
                    }
                ],
                "truncated": False,
            },
        },
        {"svc": repo},
    )

    assert excerpt is not None
    assert excerpt["source"] == "changed_lines"
    assert excerpt["level"] == "body"
    assert "line12" not in excerpt["excerpt"]  # 不再取工作区文件（版本可能不一致）
    assert "IllegalArgumentException" in excerpt["excerpt"]
    assert excerpt["start_line"] == 12
    assert excerpt["end_line"] == 13


def test_implementation_excerpt_falls_back_to_method_body(tmp_path: Path) -> None:
    """无 change_evidence（未圈定变更范围）时退回整个方法体，行为与既有口径一致。"""
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "C.java").write_text("\n".join(f"line{i}" for i in range(1, 11)), encoding="utf-8")

    excerpt = testcase_generate_module._implementation_excerpt(
        {
            "code_location": {
                "repo": "svc",
                "file": "C.java",
                "start_line": 3,
                "end_line": 5,
            }
        },
        {"svc": repo},
    )

    assert excerpt is not None
    assert excerpt["source"] == "method_body"
    assert excerpt["excerpt"].splitlines() == ["line3", "line4", "line5"]


def test_implementation_excerpt_redacts_secrets_in_changed_lines() -> None:
    """变更行片段同样要脱敏（与 code_review 同一口径）。"""
    excerpt = testcase_generate_module._implementation_excerpt(
        {
            "change_evidence": {
                "level": "callee",
                "changed_snippets": [
                    {
                        "file": "S.java",
                        "start_line": 7,
                        "end_line": 7,
                        "text": 'String password = "p@ss";',
                    }
                ],
            }
        },
        {},
    )

    assert excerpt is not None
    assert "p@ss" not in excerpt["excerpt"]
    assert "<redacted>" in excerpt["excerpt"]


def test_changed_source_retains_all_large_fragments() -> None:
    fragments = ["x" * 12000, "TAIL_MARKER"]
    excerpt = testcase_generate_module._changed_excerpt({
        "change_evidence": {"truncated": False, "changed_snippets": [
            {"file": "S.java", "start_line": i, "end_line": i, "text": text}
            for i, text in enumerate(fragments, 1)
        ]},
    })
    assert excerpt is not None and not excerpt["truncated"]
    assert all(fragment in excerpt["excerpt"] for fragment in fragments)


def test_old_truncated_change_uses_complete_target_method(tmp_path: Path) -> None:
    (tmp_path / "S.java").write_text("first\nlast\n", encoding="utf-8")
    excerpt = testcase_generate_module._implementation_excerpt({
        "code_location": {"repo": "svc", "file": "S.java", "start_line": 1, "end_line": 2},
        "change_evidence": {"truncated": True, "changed_snippets": [
            {"file": "S.java", "start_line": 1, "end_line": 1, "text": "first"},
        ]},
    }, {"svc": tmp_path})
    assert excerpt is not None
    assert excerpt["source"] == "method_body"
    assert excerpt["excerpt"] == "first\nlast"
