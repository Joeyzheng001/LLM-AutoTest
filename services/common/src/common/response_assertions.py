"""受限 JSON 响应断言语法，供用例生成校验和执行节点共用。"""

from __future__ import annotations

import math
import re
from typing import Any

_PATH_RE = re.compile(
    # 首段：标识符或纯下标；后续段：``.标识符`` / ``.下标``，或**不点直接跟下标**
    # （``a[0]``、``a.b[0]``）。最后一条不可省：数组下标通常紧跟标识符书写，
    # 漏掉它会让标准写法 ``data.items[0].id`` 被判为非法路径——而 _resolve_json_path
    # 明明能正确解析它，校验与解析口径不一致会把合法断言误报成 X-LLM-02 降级。
    r"^(?:[A-Za-z_][A-Za-z0-9_-]*|\[\d+\])"
    r"(?:\.(?:[A-Za-z_][A-Za-z0-9_-]*|\[\d+\])|\[\d+\])*$"
)
_OPERATORS = {
    "equals",
    "not_equals",
    "exists",
    "not_exists",
    "null_or_missing",
    "non_empty",
    "contains",
    "greater_than",
    "less_than",
    "not_blank",
    "equals_request",
    "in",
    "not_in",
}
_VALUE_OPERATORS = {"equals", "not_equals", "contains", "greater_than", "less_than", "equals_request", "in", "not_in"}


def normalize_response_assertions(value: Any, *, label: str = "assertions") -> list[dict[str, Any]]:
    """Validate and normalize explicit response assertions; malformed input is rejected."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} 必须是数组")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"{label}[{index}] 必须是对象")
        path = str(raw.get("path", "")).strip()
        if path.startswith("$."):
            path = path[2:]
        elif path.startswith("$"):
            path = path[1:]
        if not path or not _PATH_RE.fullmatch(path):
            raise ValueError(
                f"{label}[{index}].path 不是受支持的 JSON 路径：{path[:120]!r}"
            )
        operator = str(raw.get("op", "")).strip()
        if operator not in _OPERATORS:
            raise ValueError(f"{label}[{index}].op 不支持：{operator}")
        if operator in _VALUE_OPERATORS and "value" not in raw:
            raise ValueError(f"{label}[{index}] 的 {operator} 断言缺少 value")
        if operator in {"in", "not_in"} and (
            not isinstance(raw.get("value"), list) or not raw["value"]
        ):
            raise ValueError(f"{label}[{index}].value 必须是非空候选值数组")
        if operator == "equals_request" and (
            not isinstance(raw["value"], str) or not _PATH_RE.fullmatch(raw["value"])
        ):
            raise ValueError("equals_request.value 必须是请求参数 JSON 路径")
        entry: dict[str, Any] = {"path": path, "op": operator}
        if "value" in raw:
            entry["value"] = raw["value"]
        normalized.append(entry)
    return normalized


def evaluate_response_assertions(
    body: Any, assertions: list[dict[str, Any]], request_params: Any = None,
) -> list[dict[str, Any]]:
    """Evaluate a validated assertion list and return redaction-ready outcomes."""
    outcomes: list[dict[str, Any]] = []
    for assertion in assertions:
        path = str(assertion["path"])
        operator = str(assertion["op"])
        found, actual = _resolve_json_path(body, path)
        expected = assertion.get("value")
        passed = False
        if operator == "exists":
            passed = found
        elif operator == "not_exists":
            passed = not found
        elif operator == "null_or_missing":
            passed = not found or actual is None
        elif operator == "non_empty":
            passed = found and actual is not None and actual != "" and actual != [] and actual != {}
        elif operator == "not_blank":
            passed = found and isinstance(actual, str) and bool(actual.strip())
        elif operator == "equals_request":
            input_found, expected = _resolve_json_path(request_params, str(expected))
            passed = found and input_found and _values_equal(actual, expected)
        elif operator == "equals":
            passed = found and _values_equal(actual, expected)
        elif operator == "not_equals":
            passed = found and not _values_equal(actual, expected)
        elif operator in {"in", "not_in"}:
            if isinstance(expected, list) and expected:
                member = any(_values_equal(actual, item) for item in expected)
                passed = found and (member if operator == "in" else not member)
        elif operator == "contains":
            passed = found and _contains(actual, expected)
        elif operator in {"greater_than", "less_than"}:
            try:
                left = float(actual)
                right = float(expected) if expected is not None else float("nan")
                passed = math.isfinite(left) and math.isfinite(right) and (
                    left > right if operator == "greater_than" else left < right
                )
            except (TypeError, ValueError):
                passed = False
        outcomes.append(
            {
                "path": path,
                "op": operator,
                **({"expected": expected} if operator in _VALUE_OPERATORS else {}),
                "actual": actual if found else None,
                "found": found,
                "passed": passed,
            }
        )
    return outcomes


def response_assertion_scope(assertions: Any) -> str:
    """Metadata echoes and existence checks cannot establish a business outcome."""
    normalized = normalize_response_assertions(assertions)
    if not normalized:
        return "status_only"
    metadata = {"serialno", "requestid", "traceid", "correlationid"}
    if all(item["op"] in {"exists", "null_or_missing"} or str(item["path"]).split(".")[0].replace("_", "").lower() in metadata
           for item in normalized):
        return "response_structure"
    return "business"


def legacy_response_assertions(expected: str) -> list[dict[str, Any]]:
    """Translate the legacy ``field=value`` syntax into the explicit assertion form."""
    assertions: list[dict[str, Any]] = []
    for token in expected.replace("；", ";").replace("，", ",").split(";"):
        if "=" not in token:
            continue
        path, _, raw_value = token.partition("=")
        path = path.strip()
        raw_value = raw_value.strip()
        if path in {"status", "status_class"} or not _PATH_RE.fullmatch(path):
            continue
        if raw_value in {"非空", "non-empty"}:
            assertions.append({"path": path, "op": "non_empty"})
            continue
        if raw_value in {"存在", "exists"}:
            assertions.append({"path": path, "op": "exists"})
            continue
        if raw_value in {"不存在", "not-exists"}:
            assertions.append({"path": path, "op": "not_exists"})
            continue
        normalized_value: Any = raw_value
        if raw_value in {"true", "false"}:
            normalized_value = raw_value == "true"
        elif raw_value == "null":
            normalized_value = None
        else:
            try:
                normalized_value = float(raw_value) if "." in raw_value else int(raw_value)
            except ValueError:
                pass
        assertions.append({"path": path, "op": "equals", "value": normalized_value})
    return assertions


def _resolve_json_path(body: Any, path: str) -> tuple[bool, Any]:
    current = body
    for segment in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if segment.startswith("[") and segment.endswith("]"):
            index = int(segment[1:-1])
            if not isinstance(current, list) or index >= len(current):
                return False, None
            current = current[index]
        else:
            if not isinstance(current, dict) or segment not in current:
                return False, None
            current = current[segment]
    return True, current


def _values_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return float(actual) == float(expected)
    return bool(actual == expected)


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(actual, (list, tuple, str)):
        return expected in actual if not isinstance(actual, str) else str(expected) in actual
    if isinstance(actual, dict):
        return expected in actual
    return False
