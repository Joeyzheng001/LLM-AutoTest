"""Keep source-backed scenario conditions separate from HTTP request fields.

These conditions describe the boundary of the system under test. They do not
assert that an external service, database, or JVM has already been prepared.
"""

from __future__ import annotations

from typing import Any

CONTROL_KINDS = {"downstream_response", "application_config", "service_registry"}
STATE_KINDS = CONTROL_KINDS | {"test_data"}


def normalize_expected_behavior(
    value: Any, evidence: dict[str, dict[str, Any]], interface_id: str,
    refs: list[str],
) -> dict[str, str] | None:
    if value is None:
        return None
    keys = {"description", "evidence_ref", "quote"}
    if not isinstance(value, dict) or set(value) != keys or any(
        not isinstance(value[key], str) or not value[key].strip() for key in keys
    ):
        raise ValueError("expected_behavior 必须包含 description/evidence_ref/quote")
    ref = value["evidence_ref"]
    source = evidence.get(ref, {})
    if (ref not in refs or interface_id not in source.get("interface_ids", [])
            or value["quote"] not in str(source.get("excerpt", ""))):
        raise ValueError("expected_behavior 缺少本接口源码原文依据")
    return dict(value)


def normalize_state_conditions(
    value: Any, evidence: dict[str, dict[str, Any]], interface_id: str,
    refs: list[str],
) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("state_conditions 必须是数组")
    result: list[dict[str, str]] = []
    for condition in value:
        keys = {"kind", "description", "mode", "evidence_ref", "quote"}
        if not isinstance(condition, dict) or set(condition) != keys:
            raise ValueError("state_conditions 条目必须包含 kind/description/mode/evidence_ref/quote")
        if any(not isinstance(condition[key], str) or not condition[key].strip() for key in keys):
            raise ValueError("state_conditions 字段必须是非空字符串")
        if condition["kind"] not in STATE_KINDS or condition["mode"] not in {"integration", "controlled"}:
            raise ValueError("state_conditions 类型或执行方式不合法")
        ref = condition["evidence_ref"]
        source = evidence.get(ref, {})
        if (ref not in refs or interface_id not in source.get("interface_ids", [])
                or condition["quote"] not in str(source.get("excerpt", ""))):
            raise ValueError("state_conditions 缺少本接口的源码原文依据")
        result.append(dict(condition))
    return result


def required_control_kinds(oracle: dict[str, Any]) -> set[str]:
    return {
        str(condition["kind"])
        for condition in oracle.get("state_conditions", [])
        if condition.get("mode") == "controlled" and condition.get("kind") in CONTROL_KINDS
    }


def execution_blockers(oracle: dict[str, Any]) -> list[str]:
    blockers = list(oracle.get("execution_blockers") or [])
    # A documented HTTP/structure check can run without pretending to verify
    # business behavior. Reachability, unknown HTTP mappings and state controls
    # remain execution blockers.
    if oracle.get("expected_status") != "unresolved" and oracle.get("assertion_scope") in {"response_structure", "status_only"}:
        expectation_gaps = {
            "仅检查响应字段存在，无法证明命中了目标业务分支",
            "没有可核验的业务响应断言，不能用 HTTP 状态代替业务预期",
            "响应字段或结果关系预期待补充",
        }
        blockers = [reason for reason in blockers if reason not in expectation_gaps]
    if oracle.get("expected_status") == "unresolved":
        blockers.append("本接口处理行为已有源码依据，HTTP 响应映射尚未确认")
    blockers.extend(
        str(condition["description"])
        for condition in oracle.get("state_conditions", [])
        if condition.get("kind") == "test_data" and condition.get("mode") == "controlled"
        and not oracle.get("fixture_plan")
    )
    return list(dict.fromkeys(blockers))
