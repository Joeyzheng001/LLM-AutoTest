"""测试用例参数与前置条件的程序化校验。

用例参数来自 LLM，但“接口允许哪些参数、哪些必填、哪些默认值”来自
``interfaces.json``。本模块只使用后者做确定性检查，不猜业务数据；错误分支
如果明确预期 4xx，可以有意省略必填字段来验证服务端校验。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CaseParameterIssues:
    """单条用例的参数问题，全部是可展示的字段名/原因，不包含敏感值。"""

    missing_required: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    invalid_structured: tuple[str, ...] = ()
    params_not_object: bool = False

    def messages(self, *, allow_missing_for_error: bool = False) -> list[str]:
        """转换为稳定的前置失败文案。"""
        messages: list[str] = []
        if self.params_not_object:
            messages.append("params 必须是 JSON 对象")
        if self.unknown:
            messages.append(f"清单外参数：{', '.join(self.unknown)}")
        if self.invalid_structured:
            messages.append(
                "unknown body/query 参数必须是具体 JSON 对象："
                + ", ".join(self.invalid_structured)
            )
        if self.missing_required and not allow_missing_for_error:
            messages.append("缺少必填参数：" + ", ".join(self.missing_required))
        return messages


def expected_client_error(expected: str) -> bool:
    """判断用例是否明确期待客户端错误，允许故意缺少必填字段。"""
    text = expected.replace("；", ";").replace("：", ":")
    return bool(
        re.search(r"\bstatus\s*=\s*4\d{2}\b", text, re.I)
        or re.search(r"\bstatus_class\s*=\s*4xx\b", text, re.I)
        or re.search(r"\bHTTP\s*4xx\b", text, re.I)
        or re.fullmatch(r"\s*4\d{2}\s*", text)
    )


def materialize_declared_defaults(
    case: dict[str, Any], interface: dict[str, Any]
) -> dict[str, Any]:
    """仅填充接口定义中明确声明的默认值，不为业务字段猜值。

    错误分支若明确期待 4xx，保留缺参状态，确保能验证后端必填校验。
    """
    normalized = dict(case)
    raw_params = case.get("params")
    params = dict(raw_params) if isinstance(raw_params, dict) else {}
    if not expected_client_error(str(case.get("expected", ""))):
        for definition in interface.get("params") or []:
            if not isinstance(definition, dict):
                continue
            name = str(definition.get("name", ""))
            if not name or name in params or definition.get("required") is not True:
                continue
            if "default" in definition:
                params[name] = definition["default"]
    if params or isinstance(raw_params, dict) or "params" in case:
        normalized["params"] = params
    return normalized


def inspect_case_parameters(
    case: dict[str, Any], interface: dict[str, Any]
) -> CaseParameterIssues:
    """按接口定义检查单条用例的参数名、必填项和 unknown 容器。"""
    definitions = [
        item for item in interface.get("params", []) if isinstance(item, dict)
    ]
    raw_params = case.get("params")
    if raw_params is None:
        raw_params = {}
    if not isinstance(raw_params, dict):
        return CaseParameterIssues(params_not_object=True)
    params = {str(key): value for key, value in raw_params.items()}
    allowed = {str(item.get("name")) for item in definitions if item.get("name")}
    unknown = tuple(sorted(set(params) - allowed))
    missing = tuple(
        sorted(
            str(item["name"])
            for item in definitions
            if item.get("required") is True
            and item.get("name")
            and (str(item["name"]) not in params or params[str(item["name"])] is None)
        )
    )
    invalid_structured = tuple(
        sorted(
            str(item["name"])
            for item in definitions
            if item.get("name")
            and str(item.get("type")) == "unknown"
            and str(item.get("in", "")) in {"body", "query"}
            and str(item["name"]) in params
            and not isinstance(params[str(item["name"])], dict)
        )
    )
    return CaseParameterIssues(missing, unknown, invalid_structured)


def blocking_parameter_messages(
    case: dict[str, Any], interface: dict[str, Any]
) -> list[str]:
    """返回执行前应阻止请求的参数问题；负向 4xx 用例允许缺少必填项。"""
    issues = inspect_case_parameters(case, interface)
    return issues.messages(
        allow_missing_for_error=expected_client_error(str(case.get("expected", "")))
    )
