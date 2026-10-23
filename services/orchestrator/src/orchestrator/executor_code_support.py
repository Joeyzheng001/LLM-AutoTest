"""Resolve request codes supported by a controller's selected Java executor.

An enum parser only proves that a code can be parsed.  A selected executor may
handle a smaller set in its execute switch; normal scenarios must use that set.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def supported_executor_codes(
    root: Path, files: list[dict[str, Any]], interface: dict[str, Any]
) -> tuple[list[str], list[tuple[str, int, int, str]]] | None:
    """Return supported path codes and exact source spans, or None if ambiguous."""
    location = interface.get("code_location") or {}
    try:
        controller = (root / str(location["file"])).resolve()
        lines = controller.read_text(encoding="utf-8").splitlines()
        start, end = int(location["start_line"]), int(location["end_line"])
    except (OSError, KeyError, ValueError, UnicodeError):
        return None
    if root.resolve() not in controller.parents or start < 1 or end > len(lines) or start > end:
        return None
    method = "\n".join(lines[start - 1:end])
    selections = set(re.findall(
        r"EXECUTOR_SERVICE_CACHE\s*\.\s*get\s*\(\s*ExecutorTypeEnum\.(\w+)\s*\)", method
    ))
    if len(selections) != 1 or not re.search(r"\{code\}", str((interface.get("signature") or {}).get("path", ""))):
        return None
    executor = next(iter(selections))
    java_files = [str(item.get("path", "")) for item in files if isinstance(item, dict)
                  and str(item.get("path", "")).endswith(".java")]
    implementations: list[tuple[str, str]] = []
    enums: list[tuple[str, str]] = []
    for relative in java_files:
        source_path = (root / relative).resolve()
        if root.resolve() not in source_path.parents:
            continue
        try:
            source = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if Path(relative).name == "RequestTypeEnum.java":
            enums.append((relative, source))
        if (re.search(r"\bExecutorTypeEnum\s+getExecutorType\s*\(\s*\)", source)
                and re.search(r"\breturn\s+ExecutorTypeEnum\." + re.escape(executor) + r"\s*;", source)
                and re.search(r"\bexecute\s*\(\s*RequestTypeEnum\b", source)):
            implementations.append((relative, source))
    if len(implementations) != 1 or len(enums) != 1:
        return None
    impl_file, impl = implementations[0]
    enum_file, enum_source = enums[0]
    switch = re.search(r"\bswitch\s*\(\s*requestType\s*\)\s*\{(?P<body>.*?)\bdefault\s*:", impl, re.S)
    if switch is None:
        return None
    cases = set(re.findall(r"\bcase\s+(\w+)\s*:", switch.group("body")))
    enum_pairs = re.findall(r'\b([A-Z][A-Z0-9_]*)\s*\(\s*"([^"\n]+)"\s*\)', enum_source)
    values = [code for name, code in enum_pairs if name in cases]
    if not values or len(values) != len(cases):
        return None
    impl_start = impl.count("\n", 0, switch.start()) + 1
    impl_end = impl.count("\n", 0, switch.end()) + 1
    enum_match = re.search(r"\benum\s+RequestTypeEnum\s*\{(?P<body>.*?);", enum_source, re.S)
    if enum_match is None:
        return None
    enum_start = enum_source.count("\n", 0, enum_match.start()) + 1
    enum_end = enum_source.count("\n", 0, enum_match.end()) + 1
    selector = re.search(
        r"\bgetExecutorType\s*\(\s*\)\s*\{\s*return\s+ExecutorTypeEnum\."
        + re.escape(executor) + r"\s*;\s*\}", impl, re.S
    )
    if selector is None:
        return None
    selector_start = impl.count("\n", 0, selector.start()) + 1
    selector_end = impl.count("\n", 0, selector.end()) + 1
    return values, [
        (impl_file, selector_start, selector_end,
         "\n".join(impl.splitlines()[selector_start - 1:selector_end])),
        (impl_file, impl_start, impl_end, "\n".join(impl.splitlines()[impl_start - 1:impl_end])),
        (enum_file, enum_start, enum_end, "\n".join(enum_source.splitlines()[enum_start - 1:enum_end])),
    ]
