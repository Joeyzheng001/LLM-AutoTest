"""Shared, versioned E2E contracts. Business expectations never change at runtime."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

ACTIONS = {
    "goto",
    "click",
    "dblclick",
    "fill",
    "press",
    "check",
    "uncheck",
    "select",
    "hover",
    "scroll",
    "drag",
    "upload",
    "download",
    "open_popup",
    "open_actor",
    "close_page",
    "capture_text",
    "capture_value",
    "screenshot",
    "fixture",
}
ASSERTIONS = {
    "expect_text",
    "expect_exact_text",
    "expect_url",
    "expect_visible",
    "expect_hidden",
    "expect_value",
    "expect_count",
    "expect_checked",
    "expect_unchecked",
    "expect_enabled",
    "expect_disabled",
    "expect_options",
    "expect_response",
}
SCENARIOS = {"normal", "boundary", "exception", "permission", "state_transition", "consistency"}
TARGET_ACTIONS = (ACTIONS | ASSERTIONS) - {
    "goto",
    "expect_url",
    "expect_response",
    "screenshot",
    "close_page",
    "open_actor",
    "fixture",
}
VALUE_ACTIONS = {
    "goto",
    "fill",
    "press",
    "select",
    "expect_text",
    "expect_exact_text",
    "expect_url",
    "expect_value",
    "expect_count",
    "expect_options",
}


def evidence_path(value: str) -> bool:
    """Both legacy screenshots and immutable per-run evidence paths."""
    return bool(
        re.fullmatch(
            r"evidence/(?:E2E-\d{3,}\.png|e2e/[a-zA-Z0-9_-]+/E2E-\d{3,}/"
            r"[a-zA-Z0-9_-]+\.(?:png|json|jsonl|zip|webm|html|txt|pdf|csv|xlsx))",
            value,
        )
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def rules_from_requirements(requirements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """UI rules are independent of API/interface-expanded test points."""
    result = []
    for requirement in requirements:
        claims = requirement.get("claims") or []
        if not claims:
            claims = [{"statement": requirement.get("description") or requirement.get("title", "")}]
        for index, claim in enumerate(claims, 1):
            if not isinstance(claim, dict):
                continue
            result.append(
                {
                    "rule_id": str(
                        claim.get("id")
                        or claim.get("claim_id")
                        or f"{requirement['id']}-RULE-{index:03d}"
                    ),
                    "requirement_id": requirement["id"],
                    "title": requirement.get("title", ""),
                    "statement": claim.get("statement")
                    or claim.get("description")
                    or requirement.get("description", ""),
                    "acceptance_criteria": claim.get("acceptance_criteria")
                    or requirement.get("acceptance_criteria")
                    or [],
                    "source_refs": claim.get("source_refs") or requirement.get("source_refs") or [],
                }
            )
    return result


def observation_pages(surface: dict[str, Any], size: int = 8000) -> list[dict[str, Any]]:
    """Lossless pages with stable references; every string fragment is retained."""
    atoms: list[dict[str, Any]] = []

    def flatten(value: Any, path: list[str | int]) -> None:
        # Keep a whole control/metadata row together whenever it fits. This
        # preserves context for weaker models without discarding any field.
        packed = json.dumps(value, ensure_ascii=False)
        if len(path) >= 2 and isinstance(value, (dict, list)) and len(packed) <= size // 2:
            atoms.append({"path": path, "value": value})
            return
        if isinstance(value, dict):
            if not value:
                atoms.append({"path": path, "value": {}})
            for key, entry in value.items():
                flatten(entry, [*path, key])
        elif isinstance(value, list):
            if not value:
                atoms.append({"path": path, "value": []})
            for index, entry in enumerate(value):
                flatten(entry, [*path, index])
        elif isinstance(value, str) and len(value) > size // 2:
            for offset in range(0, len(value), size // 2):
                atoms.append(
                    {
                        "path": path,
                        "offset": offset,
                        "length": len(value),
                        "value": value[offset : offset + size // 2],
                    }
                )
        else:
            atoms.append({"path": path, "value": value})

    flatten(surface, [])
    pages: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    count = 0
    for atom in atoms:
        length = len(json.dumps(atom, ensure_ascii=False))
        if current and count + length > size:
            pages.append({"entries": current})
            current = []
            count = 0
        current.append(atom)
        count += length
    if current:
        pages.append({"entries": current})
    for index, page in enumerate(pages, 1):
        page.update(
            {
                "page_id": f"OBS-{index:05d}",
                "page": index,
                "total": len(pages),
                "surface_digest": fingerprint(surface),
            }
        )
    return pages


def validate_step(raw: Any, rules: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("action") not in ACTIONS | ASSERTIONS:
        raise ValueError("步骤必须使用受支持的浏览器动作或断言")
    step = deepcopy(raw)
    action = step["action"]
    if not str(step.get("intent", "")).strip():
        raise ValueError("每一步必须说明业务意图 intent")
    prefixes = ("role=", "text=", "label=", "placeholder=", "testid=")
    if action in TARGET_ACTIONS and not str(step.get("selector", "")).startswith(prefixes):
        if step.get("selector"):
            step["proposed_selector"] = step.pop("selector")
        if not str(step.get("target_intent", "")).strip():
            step["target_intent"] = step["intent"]
    within = step.get("within") or []
    if not isinstance(within, list) or any(
        not isinstance(s, str) or not s.startswith(prefixes) for s in within
    ):
        raise ValueError("within 必须是语义容器定位器数组")
    if action in VALUE_ACTIONS and "value" not in step:
        raise ValueError(f"{action} 缺少 value")
    if action in {"goto", "expect_url"} and (
        not isinstance(step.get("value"), str)
        or not step["value"].startswith("/")
        or step["value"].startswith("//")
    ):
        raise ValueError("页面路径必须为站内相对路径")
    if action.startswith("capture_") and not re.fullmatch(
        r"[A-Z][A-Z0-9_]*", str(step.get("variable", ""))
    ):
        raise ValueError("提取动作必须声明 variable，例如 RECORD_ID")
    if action in {"upload", "fixture", "open_actor"} and not step.get("resource"):
        raise ValueError("上传/准备动作必须引用任务配置中的 resource")
    if action in {"open_popup", "close_page"} and not re.fullmatch(
        r"[a-zA-Z][\w-]*", str(step.get("page", ""))
    ):
        raise ValueError("多窗口动作必须声明 page 名称")
    if action == "drag" and not step.get("destination"):
        raise ValueError("拖拽动作缺少 destination")
    if action == "expect_response" and not step.get("assertions"):
        raise ValueError("业务响应检查必须有明确字段断言")
    if action == "expect_response" and (
        not str(step.get("path", "")).startswith("/")
        or step.get("method") not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    ):
        raise ValueError("响应断言必须指定站内 path 和 HTTP method")
    frame = step.get("frame")
    if frame is not None and (
        not isinstance(frame, dict) or not (frame.get("url") or frame.get("name"))
    ):
        raise ValueError("iframe 必须按真实 url 或 name 标识")
    if action in ASSERTIONS and rules is not None:
        basis = step.get("basis")
        if (
            not isinstance(basis, dict)
            or basis.get("rule_id") not in rules
            or not str(basis.get("quote", "")).strip()
        ):
            raise ValueError("每个业务断言必须引用当前需求 rule_id 及原文 quote")
        # Check actual source fields, not JSON-escaped text.
        texts = [str(rules[basis["rule_id"]].get("statement", ""))]
        criteria = rules[basis["rule_id"]].get("acceptance_criteria", [])
        texts += [str(x) for x in criteria] if isinstance(criteria, list) else [str(criteria)]
        if not any(basis["quote"] in text for text in texts):
            raise ValueError("断言引用不是该需求的原文")
        if action in {"expect_text", "expect_exact_text"}:
            value = step.get("value")
            if (
                isinstance(value, str)
                and value
                and "{{" not in value
                and not any(value in text for text in texts)
            ):
                raise ValueError(
                    f"业务预期文本 {value!r} 不在本需求原文中；"
                    "必须依据需求规定的文本，不能改用实际页面名称"
                )
    return step


def validate_case(raw: Any, rules: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    if (
        not isinstance(raw, dict)
        or not str(raw.get("title", "")).strip()
        or not str(raw.get("expected", "")).strip()
    ):
        raise ValueError("用例缺少 title/expected")
    case = deepcopy(raw)
    if case.get("scenario_type") not in SCENARIOS:
        raise ValueError("场景类型必须明确为正常、边界、异常、权限、状态流转或数据一致性")
    if not isinstance(case.get("steps"), list) or not case["steps"]:
        raise ValueError("用例必须有可执行步骤")
    for phase in ("setup", "precondition_checks", "steps", "cleanup"):
        items = case.get(phase) or []
        if not isinstance(items, list):
            raise ValueError(f"{phase} 必须是数组")
        # Readiness checks establish environment/data; they do not prove a
        # requirement. Only business steps must carry requirement citations.
        case[phase] = [
            validate_step(
                s, rules if phase == "steps" or (isinstance(s, dict) and s.get("basis")) else None
            )
            for s in items
        ]
    assertions = [s for s in case["steps"] if s["action"] in ASSERTIONS]
    if not assertions or all(s["action"] == "expect_url" for s in assertions):
        raise ValueError("业务用例至少有一条可执行结果断言，文字预期不能替代")
    if rules is not None:
        refs = case.get("rule_refs")
        if not isinstance(refs, list) or not refs or any(ref not in rules for ref in refs):
            raise ValueError("用例必须绑定当前需求规则")
        if any(rules[ref]["requirement_id"] != case.get("requirement_id") for ref in refs):
            raise ValueError("用例规则不属于该需求")
        if any(
            s["basis"]["rule_id"] not in refs
            for phase in ("setup", "precondition_checks", "steps", "cleanup")
            for s in case[phase]
            if s["action"] in ASSERTIONS and s.get("basis")
        ):
            raise ValueError("断言来源必须登记在用例 rule_refs 中")
    blockers = list(case.get("execution_blockers") or [])
    if case.get("preconditions") and not case["setup"] and not case["precondition_checks"]:
        blockers.append("文字前置条件尚未绑定可执行准备或检查")
    case.update(
        {
            "contract_version": "2.0",
            "case_type": "e2e",
            "execution_blockers": list(dict.fromkeys(blockers)),
        }
    )
    case["execution_readiness"] = "blocked" if blockers else "ready"
    return case


def same_origin(base: str, other: str) -> bool:
    left, right = urlsplit(base), urlsplit(other)
    return (left.scheme.lower(), left.netloc.lower()) == (
        right.scheme.lower(),
        right.netloc.lower(),
    )
