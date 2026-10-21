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
    invalid_field_types: tuple[str, ...] = ()
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
        if self.invalid_field_types:
            messages.append(
                "请求体字段 JSON 类型与接口源码类型不符："
                + ", ".join(self.invalid_field_types)
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


# 由容器/MVC 框架在运行时注入的参数（Servlet 请求响应对象、Spring MVC 绑定对象等）。
# 业务用例既不需要、也不应该提供它们的值：
# - 当作必填会逼模型编造占位值；
# - 兜底/默认值一旦填进去，会被当作真实请求参数发出去——实测 `response: "unknown"`
#   最终进了 query（`in=unknown` 在 auto_execute 里落进 else 分支）。
# 按类型**简单名**匹配，容忍包名前缀（javax.servlet.http.HttpServletResponse）。
_RUNTIME_INJECTED_TYPES = frozenset(
    {
        "HttpServletRequest",
        "HttpServletResponse",
        "ServletRequest",
        "ServletResponse",
        "HttpSession",
        "ServletContext",
        "MultipartHttpServletRequest",
        "OutputStream",
        "ServletOutputStream",
        "PrintWriter",
        "Model",
        "ModelAndView",
        "BindingResult",
        "Errors",
        "RedirectAttributes",
        "UriComponentsBuilder",
        "WebRequest",
        "NativeWebRequest",
        "Principal",
        "Authentication",
    }
)


def is_runtime_injected_param(definition: dict[str, Any]) -> bool:
    """参数是否由框架运行时注入（容器对象），业务用例不提供其值。"""
    raw_type = str(definition.get("type", "")).strip()
    if not raw_type:
        return False
    return raw_type.rsplit(".", 1)[-1] in _RUNTIME_INJECTED_TYPES


def materialize_declared_defaults(
    case: dict[str, Any], interface: dict[str, Any]
) -> dict[str, Any]:
    """仅填充接口定义中明确声明的默认值，不为业务字段猜值。

    错误分支若明确期待 4xx，保留缺参状态，确保能验证后端必填校验。
    框架运行时注入的容器参数（HttpServletResponse 等）一律不填：它们没有业务值，
    填出来的占位串会被 auto_execute 当作真实请求参数发出去。
    """
    normalized = dict(case)
    raw_params = case.get("params")
    params = dict(raw_params) if isinstance(raw_params, dict) else {}
    intentional_bodies = _intentional_missing_bodies(
        case, [item for item in interface.get("params") or [] if isinstance(item, dict)], params,
    )
    if not expected_client_error(str(case.get("expected", ""))):
        for definition in interface.get("params") or []:
            if not isinstance(definition, dict):
                continue
            if is_runtime_injected_param(definition):
                continue
            name = str(definition.get("name", ""))
            if name in intentional_bodies:
                continue
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
    intentional_bodies = _intentional_missing_bodies(case, definitions, params)
    allowed = {str(item.get("name")) for item in definitions if item.get("name")}
    unknown = tuple(sorted(set(params) - allowed))
    missing = tuple(
        sorted(
            str(item["name"])
            for item in definitions
            if item.get("required") is True
            and item.get("name")
            # 框架运行时注入的容器参数不算缺失：用例无从提供、也不需要提供。
            and not is_runtime_injected_param(item)
            and str(item["name"]) not in intentional_bodies
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
            and str(item["name"]) not in intentional_bodies
            and not isinstance(params[str(item["name"])], dict)
        )
    )
    invalid_field_types: set[str] = set()
    for definition in definitions:
        if definition.get("in") != "body" or not definition.get("name"):
            continue
        param_name = str(definition["name"])
        body = params.get(param_name)
        declared_fields = definition.get("fields") or []
        if body is None:
            continue
        if not isinstance(body, dict):
            if declared_fields:
                invalid_field_types.add(f"{param_name} (JSON object)")
            continue
        for field in declared_fields:
            if not isinstance(field, dict) or not field.get("name"):
                continue
            field_name = str(field["name"])
            if field_name not in body or body[field_name] is None:
                # JSON null can be an intentional @NotNull/@NotEmpty negative case.
                continue
            if not _json_value_matches_java_type(body[field_name], str(field.get("type", ""))):
                invalid_field_types.add(
                    f"{param_name}.{field_name} ({field.get('type', 'unknown')})"
                )
    return CaseParameterIssues(
        missing, unknown, invalid_structured, tuple(sorted(invalid_field_types))
    )


def _intentional_missing_bodies(
    case: dict[str, Any], definitions: list[dict[str, Any]], params: dict[str, Any],
) -> set[str]:
    """Only a source-bound required-body oracle authorizes this negative input."""
    if (case.get("oracle_status") != "verified" or case.get("oracle_basis") != "spring_required_body"
            or case.get("category") != "boundary" or not case.get("oracle_id")):
        return set()
    return {str(item["name"]) for item in definitions
            if item.get("in") == "body" and item.get("required") is True and item.get("name")
            and params.get(str(item["name"])) is None
            and any(isinstance(condition, dict) and condition.get("path") == item["name"]
                    and condition.get("op") == "equals" and condition.get("value") is None
                    for condition in case.get("request_conditions") or [])}


def _json_value_matches_java_type(value: Any, field_type: str) -> bool:
    """Check only unambiguous JSON shapes; unknown/custom Java types stay unchecked."""
    normalized = re.sub(r"\s+", "", field_type)
    raw = normalized.split("<", 1)[0].rsplit(".", 1)[-1]
    if raw in {"List", "Set", "Collection", "Iterable"} or normalized.endswith("[]"):
        return isinstance(value, list)
    if raw == "Map":
        return isinstance(value, dict)
    if raw in {"String", "Character", "char"}:
        return isinstance(value, str)
    if raw in {"Boolean", "boolean"}:
        return isinstance(value, bool)
    if raw in {
        "Byte", "byte", "Short", "short", "Integer", "int", "Long", "long",
        "Float", "float", "Double", "double", "BigInteger", "BigDecimal",
    }:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def blocking_parameter_messages(
    case: dict[str, Any], interface: dict[str, Any]
) -> list[str]:
    """返回执行前应阻止请求的参数问题；负向 4xx 用例允许缺少必填项。"""
    issues = inspect_case_parameters(case, interface)
    return issues.messages(
        allow_missing_for_error=expected_client_error(str(case.get("expected", "")))
    )
