"""Requirement-based planning and grounded case generation using the task model."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from common.async_work import ordered_parallel_map
from common.e2e_contracts import (
    ASSERTIONS,
    SCENARIOS,
    fingerprint,
    observation_pages,
    rules_from_requirements,
    validate_case,
)
from common.errors import X_LLM_02, PipelineError
from common.llm_repair import SupportsComplete, complete_with_json_repair
from common.llm_text import strip_code_fence
from common.prompts_loader import load_prompt

from executor.e2e.explorer import canonical_target


def parse_object(text: str, key: str) -> Any:
    try:
        obj = json.loads(strip_code_fence(text))
        return obj[key]
    except (ValueError, KeyError, TypeError) as exc:
        raise PipelineError(
            code=X_LLM_02, node_id="testcase_generate", message=f"E2E 返回必须包含 {key}：{exc}"
        ) from exc


async def generate_cases(
    llm: SupportsComplete,
    prompts_dir: Path,
    requirements: list[dict[str, Any]],
    surface: dict[str, Any],
    config: dict[str, Any],
    save: Callable[[dict[str, Any]], None] | None = None,
    previous: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rules = rules_from_requirements(requirements)
    routes = [
        {**r, "route_id": str(r.get("state_id") or f"PAGE-{i:05d}")}
        for i, r in enumerate(surface.get("routes", []), 1)
    ]
    index = [
        {
            "route_id": r["route_id"],
            "path": r["path"],
            "title": r.get("title"),
            "menu_text": r.get("menu_text"),
            "state": r.get("state"),
            "frames": [
                {
                    "identity": f.get("identity"),
                    "text": f.get("text"),
                    "selectors": [
                        selector
                        for e in f.get("elements", [])
                        for selector in e.get("selectors", [])
                    ],
                }
                for f in r.get("frames", [])
            ],
        }
        for r in routes
    ]
    plan_prompt = load_prompt(prompts_dir, "e2e_scenario_plan")
    ground_prompt = load_prompt(prompts_dir, "e2e_grounding")
    case_prompt = load_prompt(prompts_dir, "e2e_testcase_generate")
    review_prompt = load_prompt(prompts_dir, "e2e_case_review")
    digest = fingerprint(
        {
            "rules": rules,
            "surface": surface,
            "contract": "e2e-2.1-paging",
            "available_configuration": {
                "roles": list(config.get("roles", {})),
                "fixtures": list(config.get("data_fixtures", {})),
                "files": list(config.get("files", {})),
            },
            "prompts": [p.body for p in (plan_prompt, ground_prompt, case_prompt, review_prompt)],
        }
    )
    plan: dict[str, Any] = {
        "version": "2.0",
        "input_digest": digest,
        "rules": rules,
        "scenarios": [],
        "gaps": [],
        "observations": {"digest": fingerprint(surface), "routes": len(routes)},
        "status": "running",
    }
    if previous and previous.get("input_digest") == digest:
        plan = previous
    plan["status"] = "running"
    positions = {rule["rule_id"]: index for index, rule in enumerate(rules, 1)}
    completed = {
        x["rule_id"]: x
        for x in plan.get("rule_results", [])
        if x.get("status") == "completed" and not x.get("gaps")
    }
    plan.setdefault("rule_results", [])

    async def work(rule: dict[str, Any]) -> dict[str, Any]:
        if rule["rule_id"] in completed:
            return completed[rule["rule_id"]]
        output: dict[str, Any] = {
            "rule_id": rule["rule_id"],
            "scenarios": [],
            "cases": [],
            "gaps": [],
            "status": "running",
        }
        plan["rule_results"] = [r for r in plan["rule_results"] if r["rule_id"] != rule["rule_id"]]
        plan["rule_results"].append(output)
        if save:
            save(plan)
        for directory_page in observation_pages({"pages": index}):

            def parse_plan(text: str) -> tuple[list[dict[str, Any]], list[str]]:
                data = parse_object(text, "scenarios")
                if not isinstance(data, list):
                    raise PipelineError(code=X_LLM_02, message="scenarios 必须是数组")
                for item in data:
                    if (
                        not isinstance(item, dict)
                        or not item.get("title")
                        or not item.get("expected")
                    ):
                        raise PipelineError(code=X_LLM_02, message="场景缺少 title/expected")
                    if item.get("scenario_type") not in SCENARIOS:
                        raise PipelineError(code=X_LLM_02, message="场景类型必须来自约定类型")
                    refs = item.get("route_ids")
                    if (
                        not isinstance(refs, list)
                        or not refs
                        or any(ref not in {r["route_id"] for r in routes} for ref in refs)
                    ):
                        raise PipelineError(code=X_LLM_02, message="场景必须关联真实页面编号")
                    quote = str(item.get("quote", ""))
                    criteria = rule.get("acceptance_criteria") or []
                    source = [str(rule["statement"])] + (
                        [str(c) for c in criteria]
                        if isinstance(criteria, list)
                        else [str(criteria)]
                    )
                    if not quote or not any(quote in s for s in source):
                        raise PipelineError(code=X_LLM_02, message="场景 quote 必须是当前需求原文")
                details = json.loads(strip_code_fence(text)).get("gaps", [])
                return data, [str(g) for g in details] if isinstance(details, list) else []

            try:
                scenarios, missing = await complete_with_json_repair(
                    llm,
                    "e2e_plan",
                    plan_prompt,
                    json.dumps(
                        {
                            "rule": rule,
                            "page_directory": directory_page,
                            "owners": [
                                {k: v for k, v in index[i].items() if k != "frames"}
                                | {
                                    "frames": [
                                        {"identity": f.get("identity")}
                                        for f in index[i].get("frames", [])
                                    ]
                                }
                                for i in sorted(
                                    {
                                        entry["path"][1]
                                        for entry in directory_page["entries"]
                                        if len(entry["path"]) > 1
                                        and isinstance(entry["path"][1], int)
                                    }
                                )
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    parse_plan,
                )
                output["scenarios"].extend(scenarios)
                output.setdefault("directory_notes", []).extend(
                    {"page_id": directory_page["page_id"], "reason": reason} for reason in missing
                )
            except PipelineError as exc:
                output["gaps"].append(
                    {"stage": "plan", "page_id": directory_page["page_id"], "reason": exc.message}
                )
        unique = {
            fingerprint(
                {k: s.get(k) for k in ("title", "scenario_type", "expected", "route_ids")}
            ): s
            for s in output["scenarios"]
        }
        output["scenarios"] = list(unique.values())
        if save:
            save(plan)
        if not unique:
            output["gaps"].append(
                {"stage": "coverage", "reason": "当前规则没有形成业务场景；保留需求覆盖缺口"}
            )
        grounding_cache: dict[
            str, tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]
        ] = {}
        for number, scenario in enumerate(output["scenarios"], 1):
            scenario["scenario_id"] = f"{rule['rule_id']}-SCN-{number:04d}"
            selected = [r for r in routes if r["route_id"] in scenario["route_ids"]]
            pages = observation_pages({"routes": selected})
            cache_key = fingerprint(scenario["route_ids"])
            facts, pages_read, page_failures = grounding_cache.get(cache_key, ([], [], []))
            scenario["evidence_pages"] = {
                "total": len(pages),
                "read": pages_read,
                "failures": page_failures,
            }
            for page in [] if cache_key in grounding_cache else pages:

                def parse_facts(
                    text: str,
                    selected: list[dict[str, Any]] = selected,
                    page: dict[str, Any] = page,
                ) -> list[dict[str, Any]]:
                    items = parse_object(text, "facts")
                    if not isinstance(items, list):
                        raise PipelineError(code=X_LLM_02, message="facts 必须是数组")
                    allowed = {
                        (s, json.dumps(f.get("identity"), sort_keys=True))
                        for route in selected
                        for f in route.get("frames", [])
                        for e in f.get("elements", [])
                        for s in e.get("selectors", [])
                    }
                    retained = []
                    for item in items:
                        if not isinstance(item, dict):
                            output.setdefault("grounding_diagnostics", []).append(
                                {
                                    "page_id": page["page_id"],
                                    "reason": "非对象定位事实",
                                    "raw": item,
                                }
                            )
                            continue
                        if item.get("source_page") != page["page_id"]:
                            output.setdefault("grounding_diagnostics", []).append(
                                {
                                    "page_id": page["page_id"],
                                    "reason": "事实引用不属于当前观察页",
                                    "raw": item,
                                }
                            )
                            continue
                        if item.get("selector"):
                            try:
                                if not any(
                                    contains_value(entry.get("value"), item["selector"])
                                    for entry in page["entries"]
                                ):
                                    raise ValueError("定位器未出现在当前观察分片中")
                                item.update(
                                    canonical_target(
                                        item,
                                        [f for route in selected for f in route.get("frames", [])],
                                    )
                                )
                                if (
                                    item["selector"],
                                    json.dumps(item.get("frame"), sort_keys=True),
                                ) not in allowed:
                                    raise ValueError("定位器不在当前真实页面中")
                            except ValueError as exc:
                                # A bad optional locator is not unread source. Preserve
                                # the diagnosis, and ground that control at runtime.
                                output.setdefault("grounding_diagnostics", []).append(
                                    {"page_id": page["page_id"], "reason": str(exc), "raw": item}
                                )
                                continue
                        retained.append(item)
                    return retained

                try:
                    facts.extend(
                        await complete_with_json_repair(
                            llm,
                            "e2e_grounding",
                            ground_prompt,
                            json.dumps(
                                {
                                    "rule": rule,
                                    "scenario": {
                                        "title": rule["title"],
                                        "expected": rule["statement"],
                                    },
                                    "observation": page,
                                    "owners": [
                                        {
                                            "route_id": r["route_id"],
                                            "path": r["path"],
                                            "frames": [
                                                {"identity": f.get("identity")}
                                                for f in r.get("frames", [])
                                            ],
                                        }
                                        for r in selected
                                    ],
                                },
                                ensure_ascii=False,
                            ),
                            parse_facts,
                        )
                    )
                    pages_read.append(page["page_id"])
                    plan["progress"] = {
                        "intent": "读取页面证据",
                        "rule_id": rule["rule_id"],
                        "pages_read": len(pages_read),
                        "pages_total": len(pages),
                    }
                    if save:
                        save(plan)
                except PipelineError as exc:
                    page_failures.append({"page_id": page["page_id"], "reason": exc.message})
            grounding_cache[cache_key] = (facts, pages_read, page_failures)
            if save:
                save(plan)
            output["gaps"].extend(
                {"stage": "grounding", "scenario_id": scenario["scenario_id"], **failure}
                for failure in page_failures
            )
            scenario["evidence_pages"] = {
                "total": len(pages),
                "read": pages_read,
                "failures": page_failures,
            }

            def parse_case(
                text: str, scenario: dict[str, Any] = scenario, facts: list[dict[str, Any]] = facts
            ) -> list[dict[str, Any]]:
                entries = parse_object(text, "cases")
                if not isinstance(entries, list) or len(entries) != 1:
                    raise PipelineError(
                        code=X_LLM_02,
                        message="本次必须保留当前一条业务场景，不能输出空数组或其它场景",
                    )
                try:
                    candidate = entries[0]
                    if not isinstance(candidate, dict):
                        raise ValueError("用例必须是对象")
                    # IDs and source bindings are unambiguous in a single-rule
                    # batch. Compile omitted bookkeeping; don't ask the model to
                    # duplicate it or drop a business scenario for missing IDs.
                    candidate.setdefault("requirement_id", rule["requirement_id"])
                    candidate.setdefault("rule_refs", [rule["rule_id"]])
                    for step in candidate.get("steps", []):
                        if not isinstance(step, dict) or step.get("action") not in ASSERTIONS:
                            continue
                        basis = step.get("basis")
                        if not basis:
                            step["basis"] = {"rule_id": rule["rule_id"], "quote": rule["statement"]}
                        if (
                            step.get("action") in {"expect_text", "expect_exact_text"}
                            and "value" not in step
                            and str(step.get("selector", "")).startswith("text=")
                        ):
                            literal = step["selector"][5:]
                            if literal and literal in rule["statement"]:
                                step["value"] = literal
                    available_roles = {"default", "anonymous", *config.get("roles", {})}
                    if candidate.get("role", "default") not in available_roles:
                        if not config.get("roles") and scenario["scenario_type"] != "permission":
                            candidate["role"] = "default"
                        else:
                            candidate.setdefault("execution_blockers", []).append(
                                f"业务角色 {candidate.get('role')} 尚未配置登录资料"
                            )
                    case = validate_case(candidate, {rule["rule_id"]: rule})
                    if case["scenario_type"] != scenario["scenario_type"]:
                        raise ValueError("不能改变规划场景类型")
                    allowed = {
                        (str(f.get("selector")), json.dumps(f.get("frame"), sort_keys=True))
                        for f in facts
                        if f.get("selector")
                    }
                    for phase in ("setup", "precondition_checks", "steps", "cleanup"):
                        for step in case[phase]:
                            if not step.get("selector"):
                                continue
                            key = (
                                str(step["selector"]),
                                json.dumps(step.get("frame"), sort_keys=True),
                            )
                            if key in allowed:
                                continue
                            if step["action"].startswith("expect_") and step["selector"].startswith(
                                "text="
                            ):
                                continue
                            step["proposed_selector"] = step.pop("selector")
                            step.setdefault("target_intent", step["intent"])
                            step["grounding_status"] = "runtime_observation_required"
                    return [case]
                except ValueError as exc:
                    raise PipelineError(
                        code=X_LLM_02, message=str(exc), node_id="testcase_generate"
                    ) from exc

            try:
                cases = await complete_with_json_repair(
                    llm,
                    "e2e_case",
                    case_prompt,
                    json.dumps(
                        {
                            "rule": rule,
                            "scenario": scenario,
                            "base_url": surface["base_url"],
                            "pages": [
                                {
                                    k: r.get(k)
                                    for k in (
                                        "route_id",
                                        "path",
                                        "title",
                                        "state",
                                        "role",
                                        "entry_steps",
                                        "menu_text",
                                    )
                                }
                                for r in selected
                            ],
                            "facts": facts,
                            "evidence_manifest": scenario["evidence_pages"],
                            "available_roles": [
                                "default",
                                "anonymous",
                                *list(config.get("roles", {})),
                            ],
                            "available_resources": {
                                "fixtures": list(config.get("data_fixtures", {})),
                                "files": list(config.get("files", {})),
                            },
                        },
                        ensure_ascii=False,
                    ),
                    parse_case,
                )
                drafts = cases

                def parse_review(
                    text: str, drafts: list[dict[str, Any]] = drafts
                ) -> list[dict[str, Any]]:
                    from collections import Counter

                    reviewed = parse_case(text)

                    def signatures(definition: dict[str, Any]) -> list[str]:
                        return [
                            fingerprint(
                                {k: step.get(k) for k in ("action", "value", "assertions")}
                                | {"rule_id": step.get("basis", {}).get("rule_id")}
                            )
                            for step in definition["steps"]
                            if step["action"] in ASSERTIONS
                        ]

                    before, after = Counter(signatures(drafts[0])), Counter(signatures(reviewed[0]))
                    if before - after:
                        raise PipelineError(
                            code=X_LLM_02,
                            message="复核不得删改原有业务断言的 action/value/basis；"
                            "只可改定位、补路径或新增有依据的断言。原业务步骤："
                            + json.dumps(drafts[0]["steps"], ensure_ascii=False),
                        )
                    return reviewed

                try:
                    cases = await complete_with_json_repair(
                        llm,
                        "e2e_case_review",
                        review_prompt,
                        json.dumps(
                            {
                                "rule": rule,
                                "scenario": scenario,
                                "cases": cases,
                                "facts": facts,
                                "pages": [
                                    {
                                        k: r.get(k)
                                        for k in ("route_id", "path", "state", "entry_steps")
                                    }
                                    for r in selected
                                ],
                                "available_roles": [
                                    "default",
                                    "anonymous",
                                    *config.get("roles", {}),
                                ],
                                "available_resources": {
                                    "fixtures": list(config.get("data_fixtures", {})),
                                    "files": list(config.get("files", {})),
                                },
                            },
                            ensure_ascii=False,
                        ),
                        parse_review,
                    )
                    for case in cases:
                        case["review_status"] = "reviewed"
                except PipelineError as exc:
                    output["gaps"].append(
                        {
                            "stage": "case_review",
                            "scenario_id": scenario["scenario_id"],
                            "reason": exc.message,
                        }
                    )
                    for case in cases:
                        case["review_status"] = "pending"
                for case in cases:
                    case["design_id"] = scenario["scenario_id"]
                    case["id"] = f"E2E-{positions[rule['rule_id']]:03d}{number:03d}"
                    case["source_observations"] = pages_read
                    if page_failures:
                        case["execution_blockers"].append(
                            "部分页面证据尚未处理完成，不能声称完整设计"
                        )
                        case["execution_readiness"] = "blocked"
                    output["cases"].append(case)
                    scenario["design_status"] = "retained"
            except PipelineError as exc:
                scenario.update({"design_status": "pending", "reason": exc.message})
                output["gaps"].append(
                    {"stage": "case", "scenario_id": scenario["scenario_id"], "reason": exc.message}
                )
            if save:
                save(plan)
        output["status"] = "incomplete" if output["gaps"] else "completed"
        if save:
            save(plan)
        return output

    results = await ordered_parallel_map(
        rules, work, concurrency=max(1, int(config.get("max_concurrency", 4)))
    )
    cases = [c for result in results for c in result["cases"]]
    for index_, case in enumerate(cases, 1):
        case.setdefault("id", f"E2E-{index_:03d}")
        if case.get("rule_refs") and re_claim(case["rule_refs"][0]):
            case["claim_id"] = case["rule_refs"][0]
    plan.update(
        {
            "scenarios": [s for r in results for s in r["scenarios"]],
            "gaps": [g | {"rule_id": r["rule_id"]} for r in results for g in r["gaps"]],
            "coverage": {
                "rules": len(rules),
                "rules_with_cases": len({ref for c in cases for ref in c["rule_refs"]}),
                "scenarios": sum(len(r["scenarios"]) for r in results),
                "cases": len(cases),
            },
            "cases": cases,
            "status": "incomplete" if any(r["gaps"] for r in results) else "completed",
        }
    )
    if save:
        save(plan)
    return cases, plan


def re_claim(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"CLM-\d{3,}", value))


async def recover_target(
    llm: SupportsComplete, prompts_dir: Path, step: dict[str, Any], observation: dict[str, Any]
) -> dict[str, Any] | None:
    from executor.e2e.explorer import semantic_selectors

    direct = direct_target(step, observation)
    if direct is not None:
        return direct
    allowed = set()
    for frame in observation.get("frames", []):
        for element in frame.get("elements", []):
            for selector in semantic_selectors(element):
                allowed.add((selector, json.dumps(frame.get("identity"), sort_keys=True)))
    targets: dict[str, dict[str, Any]] = {}
    prompt = load_prompt(prompts_dir, "e2e_relocate")
    for page in observation_pages(observation):

        def parse(text: str) -> dict[str, Any] | None:
            target = parse_object(text, "target")
            if target is None:
                return None
            if not isinstance(target, dict) or set(target) - {
                "selector",
                "frame",
                "within",
                "reason",
                "select_value",
            }:
                raise PipelineError(code=X_LLM_02, message="重新定位只能返回目标及受控选择适配")
            compound = re.fullmatch(
                r"(role=combobox\[name=(.+?)\]|label=.+?)\s*>>\s*role=option\[name=(.+)\]",
                str(target.get("selector", "")),
            )
            if compound:
                parent, label = compound[1], compound[3].strip("\"'")
                matches = [
                    o
                    for f in observation.get("frames", [])
                    for e in f.get("elements", [])
                    if e.get("tag") == "select" and parent in e.get("selectors", [])
                    for o in e.get("options", [])
                    if o.get("label") == label
                ]
                if len(matches) == 1:
                    target = {**target, "selector": parent, "select_value": matches[0]["value"]}
            try:
                target = canonical_target(target, observation.get("frames", []))
            except ValueError as exc:
                raise PipelineError(code=X_LLM_02, message=str(exc)) from exc
            if (
                target.get("selector"),
                json.dumps(target.get("frame"), sort_keys=True),
            ) not in allowed:
                raise PipelineError(code=X_LLM_02, message="重新定位目标不是当前真实控件")
            if "select_value" in target:
                goal = " ".join(
                    str(step.get(k, ""))
                    for k in ("intent", "target_intent", "selector", "proposed_selector")
                )
                options = [
                    o
                    for f in observation.get("frames", [])
                    if f.get("identity") == target.get("frame")
                    for e in f.get("elements", [])
                    if e.get("tag") == "select" and target["selector"] in e.get("selectors", [])
                    for o in e.get("options", [])
                    if o.get("value") == target["select_value"] and not o.get("disabled")
                ]
                if (
                    step.get("action") not in {"click", "select"}
                    or len(options) != 1
                    or not any(
                        str(options[0].get(k) or "") in goal
                        for k in ("label", "value")
                        if len(str(options[0].get(k) or "")) >= 2
                    )
                ):
                    raise PipelineError(
                        code=X_LLM_02, message="选择适配必须是原业务意图中的真实选项"
                    )
                if step.get("action") == "select" and step.get("value") != target["select_value"]:
                    raise PipelineError(code=X_LLM_02, message="不得更换原始选择输入")
            return target

        target = await complete_with_json_repair(
            llm,
            "e2e_relocate",
            prompt,
            json.dumps({"original_step": step, "observation": page}, ensure_ascii=False),
            parse,
        )
        if target:
            targets[
                fingerprint(
                    {k: target.get(k) for k in ("selector", "frame", "within", "select_value")}
                )
            ] = target
    return next(iter(targets.values())) if len(targets) == 1 else None


def contains_value(value: Any, target: str) -> bool:
    if isinstance(value, dict):
        return any(contains_value(v, target) for v in value.values())
    if isinstance(value, list):
        return any(contains_value(v, target) for v in value)
    return value == target


def direct_target(step: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve explicit, unique UI names/options mechanically; never infer an oracle."""
    action = str(step.get("action", ""))
    goal = " ".join(
        str(step.get(k) or "") for k in ("intent", "target_intent", "selector", "proposed_selector")
    )
    candidates = []
    exact_texts = []
    for frame in observation.get("frames", []):
        for element in frame.get("elements", []):
            if not element.get("visible"):
                continue
            selectors = element.get("selectors") or []
            if not selectors:
                continue
            target = {
                "selector": selectors[0],
                "frame": frame.get("identity"),
                "reason": "当前浏览器观察中的唯一名称/选项与原业务输入逐字匹配",
                "resolution": "observed_exact_match",
            }
            choice_check = action == "expect_visible" and not any(
                word in str((step.get("basis") or {}).get("quote", ""))
                for word in ("按钮", "button")
            )
            if element.get("tag") == "select" and (action in {"click", "select"} or choice_check):
                options = [
                    o
                    for o in element.get("options") or []
                    if not o.get("disabled")
                    and (
                        (action == "select" and o.get("value") == step.get("value"))
                        or (
                            (action == "click" or choice_check)
                            and any(
                                str(o.get(k) or "") in goal
                                for k in ("label", "value")
                                if len(str(o.get(k) or "")) >= 2
                            )
                        )
                    )
                ]
                if len(options) == 1:
                    key = "option_present" if choice_check else "select_value"
                    candidates.append({**target, key: options[0]["value"]})
                continue
            if step.get("selector") and not step.get("target_intent"):
                continue
            name = str(
                element.get("aria") or element.get("label") or element.get("text") or ""
            ).strip()
            desired = str(step.get("target_intent") or "")
            value = step.get("value")
            named = len(name) >= 2 and re.sub(r"\s+", "", name) in re.sub(r"\s+", "", desired)
            textual = (
                action in {"expect_text", "expect_exact_text"}
                and isinstance(value, str)
                and name == value
            )
            if not (named or textual):
                continue
            if (
                action in {"click", "dblclick", "hover"}
                and element.get("tag") not in {"button", "a", "li"}
                and element.get("role") not in {"button", "tab", "menuitem", "link"}
            ):
                continue
            candidates.append(target)
            if textual:
                exact_texts.append(target)
    if exact_texts:
        return exact_texts[0] if len(exact_texts) == 1 else None
    if len(candidates) == 1:
        return candidates[0]
    return None
