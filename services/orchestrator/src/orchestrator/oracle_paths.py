"""Source-backed controller exits, independent of model-authored branch names."""

from __future__ import annotations

import re
from typing import Any

from common.java_response_paths import _close, _mask, _method


def controller_exits(texts: dict[str, str]) -> list[dict[str, str]]:
    """Keep catch and final return exits alongside the existing if catalog."""
    exits: list[dict[str, str]] = []
    seen_sources: set[str] = set()
    for ref, source in texts.items():
        if source in seen_sources:
            continue
        if not re.search(r"@(?:Get|Post|Put|Patch|Delete|Request)Mapping\b", source):
            continue
        method = _method(source)
        if method is None:
            continue
        code = _mask(source)
        method_end = _close(code, method[2], "{", "}")
        if method_end is None:
            continue
        seen_sources.add(source)
        for index, catch in enumerate(re.finditer(r"\bcatch\s*\(", code), 1):
            opening = code.find("(", catch.start(), catch.end())
            closing = _close(code, opening, "(", ")")
            if closing is None:
                continue
            brace = closing + 1
            while brace < len(code) and code[brace].isspace():
                brace += 1
            end = _close(code, brace, "{", "}") if brace < len(code) and code[brace] == "{" else None
            if end is None:
                continue
            prefix = code[method[2]:catch.start()]
            external = bool(re.search(r"\b\w*(?:Service|Client)\w*\s*\.\s*\w+\s*\(", prefix))
            # Locally typed receivers may be named simply `service` or `client`.
            # Bind through their declaration, not the catch scenario's prose.
            receivers = re.findall(r"\b\w*(?:Service|Client)\w*\s+(\w+)\s*=", prefix)
            external = external or any(re.search(r"\b" + re.escape(receiver) + r"\s*\.\s*\w+\s*\(", prefix)
                                       for receiver in receivers)
            exits.append({
                "branch_id": f"{ref}:catch:{index}", "node_kind": "catch",
                "evidence_ref": ref, "quote": source[catch.start():closing + 1],
                "body_quote": source[brace:end + 1],
                "control_kind": "downstream_response" if external else "unresolved",
                "required_controls": "downstream_response" if external else "",
            })
        for index, returned in enumerate(re.finditer(r"\breturn\b[^;{}]*;", code), 1):
            if not method[2] < returned.start() < method_end:
                continue
            prefix = code[method[2]:returned.start()]
            # Nested returns remain represented by their own if/catch paths.
            if prefix.count("{") - prefix.count("}") != 1:
                continue
            exits.append({
                "branch_id": f"{ref}:return:{index}", "node_kind": "return",
                "evidence_ref": ref, "quote": source[returned.start():returned.end()],
                "body_quote": source[method[2]:method_end + 1],
                "control_kind": "request", "required_controls": "",
            })
    return exits


def bind_exception_exit(raw: dict[str, Any], texts: dict[str, str]) -> dict[str, Any]:
    """Bind a missing catch ID only through a cited catch body, never a title."""
    if raw.get("category") != "exception" or raw.get("branch_id"):
        return raw
    quotes = raw.get("evidence_quotes")
    if not isinstance(quotes, list):
        return raw
    matches: dict[str, dict[str, str]] = {}
    for exit in controller_exits(texts):
        if exit["node_kind"] != "catch":
            continue
        for quote in quotes:
            if not isinstance(quote, dict) or texts.get(str(quote.get("evidence_ref"))) != texts[exit["evidence_ref"]]:
                continue
            text = quote.get("quote")
            if isinstance(text, str) and text.strip() and (
                text in exit["body_quote"] or exit["quote"] in text
            ):
                matches[exit["branch_id"]] = exit
    if len(matches) == 1:
        selected = next(iter(matches.values()))
        result = {**raw, "branch_id": selected["branch_id"], "branch_outcome": True}
        states = list(raw.get("state_conditions") or [])
        if selected["control_kind"] == "downstream_response" and not any(
            isinstance(state, dict) and state.get("kind") == "downstream_response" for state in states
        ):
            states.append({
                "kind": "downstream_response", "mode": "controlled",
                "description": "目标控制器调用的服务抛出异常并进入已定位的 catch 出口",
                "evidence_ref": selected["evidence_ref"], "quote": selected["quote"],
            })
        result["state_conditions"] = states
        return result
    return raw


def bind_controlled_return(raw: dict[str, Any], texts: dict[str, str]) -> dict[str, Any]:
    """A cited return path may retain an explicitly controlled data scenario."""
    if raw.get("category") != "normal" or raw.get("branch_id") or raw.get("oracle_basis") == "integration_entry":
        return raw
    if not any(isinstance(state, dict) and state.get("mode") == "controlled" for state in raw.get("state_conditions") or []):
        return raw
    matches: dict[str, dict[str, str]] = {}
    for exit in controller_exits(texts):
        if exit["node_kind"] != "return":
            continue
        for quote in raw.get("evidence_quotes") or []:
            if (not isinstance(quote, dict) or not str(quote.get("target", "")).startswith("assertions[")
                    or texts.get(str(quote.get("evidence_ref"))) != texts[exit["evidence_ref"]]):
                continue
            value = quote.get("quote")
            if isinstance(value, str) and value.strip() and (value in exit["body_quote"] or exit["quote"] in value):
                matches[exit["branch_id"]] = exit
    if len(matches) == 1:
        return {**raw, "branch_id": next(iter(matches)), "branch_outcome": True, "covered_claim_ids": []}
    return raw
