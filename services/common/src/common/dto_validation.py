"""Small, fail-closed helpers for binding Java Bean Validation to DTO fields."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_JAVA_FIELD = re.compile(
    r"^(?:(?:public|protected|private)\s+)?"
    r"(?:(?:static|final|transient|volatile)\s+)*"
    r"(?P<type>[^;=]+?)\s+(?P<name>[A-Za-z_$][\w$]*)\s*"
    r"(?:=[^;]*)?;\s*$"
)
_JAVA_ANNOTATION_NAME = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
_VALIDATION_KIND = re.compile(r"@(?:[\w$]+\.)*(NotNull|NotBlank|NotEmpty)\b")
_SCENARIO_DTO_VALIDATION = re.compile(
    r"DTO|参数校验|字段校验|@Not(?:Null|Blank|Empty)\b", re.I
)
_SCENARIO_DTO_VALIDATION_FAILURE = re.compile(
    r"失败|未通过|不通过|错误|拦截|不满足|不符合|违反|为空|不能为空|缺失|无效|非法|拒绝|invalid|reject|empty",
    re.I,
)


def scenario_claims_dto_validation_failure(scenario: Any) -> bool:
    """Return true only when the scenario explicitly says DTO validation fails.

    A broad keyword check used to treat unrelated phrases such as “DTO 校验通过，
    Service 执行无异常” as validation failures. Keep the failure signal close to
    the validation claim and recognize common Chinese word orders.
    """
    text = str(scenario or "")
    if not _SCENARIO_DTO_VALIDATION.search(text):
        return False
    # Explicit success language overrides failure words elsewhere in the scenario.
    if re.search(
        r"(?:通过|成功|正常).{0,12}(?:DTO.{0,6}(?:校验|验证)|(?:参数|字段)(?:校验|验证))"
        r"|(?:DTO|参数|字段).{0,8}(?:校验|验证).{0,8}(?:通过|成功|正常)",
        text,
        re.I,
    ):
        return False
    # Failure must be attached to validation, a constrained value, or an annotation;
    # generic Service exceptions/failures do not describe a DTO validation failure.
    return bool(
        re.search(
            r"(?:DTO|参数|字段).{0,16}(?:校验|验证).{0,10}"
            r"(?:失败|未通过|不通过|错误|异常|拦截|拒绝|无效|非法)",
            text,
            re.I,
        )
        or re.search(
            r"(?:不满足|不符合|违反|为空|不能为空|缺失|无效|非法).{0,18}"
            r"(?:DTO|校验|验证|@Not(?:Null|Blank|Empty))",
            text,
            re.I,
        )
        or re.search(
            r"@Not(?:Null|Blank|Empty)\b.{0,24}(?:不满足|违反|失败|不通过)",
            text,
            re.I,
        )
        or re.search(
            r"(?:DTO.{0,6}字段|参数字段|字段).{0,12}"
            r"(?:为空|缺失|无效|非法|违反|不满足)",
            text,
            re.I,
        )
    )


def _paren_delta(text: str) -> int:
    """Count annotation parentheses without counting characters inside literals."""
    without_literals = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', "", text)
    return without_literals.count("(") - without_literals.count(")")


def _matching_parenthesis(text: str, opening: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _leading_annotations(line: str) -> tuple[list[str], str, bool]:
    """Split complete annotations at the beginning of a Java source line."""
    annotations: list[str] = []
    cursor = 0
    while cursor < len(line):
        while cursor < len(line) and line[cursor].isspace():
            cursor += 1
        if cursor >= len(line) or line[cursor] != "@":
            break
        start = cursor
        cursor += 1
        match = _JAVA_ANNOTATION_NAME.match(line, cursor)
        if match is None:
            return annotations, line[start:].strip(), False
        cursor = match.end()
        while cursor < len(line) and line[cursor].isspace():
            cursor += 1
        if cursor < len(line) and line[cursor] == "(":
            closing = _matching_parenthesis(line, cursor)
            if closing is None:
                return annotations + [line[start:].strip()], "", True
            cursor = closing + 1
        annotations.append(line[start:cursor].strip())
    return annotations, line[cursor:].strip(), False


def parse_java_dto_fields(source: str) -> dict[str, dict[str, Any]]:
    """Return Java DTO fields with only annotations directly attached to each field.

    Annotation state is cleared at every non-annotation member. This prevents an
    annotation above one field from leaking onto a neighboring field merely because
    they happen to be within a fixed number of source lines.
    """
    fields: dict[str, dict[str, Any]] = {}
    pending_annotations: list[str] = []
    annotation_depth = 0
    in_block_comment = False

    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if in_block_comment:
            if "*/" in line:
                in_block_comment = False
            continue
        if line.startswith("/*"):
            if "*/" not in line[2:]:
                in_block_comment = True
            continue
        if line.startswith("//") or line.startswith("*") or line.startswith("*/"):
            continue

        if annotation_depth:
            pending_annotations.append(line)
            annotation_depth += _paren_delta(line)
            if annotation_depth <= 0:
                annotation_depth = 0
            continue

        if line.startswith("@"):
            annotations, remainder, incomplete = _leading_annotations(line)
            if incomplete:
                pending_annotations.extend(annotations)
                annotation_depth = max(0, _paren_delta(line))
                continue
            pending_annotations.extend(annotations)
            if not remainder:
                continue
            field_match = _JAVA_FIELD.fullmatch(remainder)
            if field_match is None:
                pending_annotations.clear()
                continue
        else:
            field_match = _JAVA_FIELD.fullmatch(line)
            if field_match is None:
                pending_annotations.clear()
                continue

        field_type = field_match.group("type").strip()
        field_name = field_match.group("name")
        fields[field_name] = {
            "name": field_name,
            "type": field_type,
            "annotations": list(pending_annotations),
        }
        pending_annotations.clear()

    return fields


def validation_kind(annotation: Any) -> str | None:
    """Return the supported Bean Validation annotation name, if present."""
    match = _VALIDATION_KIND.search(str(annotation or ""))
    return match.group(1) if match else None


def field_validation_annotation(field: dict[str, Any] | None) -> str | None:
    if not isinstance(field, dict):
        return None
    for annotation in field.get("annotations") or []:
        if validation_kind(annotation):
            return str(annotation)
    return None


def invalid_value_for_validation(field_type: str, annotation: Any) -> tuple[bool, Any]:
    """Return a deterministic invalid value for a supported DTO constraint."""
    kind = validation_kind(annotation)
    normalized_type = re.sub(r"\s+", "", str(field_type or ""))
    raw_type = normalized_type.split("<", 1)[0].rsplit(".", 1)[-1]
    is_string = raw_type == "String"
    is_container = bool(
        re.search(r"(?:List|Set|Collection|Map|Iterable)<", normalized_type)
        or normalized_type.endswith("[]")
    )
    if kind == "NotNull":
        return True, None
    if kind == "NotBlank" and is_string:
        return True, ""
    if kind == "NotEmpty":
        if is_string:
            return True, ""
        if is_container:
            return True, []
    return False, None


def value_violates_validation(field_type: str, annotation: Any, value: Any) -> bool:
    """Check whether a supplied scalar/container violates a supported constraint."""
    kind = validation_kind(annotation)
    normalized_type = re.sub(r"\s+", "", str(field_type or ""))
    raw_type = normalized_type.split("<", 1)[0].rsplit(".", 1)[-1]
    is_string = raw_type == "String"
    is_container = bool(
        re.search(r"(?:List|Set|Collection|Map|Iterable)<", normalized_type)
        or normalized_type.endswith("[]")
    )
    if kind == "NotNull":
        return value is None
    if kind == "NotBlank" and is_string:
        return value is None or (isinstance(value, str) and not value.strip())
    if kind == "NotEmpty":
        if value is None:
            return True
        if is_string:
            return isinstance(value, str) and not value
        if is_container:
            return isinstance(value, (list, tuple, set, dict)) and len(value) == 0
    return False


def dto_field_contracts(
    interface: dict[str, Any], evidence_items: list[dict[str, Any]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """Bind source DTO annotations to the matching request body parameter fields."""
    dto_evidence = [
        item for item in evidence_items
        if isinstance(item, dict) and item.get("role") == "dto_validation"
    ]
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for param in interface.get("params") or []:
        if not isinstance(param, dict) or param.get("in") != "body":
            continue
        param_name = str(param.get("name", ""))
        type_name = str(param.get("type", "")).split("<", 1)[0].rsplit(".", 1)[-1].strip()
        candidates = [
            item for item in dto_evidence
            if Path(str(item.get("file", ""))).stem == type_name
        ] if type_name else []
        if not candidates and not type_name and len(dto_evidence) == 1:
            candidates = dto_evidence
        if len(candidates) != 1:
            continue

        source = candidates[0]
        source_fields = parse_java_dto_fields(str(source.get("excerpt", "")))
        declared_fields = {
            str(field.get("name")): field
            for field in param.get("fields") or []
            if isinstance(field, dict) and field.get("name")
        }
        for field_name, source_field in source_fields.items():
            metadata = declared_fields.get(field_name)
            if declared_fields and metadata is None:
                continue
            annotation = field_validation_annotation(source_field)
            source_kind = validation_kind(annotation)
            metadata_kind = validation_kind((metadata or {}).get("required_evidence"))
            conflict = bool(metadata_kind and metadata_kind != source_kind)
            result[(param_name, field_name)] = {
                **source_field,
                "param_name": param_name,
                "source_evidence_id": str(source.get("evidence_id", "")),
                "validation_annotation": annotation,
                "validation_kind": source_kind,
                "validation_conflict": conflict,
                "metadata": metadata or {},
            }
    return result


def dto_validation_oracle_issue(
    oracle: dict[str, Any],
    interface: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
) -> str | None:
    """Reject DTO-validation oracles whose condition/quote is not bound to that field."""
    scenario = str(oracle.get("scenario", ""))
    scenario_claims_validation = scenario_claims_dto_validation_failure(scenario)
    contracts = dto_field_contracts(interface, list(evidence_by_id.values()))
    # A hand-written JSONObject validator is not Bean Validation merely because
    # the scenario title contains “参数校验失败”. Bind this check to real annotations.
    scenario_claims_validation = scenario_claims_validation and any(
        contract.get("validation_kind") for contract in contracts.values()
    )
    validated_conditions = 0

    for index, condition in enumerate(oracle.get("preconditions") or []):
        if not isinstance(condition, dict):
            continue
        target = f"preconditions[{index}]"
        quotes = [
            quote for quote in oracle.get("evidence_quotes") or []
            if isinstance(quote, dict) and str(quote.get("target", "")) == target
        ]
        dto_quotes: list[tuple[dict[str, Any], str]] = []
        for quote in quotes:
            evidence = evidence_by_id.get(str(quote.get("evidence_ref", "")), {})
            quoted_text = str(quote.get("quote", ""))
            if evidence.get("role") == "dto_validation" and validation_kind(quoted_text):
                dto_quotes.append((evidence, quoted_text))
        if not dto_quotes:
            continue

        path = str(condition.get("path", ""))
        if path.count(".") != 1:
            return "DTO 校验前置条件必须精确指向请求体字段"
        param_name, field_name = path.split(".", 1)
        contract = contracts.get((param_name, field_name))
        if contract is None:
            return f"请求 DTO 源码中找不到字段 {path}"
        if contract.get("validation_conflict"):
            return f"接口字段元数据与 DTO 源码注解不一致：{path}"
        annotation = contract.get("validation_annotation")
        if not annotation:
            return f"DTO 字段 {path} 没有对应的源码校验注解"
        quote_matches_field = any(
            validation_kind(quoted_text) == contract.get("validation_kind")
            and quoted_text in str(annotation)
            for _, quoted_text in dto_quotes
        )
        if not quote_matches_field:
            return f"引用的校验注解不属于 DTO 字段 {path}"
        if not value_violates_validation(
            str(contract.get("type", "")), annotation, condition.get("value")
        ):
            if scenario_claims_validation:
                return f"DTO 字段 {path} 的前置值没有违反源码校验注解"
            # Bean Validation can also document a valid value in an ordinary
            # success scenario. That does not make the scenario a validation error.
            continue
        validated_conditions += 1

    if scenario_claims_validation and validated_conditions == 0:
        return "DTO 校验场景没有可验证的请求字段前置条件"
    return None


def dto_precondition_reachability_issue(
    oracle: dict[str, Any],
    interface: dict[str, Any],
    evidence_items: list[dict[str, Any]],
) -> str | None:
    """Reject a later business branch when its request is intercepted by DTO validation.

    The check intentionally uses source-bound DTO field annotations and the request
    values, not the model's evidence citations. A service oracle cannot evade an
    earlier ``@NotEmpty`` by citing only the service's own ``if (empty)`` branch.
    A correctly evidenced DTO-validation oracle remains executable.
    """
    scenario_claims_validation = scenario_claims_dto_validation_failure(
        oracle.get("scenario", "")
    )
    if scenario_claims_validation and dto_validation_oracle_issue(
        oracle,
        interface,
        {
            str(item.get("evidence_id", "")): item
            for item in evidence_items
            if isinstance(item, dict) and item.get("evidence_id")
        },
    ) is None:
        # The validator above requires a DTO annotation quote bound to the same
        # request field and a value that actually violates it.
        return None

    body_params = {
        str(param.get("name", ""))
        for param in interface.get("params") or []
        if isinstance(param, dict) and param.get("in") == "body" and param.get("name")
    }
    validated_body_params = {
        str(param.get("name", ""))
        for param in interface.get("parameter_evidence") or []
        if isinstance(param, dict)
        and param.get("in") == "body"
        and param.get("name")
        and any(
            re.search(r"@?(?:Valid|Validated)\b", str(annotation))
            for annotation in param.get("annotations") or []
        )
    }
    # Legacy interface snapshots may not have parameter_evidence. For a single
    # body parameter, the exact controller signature is still unambiguous.
    if not validated_body_params and len(body_params) == 1:
        controller_validates = any(
            isinstance(item, dict)
            and item.get("role") == "controller"
            and re.search(r"@(?:Valid|Validated)\b", str(item.get("excerpt", "")))
            for item in evidence_items
        )
        if controller_validates:
            validated_body_params = body_params

    contracts = dto_field_contracts(interface, evidence_items)
    for condition in oracle.get("preconditions") or []:
        if not isinstance(condition, dict) or condition.get("op") != "equals":
            continue
        path = str(condition.get("path", ""))
        if path.count(".") != 1:
            continue
        param_name, field_name = path.split(".", 1)
        if param_name not in validated_body_params:
            continue
        contract = contracts.get((param_name, field_name))
        if contract is None:
            continue
        annotation = contract.get("validation_annotation")
        if not annotation:
            continue
        if value_violates_validation(
            str(contract.get("type", "")), annotation, condition.get("value")
        ):
            return (
                f"请求前置条件 {path}={condition.get('value')!r} 违反源码 DTO 字段注解 "
                f"{annotation}；请求会先被参数校验拦截，后续 Controller/Service 分支不可达"
            )
    return None
