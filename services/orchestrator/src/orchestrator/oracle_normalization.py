"""Lossless bookkeeping for model-produced test designs.

Only references whose original text is present in this interface are repaired.
No response value or business rule is inferred from the model's prose.
"""

from __future__ import annotations

import re
from typing import Any
from common.dto_validation import field_validation_annotation, parse_java_dto_fields
from common.java_source_citations import entry_assertion_issue, exact_java_source_quote, linked_exception_message_quotes
from common.java_response_paths import _close, _mask, _method
from common.response_assertions import response_assertion_scope
from orchestrator.oracle_paths import bind_controlled_return, bind_exception_exit


def required_body_binding(interface: dict[str, Any], texts: dict[str, str]) -> tuple[str, str, str] | None:
    for param in interface.get("params", []):
        if param.get("in") != "body" or not param.get("name"):
            continue
        name = str(param["name"])
        pattern = (r"@RequestBody(?:\s*\(([^)]*)\))?\s*"
                   r"(?:@\w+(?:\([^)]*\))?\s+)*[\w.<>]+\s+" + re.escape(name) + r"\b")
        for ref, source in texts.items():
            match = re.search(pattern, source)
            if match and not re.search(r"required\s*=\s*false", match.group(1) or ""):
                return name, ref, match.group(0)
    return None


def blank_request_guards(interface: dict[str, Any], texts: dict[str, str]) -> list[dict[str, str]]:
    constants = dict(re.findall(r'\bString\s+(\w+)\s*=\s*"([^"\n]+)"', "\n".join(texts.values())))
    result: list[dict[str, str]] = []
    for param in interface.get("params", []):
        if param.get("in") != "body" or not param.get("name"):
            continue
        name = str(param["name"])
        pattern = (r'if\s*\(\s*(?:' + re.escape(name) + r'\s*==\s*null\s*\|\|\s*)?'
                   r'StringUtils\.isBlank\s*\(\s*' + re.escape(name)
                   + r'\.getString\s*\(\s*("[^"\n]+"|[A-Z_][A-Z0-9_]*)\s*\)\s*\)\s*\)'
                   r'\s*\{?\s*(throw\s+new\s+[^;]+;)')
        for ref, source in texts.items():
            for match in re.finditer(pattern, source):
                raw_key = match.group(1)
                key = raw_key.strip('"') if raw_key.startswith('"') else constants.get(raw_key)
                if key:
                    result.append({"path": f"{name}.{key}", "evidence_ref": ref,
                                   "quote": match.group(0), "throw": match.group(2)})
    return result


def framework_body_design(interface: dict[str, Any], texts: dict[str, str]) -> dict[str, Any] | None:
    binding = required_body_binding(interface, texts)
    if binding is None:
        return None
    name, ref, quote = binding
    # A project handler can override Spring's default 400. Preserve the boundary
    # design even when that override has not yet been mapped to an HTTP contract.
    override = any(re.search(r"@ExceptionHandler\s*\([^)]*(?:Exception\.class|HttpMessageNotReadableException)", source)
                   for source in texts.values())
    return {
        "interface_id": interface["id"], "category": "boundary",
        "scenario": "请求体为空或为 JSON null 时，框架拒绝必填请求体绑定",
        "oracle_basis": "spring_required_body",
        "preconditions": [{"path": name, "op": "equals", "value": None}],
        "state_conditions": [], "expected_status": "unresolved" if override else "status=400",
        "assertions": [],
        "expected_behavior": {"description": "必填请求体缺失，在业务方法调用前拒绝请求；Spring 默认 HTTP 400，项目异常处理优先",
                              "evidence_ref": ref, "quote": quote},
        "evidence_refs": [ref],
        "evidence_quotes": [{"target": target, "evidence_ref": ref, "quote": quote}
                            for target in ("expected_status", "preconditions[0]")],
    }


def normalize_oracle_bookkeeping(raw: dict[str, Any], interface: dict[str, Any], texts: dict[str, str]) -> dict[str, Any]:
    result = dict(raw)
    for key in ("oracle_id", "control_kinds", "execution_readiness", "execution_blockers", "design_status", "assertion_scope", "fixture_plan", "observed_params"):
        result.pop(key, None)
    # Empty arrays describe absence of machine assertions; they are never proof
    # of business success. Readiness is decided separately after source binding.
    for key in ("assertions", "preconditions", "state_conditions", "evidence_quotes", "evidence_refs"):
        result.setdefault(key, [])
        if result[key] is None:
            result[key] = []
    # Empty model bookkeeping has no branch semantics, regardless of assertion scope.
    if not result.get("branch_id"):
        result.pop("branch_id", None)
        result.pop("branch_outcome", None)
    if result.get("oracle_basis") == "integration_entry":
        result["covered_claim_ids"] = []
    # The orchestration step owns this classification. A weak model should not
    # have to repeat a bookkeeping flag to retain a plain HTTP entry check.
    try:
        entry_scope = response_assertion_scope(result["assertions"])
    except ValueError:
        entry_scope = "invalid"
    states = result["state_conditions"]
    if (result.get("category") == "normal" and not result.get("branch_id")
            and (entry_scope in {"status_only", "response_structure"}
                 or (entry_scope == "business" and entry_assertion_issue(result["assertions"], texts) is None))
            and isinstance(states, list)
            and all(isinstance(c, dict) and c.get("mode") != "controlled" for c in states)):
        result["oracle_basis"] = "integration_entry"
        result["covered_claim_ids"] = []
        result.pop("branch_id", None)
        result.pop("branch_outcome", None)
    quotes = result["evidence_quotes"]
    refs = result["evidence_refs"]
    if not isinstance(quotes, list) or not isinstance(refs, list):
        return result
    quotes = [dict(quote) if isinstance(quote, dict) else quote for quote in quotes]
    refs = list(refs)
    conditions = result.get("preconditions")
    binding = required_body_binding(interface, texts)
    if binding and isinstance(conditions, list) and any(
        isinstance(c, dict) and c.get("path") == binding[0] and c.get("op") == "equals" and c.get("value") is None
        for c in conditions
    ):
        return framework_body_design(interface, texts) or result
    if isinstance(conditions, list) and result.get("category") == "normal":
        conditions = [dict(c) if isinstance(c, dict) else c for c in conditions]
        for guard in blank_request_guards(interface, texts):
            index = next((i for i, c in enumerate(conditions) if isinstance(c, dict) and c.get("path") == guard["path"]), len(conditions))
            if index == len(conditions):
                conditions.append({"path": guard["path"], "op": "not_blank"})
            elif conditions[index].get("op") in {"exists", "non_empty"}:
                conditions[index] = {"path": guard["path"], "op": "not_blank"}
            else:
                continue
            target = f"preconditions[{index}]"
            quotes = [q for q in quotes if not isinstance(q, dict) or q.get("target") != target]
            quotes.append({"target": target, "evidence_ref": guard["evidence_ref"], "quote": guard["quote"]})
        result["preconditions"] = conditions
    if isinstance(result.get("preconditions"), list):
        guards = blank_request_guards(interface, texts)
        for index, condition in enumerate(result["preconditions"]):
            if not isinstance(condition, dict):
                continue
            target = f"preconditions[{index}]"
            if any(isinstance(q, dict) and q.get("target") == target for q in quotes):
                continue
            guard = next((g for g in guards if g["path"] == condition.get("path")), {})
            if guard and condition.get("op") in {"equals", "exists", "non_empty", "not_blank"}:
                quotes.append({"target": target, "evidence_ref": guard["evidence_ref"], "quote": guard["quote"]})
                continue
            path = str(condition.get("path", ""))
            root = path.split(".", 1)[0]
            param = next((p for p in interface.get("params", []) if p.get("name") == root), None)
            if not param:
                continue
            if path == root and binding and binding[0] == root:
                quotes.append({"target": target, "evidence_ref": binding[1], "quote": binding[2]})
                continue
            if path == root:
                binding_pattern = (
                    r'@(?:RequestParam|PathVariable|RequestHeader)(?:\s*\([^)]*\))?\s*'
                    r'(?:@\w+(?:\([^)]*\))?\s+)*[\w.<>?\[\]]+\s+' + re.escape(root) + r'\b'
                )
                for request_ref, request_source in texts.items():
                    matched = re.search(binding_pattern, request_source)
                    if matched:
                        quotes.append({"target": target, "evidence_ref": request_ref, "quote": matched.group(0)})
                        break
                continue
            if "." not in path:
                continue
            leaf = re.findall(r"[A-Za-z_$][\w$]*", path)[-1]
            # Bind typed DTO fields to their actual owner, not a same-named field
            # in a response or another request class.
            declared_type = str(param.get("type", "")).rsplit(".", 1)[-1]
            if path == f"{root}.{leaf}":
                dto_owners = [(dto_ref, dto_source) for dto_ref, dto_source in texts.items()
                              if re.search(r"\bclass\s+" + re.escape(declared_type) + r"\b", _mask(dto_source))
                              and leaf in parse_java_dto_fields(dto_source)]
                distinct = {dto_source: dto_ref for dto_ref, dto_source in dto_owners}
                if len(distinct) == 1:
                    dto_source, dto_ref = next(iter(distinct.items()))
                    field = parse_java_dto_fields(dto_source)[leaf]
                    annotation = field_validation_annotation(field)
                    declaration = re.search(r"\b(?:private|protected|public)\s+[\w.<>?,\[\] ]+\s+" + re.escape(leaf) + r"\s*;", dto_source)
                    field_quote = annotation or (declaration.group(0) if declaration else None)
                    if field_quote:
                        quotes.append({"target": target, "evidence_ref": dto_ref, "quote": field_quote})
                        continue
            # Bind a missing citation to the complete owning request-reading
            # method, preserving its aliases, earlier guards and literal sets.
            # Ambiguous owners stay unresolved instead of guessing a snippet.
            candidates: dict[tuple[str, tuple[str, ...], str], tuple[str, str]] = {}
            read = re.compile(r'\.get(?:String|Integer|IntValue|Long|Boolean|JSONArray|JSONObject|BigDecimal|Double|Object)?\s*\(\s*"'+re.escape(leaf)+r'"\s*\)')
            for owner_ref, owner_source in texts.items():
                method = _method(owner_source)
                if not method or root not in method[1] or not read.search(owner_source):
                    continue
                end = _close(_mask(owner_source), method[2], '{', '}')
                if end is not None:
                    owner_key = (method[0], tuple(method[1]), owner_source[method[2]:end+1])
                    candidates.setdefault(owner_key, (owner_ref, owner_source))
            if len(candidates) == 1:
                request_ref, request_source = next(iter(candidates.values()))
                quotes.append({"target": target, "evidence_ref": request_ref, "quote": request_source})
    result["evidence_quotes"] = quotes
    result = bind_exception_exit(result, texts)
    result = bind_controlled_return(result, texts)
    for linked_quote in linked_exception_message_quotes(result, texts):
        if linked_quote not in quotes:
            quotes.append(linked_quote)
    citations: list[Any] = list(quotes)
    if isinstance(result.get("state_conditions"), list):
        result["state_conditions"] = [dict(c) if isinstance(c, dict) else c for c in result["state_conditions"]]
        citations.extend(result["state_conditions"])
    if isinstance(result.get("expected_behavior"), dict):
        result["expected_behavior"] = dict(result["expected_behavior"])
        citations.append(result["expected_behavior"])
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        ref, quote = citation.get("evidence_ref"), citation.get("quote")
        if isinstance(ref, str) and isinstance(quote, str):
            exact = exact_java_source_quote(quote, texts.get(ref, ""))
            if exact is not None:
                citation["quote"] = exact
                quote = exact
        if isinstance(ref, str) and isinstance(quote, str) and quote and quote in texts.get(ref, "") and ref not in refs:
            refs.append(ref)
    result["evidence_quotes"], result["evidence_refs"] = quotes, refs
    return result
