"""Final request binding counts, separate from execution and requirement coverage."""

from __future__ import annotations

from typing import Any


def oracle_request_bindings(
    oracles: list[dict[str, Any]], cases: list[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """Count validated request designs; blocked environment conditions do not erase them.

    An explicitly retained placeholder or unloadable script is not a usable
    request. This does not validate new parameters or infer business coverage.
    """
    declared = {str(o["oracle_id"]) for o in oracles if o.get("oracle_id")}
    bound = {
        str(case["oracle_id"])
        for case in cases
        if case.get("oracle_id") in declared
        and case.get("oracle_status") == "verified"
        and case.get("loadable") is not False
        and isinstance(case.get("params"), dict)
        and not any("有效 HTTP 请求尚未补齐" in str(reason)
                    for reason in case.get("execution_blockers") or [])
    }
    return bound, declared - bound
