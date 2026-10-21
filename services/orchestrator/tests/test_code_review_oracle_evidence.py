"""Focused regressions for source-backed oracle evidence and status proof."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from common.errors import PipelineError
from common.llm import LlmResult

from orchestrator.nodes.code_review import (
    _canonicalize_request_condition_paths,
    _collect_oracle_auxiliary_evidence,
    _collect_oracle_called_method_evidence,
    _default_http_status_evidence,
    _derive_test_oracles_with_llm,
    _oracle_candidate_preflight_reason,
    _oracle_source_branches,
    _oracle_uncontrolled_state_reason,
    _parse_test_oracle_response,
    _remove_empty_path_assertions,
)


def test_called_method_evidence_follows_private_and_imported_validator(tmp_path: Path) -> None:
    controller_file = "module/src/main/java/sample/TrialController.java"
    validator_file = "module/src/main/java/sample/RequestValidator.java"
    controller = tmp_path / controller_file
    validator = tmp_path / validator_file
    controller.parent.mkdir(parents=True)
    controller.write_text(
        "import sample.RequestValidator;\n"
        "public class TrialController {\n"
        '  private static final String FIELD = "accountNo";\n'
        '  private static final String MESSAGE = "required";\n'
        "  public Object trial(Object request) {\n"
        "    validate(request);\n"
        "    RequestValidator.check(request);\n"
        "    return request;\n"
        "  }\n"
        "  private void validate(Object request) {\n"
        "    if (request == null) throw new IllegalArgumentException(MESSAGE + FIELD);\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    validator.write_text(
        "package sample;\n"
        "public final class RequestValidator {\n"
        "  public static void check(Object request) {\n"
        '    if (request == null) throw new IllegalArgumentException("empty");\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    inventory = {
        "repositories": [
            {
                "name": "sample",
                "snapshot_path": str(tmp_path),
                "files": [{"path": controller_file}, {"path": validator_file}],
            }
        ]
    }
    interface = {
        "id": "IF-REST-001",
        "protocol": "rest",
        "code_location": {
            "repo": "sample",
            "file": controller_file,
            "start_line": 5,
            "end_line": 9,
        },
    }

    evidence = _collect_oracle_called_method_evidence(inventory, [interface], [])
    excerpts = "\n".join(item["excerpt"] for item in evidence)
    assert "private void validate" in excerpts
    assert 'String FIELD = "accountNo"' in excerpts
    assert 'String MESSAGE = "required"' in excerpts
    assert "public static void check" in excerpts


def test_default_status_uses_all_class_annotations() -> None:
    item = {
        "interface": {
            "protocol": "rest",
            "signature": {"method": "POST", "path": "/risk/trial"},
        },
        "evidence": [
            {
                "role": "controller_header",
                "file": "TrialController.java",
                "evidence_id": "EVD-1",
                "excerpt": "@RestController",
            },
            {
                "role": "controller_header",
                "file": "TrialController.java",
                "evidence_id": "EVD-2",
                "excerpt": '@RequestMapping("risk")',
            },
            {
                "role": "controller",
                "file": "TrialController.java",
                "evidence_id": "EVD-3",
                "excerpt": (
                    '@PostMapping("/trial")\npublic Object trial() {\n'
                    "Object response = new Object();\nreturn response;\n}"
                ),
            },
        ],
    }
    proof = _default_http_status_evidence(item)
    assert proof is not None
    assert {quote["quote"] for quote in proof[2]} == {"@RestController", "return response;"}


def test_exception_advice_is_found_by_annotation_not_filename(tmp_path: Path) -> None:
    advice_file = "module/src/main/java/sample/ApiErrors.java"
    source = tmp_path / advice_file
    source.parent.mkdir(parents=True)
    source.write_text(
        "@RestControllerAdvice\n"
        "public class ApiErrors {\n"
        "  public Object handle(Exception error) { return error; }\n"
        "}\n",
        encoding="utf-8",
    )
    inventory = {
        "repositories": [
            {
                "name": "sample",
                "snapshot_path": str(tmp_path),
                "files": [{"path": advice_file}],
            }
        ]
    }
    interface = {
        "id": "IF-REST-001",
        "protocol": "rest",
        "code_location": {"repo": "sample", "file": "TrialController.java"},
    }
    evidence = _collect_oracle_auxiliary_evidence(inventory, [interface])
    assert len(evidence) == 1
    assert evidence[0]["role"] == "exception_advice"
    assert "@RestControllerAdvice" in evidence[0]["excerpt"]


def test_throw_quote_cannot_prove_http_500() -> None:
    source = 'throw new RuntimeException("Request Type Not Supported");'
    raw = {
        "interface_id": "IF-REST-001",
        "category": "exception",
        "scenario": "unknown code",
        "preconditions": [],
        "expected_status": "status=500",
        "assertions": [],
        "evidence_refs": ["EVD-1"],
        "evidence_quotes": [
            {
                "target": "expected_status",
                "evidence_ref": "EVD-1",
                "quote": source,
            }
        ],
    }
    with pytest.raises(PipelineError, match="HTTP 状态源码依据"):
        _parse_test_oracle_response(
            json.dumps({"oracles": [raw]}),
            [{"id": "IF-REST-001"}],
            {"IF-REST-001": {"EVD-1": source}},
            strict_items=True,
        )


def test_blank_path_assertion_is_removed_with_quote_reindex() -> None:
    raw = {
        "oracles": [
            {
                "assertions": [
                    {"path": "code", "op": "equals", "value": 1},
                    {"path": "", "op": "exists"},
                    {"path": "message", "op": "non_empty"},
                ],
                "evidence_quotes": [
                    {"target": "assertions[0]", "evidence_ref": "EVD-1", "quote": "one"},
                    {"target": "assertions[1]", "evidence_ref": "EVD-1", "quote": "blank"},
                    {"target": "assertions[2]", "evidence_ref": "EVD-1", "quote": "message"},
                ],
            }
        ]
    }
    result = json.loads(_remove_empty_path_assertions(json.dumps(raw)))
    oracle = result["oracles"][0]
    assert [item["path"] for item in oracle["assertions"]] == ["code", "message"]
    assert [item["target"] for item in oracle["evidence_quotes"]] == [
        "assertions[0]",
        "assertions[1]",
    ]


@pytest.mark.parametrize(
    "path,source",
    [
        ("accountNo", 'param.getString("accountNo")'),
        ("autoApprovalFlag", 'param.getString("autoApprovalFlag")'),
        ("GroupList[0].ExDestination", 'param.getJSONArray("GroupList")'),
        ("riskChannel", 'String RISK_CHANNEL = "riskChannel"; param.getInteger(RISK_CHANNEL)'),
    ],
)
def test_bare_request_fields_have_source_proven_body_root(path: str, source: str) -> None:
    interface = {"params": [{"name": "param", "in": "body", "fields": []}]}
    raw = [{"path": path, "op": "equals", "value": "invalid"}]
    result = _canonicalize_request_condition_paths(raw, interface, source)
    assert result[0]["path"] == f"param.{path}"
    assert raw[0]["path"] == path
    assert _canonicalize_request_condition_paths(result, interface, source) == result


def test_request_path_mapping_rejects_internal_unknown_and_ambiguous_fields() -> None:
    interface = {
        "params": [
            {"name": "param", "in": "body", "fields": [{"name": "shared"}]},
            {"name": "other", "in": "body", "fields": [{"name": "shared"}]},
            {"name": "code", "in": "path"},
        ]
    }
    paths = ["errorCode", "unknown", "shared", "code", "param.shared"]
    conditions = [{"path": path, "op": "exists"} for path in paths]
    result = _canonicalize_request_condition_paths(
        conditions, interface, 'result.getString("errorCode"); param.getString("code");'
    )
    assert [item["path"] for item in result] == paths


def test_bare_array_field_preflight_does_not_become_internal_state() -> None:
    interface = {"params": [{"name": "param", "in": "body", "fields": []}]}
    evidence = [
        {
            "role": "controller",
            "excerpt": ('param.getJSONArray("GroupList"); item.getString("ExDestination");'),
        }
    ]
    oracle = {
        "category": "normal",
        "scenario": "invalid market",
        "preconditions": [
            {"path": "GroupList[0].ExDestination", "op": "equals", "value": "INVALID"}
        ],
        "assertions": [{"path": "code", "op": "equals", "value": "INVALID"}],
    }
    assert _oracle_candidate_preflight_reason(oracle, evidence, interface) is None
    assert _oracle_uncontrolled_state_reason(oracle, evidence, interface) is None


def test_explicit_null_body_is_framework_boundary() -> None:
    interface = {"params": [{"name": "param", "in": "body"}]}
    evidence = [
        {
            "role": "controller",
            "excerpt": (
                "public Object trial(@RequestBody JSONObject param) { return service.trial(param); }"
            ),
        }
    ]
    oracle = {
        "category": "exception",
        "scenario": "null body",
        "preconditions": [{"path": "param", "op": "equals", "value": None}],
        "assertions": [{"path": "code", "op": "exists"}],
    }
    assert "框架拦截" in str(_oracle_candidate_preflight_reason(oracle, evidence, interface))


def test_existing_registry_normal_state_does_not_require_registry_mutation() -> None:
    oracle = {
        "category": "normal",
        "scenario": "EXECUTOR_SERVICE_CACHE中存在对应服务实例",
        "preconditions": [{"path": "code", "op": "equals", "value": "TRIAL"}],
        "assertions": [{"path": "code", "op": "equals", "value": "OK"}],
    }
    interface = {"params": [{"name": "code", "in": "path"}]}
    assert _oracle_uncontrolled_state_reason(oracle, [], interface) is None
    oracle["scenario"] = "EXECUTOR_SERVICE_CACHE中不存在对应服务实例"
    assert "service_registry" in str(_oracle_uncontrolled_state_reason(oracle, [], interface))


async def test_exists_only_candidate_gets_business_assertion_repair() -> None:
    source = (
        "@RestController public Object trial(@RequestBody JSONObject param) {"
        'param.getString("accountNo"); return Map.of("code", "OK"); }'
    )
    interface = {
        "id": "IF-REST-001",
        "protocol": "rest",
        "params": [{"name": "param", "in": "body", "fields": [{"name": "accountNo"}]}],
    }
    evidence = [
        {
            "evidence_id": "EVD-0001",
            "category": "interface_implementation",
            "interface_ids": ["IF-REST-001"],
            "role": "controller",
            "excerpt": source,
        }
    ]
    oracle = {
        "interface_id": "IF-REST-001",
        "category": "normal",
        "scenario": "normal request",
        "preconditions": [{"path": "accountNo", "op": "equals", "value": "ACCOUNT"}],
        "expected_status": "status=200",
        "assertions": [{"path": "code", "op": "exists"}],
        "evidence_refs": ["EVD-0001"],
        "evidence_quotes": [
            {
                "target": "preconditions[0]",
                "evidence_ref": "EVD-0001",
                "quote": 'param.getString("accountNo")',
            },
            {"target": "expected_status", "evidence_ref": "EVD-0001", "quote": "@RestController"},
            {
                "target": "assertions[0]",
                "evidence_ref": "EVD-0001",
                "quote": 'Map.of("code", "OK")',
            },
        ],
    }

    class RepairLlm:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, node, prompt, user_content):
            self.calls += 1
            result = json.loads(json.dumps(oracle))
            if self.calls > 1:
                assert "仅检查响应字段存在" in user_content
                result["assertions"] = [{"path": "code", "op": "equals", "value": "OK"}]
            return LlmResult(json.dumps({"oracles": [result]}), 1, str(self.calls))

    llm = RepairLlm()
    parsed, warnings = await _derive_test_oracles_with_llm(
        llm,
        Path(__file__).resolve().parents[3] / "prompts",
        [interface],
        evidence,
    )
    assert llm.calls == 2
    assert parsed[0]["preconditions"][0]["path"] == "param.accountNo"
    assert parsed[0]["assertions"][0] == {"path": "code", "op": "equals", "value": "OK"}
    assert not warnings


def test_downstream_guard_origin_survives_arbitrary_local_variable_names() -> None:
    interface = {"params": [{"name": "param", "in": "body"}]}
    source = (
        'Object anything = standardRiskTrialService.trial(param);'
        'if (anything == null) { return error(); }'
        'Object different = anything.getO32TrialObj();'
        'if (!"0".equals(different.getErrorCode())) { return response; }'
    )
    evidence = [{"evidence_id": "EVD-0001", "role": "service_impl", "excerpt": source}]
    branches = _oracle_source_branches(interface, {"EVD-0001": source})
    assert len(branches) == 2
    assert all(branch["control_kind"] == "downstream_response" for branch in branches)
    for branch in branches:
        oracle = {
            "branch_id": branch["branch_id"], "category": "normal", "scenario": "arbitrary title",
            "preconditions": [{"path": "param", "op": "not_equals", "value": None}],
            "assertions": [{"path": "code", "op": "equals", "value": "FAIL"}],
        }
        assert "downstream_response" in str(_oracle_uncontrolled_state_reason(oracle, evidence, interface))
        assert _oracle_uncontrolled_state_reason(
            oracle, evidence, interface, control_fixture_kinds={"downstream_response"},
        ) is None


def test_request_precondition_must_match_selected_guard() -> None:
    interface = {"params": [{"name": "param", "in": "body", "fields": [{"name": "riskChannel"}]}]}
    source = 'Integer riskChannel = param.getInteger("riskChannel"); if (riskChannel == 1) { return std(); }'
    evidence = [{"evidence_id": "EVD-0001", "role": "controller", "excerpt": source}]
    branch = _oracle_source_branches(interface, {"EVD-0001": source})[0]
    assert branch["control_kind"] == "request"
    oracle = {
        "branch_id": branch["branch_id"], "category": "normal", "scenario": "STD",
        "preconditions": [{"path": "param.riskChannel", "op": "equals", "value": 2}],
        "assertions": [{"path": "code", "op": "equals", "value": "OK"}],
    }
    assert "不能证明" in str(_oracle_uncontrolled_state_reason(oracle, evidence, interface))
    oracle["preconditions"][0]["value"] = 1
    assert _oracle_uncontrolled_state_reason(oracle, evidence, interface) is None


def test_combined_control_origins_require_every_fixture() -> None:
    interface = {"params": [{"name": "param", "in": "body"}]}
    source = (
        'Object configuredExecutors = executorProperties.getComplexExecutors();'
        'Object result = riskService.trial(configuredExecutors, param);'
        'if (result == null) { return response; }'
    )
    evidence = [{"evidence_id": "EVD-0001", "role": "service_impl", "excerpt": source}]
    oracle = {
        "branch_id": "EVD-0001:if:1", "category": "normal", "scenario": "combined control",
        "preconditions": [], "assertions": [{"path": "code", "op": "equals", "value": "FAIL"}],
    }
    reason = _oracle_uncontrolled_state_reason(
        oracle, evidence, interface, control_fixture_kinds={"application_config"}
    )
    assert reason and "downstream_response" in reason
    assert _oracle_uncontrolled_state_reason(
        oracle, evidence, interface,
        control_fixture_kinds={"application_config", "downstream_response"},
    ) is None


def test_branch_locator_and_missing_field_semantics_are_validated() -> None:
    source = 'if (param == null) { return response; } return response;'
    interface = {"id": "IF-REST-001", "params": [{"name": "param", "in": "body"}]}
    raw = {
        "interface_id": "IF-REST-001", "category": "normal", "scenario": "empty result",
        "preconditions": [], "expected_status": "status=200",
        "assertions": [{"path": "items", "op": "not_exists"}],
        "evidence_refs": ["EVD-0001"],
        "evidence_quotes": [
            {"target": "expected_status", "evidence_ref": "EVD-0001", "quote": "return response;"},
            {"target": "assertions[0]", "evidence_ref": "EVD-0001", "quote": "return response;"},
        ],
    }
    inputs = {"IF-REST-001": {"EVD-0001": source}}
    with pytest.raises(PipelineError, match="branch_id"):
        _parse_test_oracle_response(json.dumps({"oracles": [raw]}), [interface], inputs)
    raw["branch_id"] = "EVD-0001:if:1"
    with pytest.raises(PipelineError, match="序列化忽略证据"):
        _parse_test_oracle_response(json.dumps({"oracles": [raw]}), [interface], inputs)
    raw["assertions"] = [{"path": "items", "op": "exists"}]
    parsed = _parse_test_oracle_response(json.dumps({"oracles": [raw]}), [interface], inputs)
    assert parsed[0]["branch_id"] == "EVD-0001:if:1"
    assert any(quote["target"] == "branch" and quote["quote"] == "if (param == null)"
               for quote in parsed[0]["evidence_quotes"])


async def test_runtime_request_match_does_not_hide_uncontrolled_downstream_branch() -> None:
    interface = {
        "id": "IF-REST-001", "protocol": "rest",
        "params": [{"name": "param", "in": "body"}],
    }
    source = (
        '@RestController public Object trial(JSONObject param) {'
        'Object result = riskService.trial(param);'
        'if (result == null) { return response; } return result; }'
    )
    ref = "EVD-0001"
    oracle = {
        "interface_id": "IF-REST-001", "branch_id": ref + ":if:1",
        "category": "exception", "scenario": "内部 Service 返回空对象",
        "preconditions": [{"path": "param", "op": "exists"}],
        "expected_status": "status=200",
        "assertions": [{"path": "code", "op": "equals", "value": "UNPROVEN"}],
        "evidence_refs": [ref],
        "evidence_quotes": [
            {"target": "preconditions[0]", "evidence_ref": ref, "quote": "JSONObject param"},
            {"target": "expected_status", "evidence_ref": ref, "quote": "@RestController"},
            {"target": "assertions[0]", "evidence_ref": ref, "quote": "return response;"},
        ],
    }

    class Llm:
        async def complete(self, node, prompt, user_content):
            return LlmResult(json.dumps({"oracles": [oracle]}), 1, "replay")

    parsed, warnings = await _derive_test_oracles_with_llm(
        Llm(), Path(__file__).resolve().parents[3] / "prompts", [interface],
        [{"evidence_id": ref, "category": "interface_implementation",
          "interface_ids": [interface["id"]], "role": "controller", "excerpt": source}],
        runtime_observations=[{"interface_id": interface["id"], "request_body": {"id": 0}}],
    )
    assert not parsed
    assert any("downstream_response" in warning for warning in warnings)
