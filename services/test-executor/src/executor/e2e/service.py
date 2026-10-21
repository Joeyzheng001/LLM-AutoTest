"""Standalone E2E operations; no dependency on API oracles or nine-node order."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from common.artifacts import ArtifactStore
from common.e2e_contracts import fingerprint
from common.llm_repair import SupportsComplete

from executor.e2e.evidence import now
from executor.e2e.explorer import explore
from executor.e2e.planner import generate_cases, recover_target
from executor.e2e.reporting import defects_for_run, render_html, summarize
from executor.e2e.runtime import execute_case


def optional(store: ArtifactStore, job_id: str, filename: str) -> dict[str, Any]:
    try:
        return store.read_json(job_id, filename)
    except FileNotFoundError:
        return {}


async def generate_job(
    store: ArtifactStore,
    job_id: str,
    llm: SupportsComplete,
    prompts_dir: Path,
    config: dict[str, Any],
    auth: dict[str, Any] | None = None,
    auth_headers: dict[str, str] | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    requirements = store.read_json(job_id, "requirement_items.json")["items"]
    previous = optional(store, job_id, "e2e_design.json") if resume else {}
    run_id = str(previous.get("run_id") or uuid.uuid4().hex)
    cfg = {**config, "run_id": run_id}

    def write(path: str, content: bytes) -> str:
        return (
            store.write_bytes(job_id, path, content).relative_to(store.job_dir(job_id)).as_posix()
        )

    # Resume only the captured observation, never reinterpret a partial capture
    # as a fresh scan. A new generation explicitly sets resume=False.
    surface = (
        store.read_json(job_id, previous["surface_ref"]) if previous.get("surface_ref") else {}
    )
    if surface.get("base_url") != str(config["base_url"]).rstrip("/"):
        previous = {}
        run_id = uuid.uuid4().hex
        cfg["run_id"] = run_id
        surface = await explore(cfg, auth, write, auth_headers)
    surface_ref = f"evidence/e2e/{run_id}/E2E-000/surface.json"
    write(surface_ref, json.dumps(surface, ensure_ascii=False).encode())

    generation_started = False

    def save(plan: dict[str, Any]) -> None:
        nonlocal generation_started, run_id
        if not generation_started:
            generation_started = True
            if previous and previous.get("input_digest") != plan.get("input_digest"):
                run_id = uuid.uuid4().hex
        if plan.get("status") == "running":
            plan["cases"] = [
                c
                for result in plan.get("rule_results", [])
                for c in result.get("cases", [])
                if c.get("id")
            ]
            plan["scenarios"] = [
                s for result in plan.get("rule_results", []) for s in result.get("scenarios", [])
            ]
            plan["gaps"] = [
                g | {"rule_id": result["rule_id"]}
                for result in plan.get("rule_results", [])
                for g in result.get("gaps", [])
            ]
        store.write(
            job_id,
            "e2e_design.json",
            {
                **plan,
                "schema_version": "1.0",
                "job_id": job_id,
                "run_id": run_id,
                "surface_ref": surface_ref,
                "updated_at": now(),
            },
        )

    cases, plan = await generate_cases(llm, prompts_dir, requirements, surface, cfg, save, previous)
    plan.update(
        {
            "schema_version": "1.0",
            "job_id": job_id,
            "run_id": run_id,
            "surface_ref": surface_ref,
            "exploration_pending": surface.get("pending", []),
            "updated_at": now(),
        }
    )
    if surface.get("pending"):
        plan["status"] = "incomplete"
    save(plan)
    return {**plan, "cases": cases}


async def execute_job(
    store: ArtifactStore,
    job_id: str,
    llm: SupportsComplete | None,
    prompts_dir: Path,
    config: dict[str, Any],
    auth: dict[str, Any] | None = None,
    auth_headers: dict[str, str] | None = None,
    case_ids: set[str] | None = None,
    progress: Callable[[dict[str, Any]], Any] | None = None,
    cases_override: list[dict[str, Any]] | None = None,
    collect_defects: bool = True,
) -> dict[str, Any]:
    design = store.read_json(job_id, "e2e_design.json")
    cases = cases_override if cases_override is not None else design.get("cases", [])
    if case_ids is not None:
        cases = [c for c in cases if c["id"] in case_ids]
    if not cases:
        raise ValueError("没有可执行的 E2E 用例，设计缺口仍保留在 e2e_design.json")
    run_id = uuid.uuid4().hex
    cfg = {**config, "run_id": run_id}

    def write(path: str, content: bytes) -> str:
        return (
            store.write_bytes(job_id, path, content).relative_to(store.job_dir(job_id)).as_posix()
        )

    async def recover(step: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any] | None:
        if llm is None:
            return None
        return await recover_target(llm, prompts_dir, step, observation)

    results: list[dict[str, Any]] = [
        {
            "case_id": c["id"],
            "case_type": "e2e",
            "interface_id": "UI-E2E-001",
            "timestamp": now(),
            "run_id": run_id,
            "title": c["title"],
            "expected": c["expected"],
            "requirement_id": c["requirement_id"],
            "passed": False,
            "outcome": "not_executed",
            "execution_state": "not_executed",
            "scenario_type": c.get("scenario_type", "normal"),
            "step_results": [],
            "assertion_results": [],
            "failure_reason": "等待执行",
        }
        for c in cases
    ]
    payload = {
        "schema_version": "1.0",
        "job_id": job_id,
        "run_id": run_id,
        "design_run_id": design["run_id"],
        "results": results,
        "summary": summarize(results),
        "status": "running",
        "scope": "selected" if case_ids is not None or cases_override is not None else "all",
        "planned_cases": len(cases),
    }
    store.write(job_id, "e2e_execution.json", payload)
    from common.e2e_contracts import validate_case

    rules = {r["rule_id"]: r for r in design.get("rules", [])}
    try:
        for index, case in enumerate(cases):
            validate_case(case, rules or None)

            def report_step(value: dict[str, Any], position: int = index) -> None:
                if value.get("step_result"):
                    current = results[position]
                    current.update(
                        {
                            "outcome": "running",
                            "execution_state": "observed_unjudged",
                            "failure_reason": "正在执行",
                        }
                    )
                    steps = current["step_results"]
                    steps[:] = [s for s in steps if s["step_id"] != value["step_result"]["step_id"]]
                    steps.append(value["step_result"])
                    payload["summary"] = summarize(results)
                    store.write(job_id, "e2e_execution.json", payload)
                if progress:
                    progress({k: v for k, v in value.items() if k != "step_result"})

            result = await execute_case(case, cfg, write, auth, auth_headers, recover, report_step)
            results[index] = result
            payload["summary"] = summarize(results)
            store.write(job_id, "e2e_execution.json", payload)
    except BaseException:
        payload["status"] = "failed"
        payload["summary"] = summarize(results)
        store.write(job_id, "e2e_execution.json", payload)
        raise
    payload["status"] = "completed"
    prior_defects = optional(store, job_id, "e2e_defects.json") or {
        "schema_version": "1.0",
        "job_id": job_id,
        "defects": [],
    }
    defects = (
        defects_for_run(job_id, results, cases, prior_defects) if collect_defects else prior_defects
    )
    store.write(job_id, "e2e_defects.json", defects)
    html = render_html(job_id, results, design, defects)
    # Report is exported at the package root so evidence paths remain portable.
    report_ref = write(f"evidence/e2e/{run_id}/E2E-000/report.html", html.encode())
    payload["report_ref"] = report_ref
    store.write(job_id, "e2e_execution.json", payload)
    history = optional(store, job_id, "e2e_history.json")
    runs = history.get("runs", [])
    runs.append(
        {
            "run_id": run_id,
            "design_run_id": design["run_id"],
            "timestamp": now(),
            "summary": payload["summary"],
            "execution_ref": write(
                f"evidence/e2e/{run_id}/E2E-000/execution.json",
                json.dumps(payload, ensure_ascii=False).encode(),
            ),
        }
    )
    store.write(
        job_id, "e2e_history.json", {"schema_version": "1.0", "job_id": job_id, "runs": runs}
    )
    archive_regression(store, job_id, design, cases, results, runs)
    return payload


async def retest_job(
    store: ArtifactStore,
    job_id: str,
    llm: SupportsComplete,
    prompts_dir: Path,
    config: dict[str, Any],
    auth: dict[str, Any] | None = None,
    auth_headers: dict[str, str] | None = None,
    defect_ids: set[str] | None = None,
) -> dict[str, Any]:
    original = store.read_json(job_id, "e2e_defects.json")
    selected = [d for d in original["defects"] if defect_ids is None or d["id"] in defect_ids]
    if any(not d.get("case_snapshot") for d in selected):
        raise ValueError("部分历史缺陷没有原始用例快照，禁止改用同编号的新场景复测")
    snapshots: dict[str, dict[str, Any]] = {}
    execution_ids: dict[str, str] = {}
    for defect in selected:
        key = fingerprint(defect["case_snapshot"])
        if key not in snapshots:
            snapshots[key] = {**defect["case_snapshot"], "id": f"E2E-800{len(snapshots) + 1:06d}"}
        execution_ids[defect["id"]] = snapshots[key]["id"]
    result = await execute_job(
        store,
        job_id,
        llm,
        prompts_dir,
        config,
        auth,
        auth_headers,
        cases_override=list(snapshots.values()),
        collect_defects=False,
    )
    defects = store.read_json(job_id, "e2e_defects.json")
    by_case = {r["case_id"]: r for r in result["results"]}
    for defect in defects["defects"]:
        if defect["id"] not in {d["id"] for d in selected}:
            continue
        actual = by_case.get(execution_ids[defect["id"]])
        if not actual:
            continue
        original_step = (defect.get("actual", {}).get("failed_step") or {}).get("step_id")
        same_failure = (
            actual["outcome"] == "assertion_failed"
            and actual.get("failed_step_id") == original_step
        )
        if actual["outcome"] != "passed" and not same_failure:
            defect.setdefault("retest_attempts", []).append(
                {
                    "run_id": result["run_id"],
                    "outcome": actual["outcome"],
                    "reason": actual.get("failure_reason"),
                    "original_failure_verified": False,
                }
            )
            continue
        passed = actual["outcome"] == "passed"
        defect["status"] = "closed_suggested" if passed else "retained"
        defect["history"].extend(
            [
                {
                    "event": "retest_started",
                    "timestamp": actual["timestamp"],
                    "run_id": result["run_id"],
                },
                {
                    "event": "retest_passed" if passed else "retest_failed",
                    "timestamp": now(),
                    "run_id": result["run_id"],
                },
            ]
        )
        defect["last_retest"] = actual
    store.write(job_id, "e2e_defects.json", defects)
    return result


async def regress_job(
    store: ArtifactStore,
    job_id: str,
    llm: SupportsComplete,
    prompts_dir: Path,
    config: dict[str, Any],
    auth: dict[str, Any] | None = None,
    auth_headers: dict[str, str] | None = None,
    fingerprints: set[str] | None = None,
) -> dict[str, Any]:
    manifest = store.read_json(job_id, "e2e_regression.json")
    entries = [
        e
        for e in manifest.get("entries", [])
        if fingerprints is None or e["case_fingerprint"] in fingerprints
    ]
    if not entries:
        raise ValueError("没有匹配的回归资产")
    # Multiple historical versions can share the visible case ID. Give each
    # immutable snapshot a distinct execution ID, without changing its steps.
    cases = []
    for index, entry in enumerate(entries, 1):
        case = {**entry["case_snapshot"], "id": f"E2E-900{index:06d}"}
        cases.append(case)
    return await execute_job(
        store, job_id, llm, prompts_dir, config, auth, auth_headers, cases_override=cases
    )


def archive_regression(
    store: ArtifactStore,
    job_id: str,
    design: dict[str, Any],
    cases: list[dict[str, Any]],
    results: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> None:
    manifest = optional(store, job_id, "e2e_regression.json")
    entries = {e["case_fingerprint"]: e for e in manifest.get("entries", [])}
    definitions = {c["id"]: c for c in cases}
    for result in results:
        if (
            not result.get("passed")
            or not result.get("evidence_complete")
            or not result.get("case_fingerprint")
        ):
            continue
        signature = result["case_fingerprint"]
        successes = {result["run_id"]}
        for run in runs:
            previous = store.read_json(job_id, run["execution_ref"])
            if previous.get("design_run_id") != design["run_id"]:
                continue
            for old in previous.get("results", []):
                if (
                    old.get("case_fingerprint") == signature
                    and old.get("passed")
                    and old.get("evidence_complete")
                ):
                    successes.add(old["run_id"])
        if len(successes) < 2:
            continue
        old = entries.get(signature, {})
        entries[signature] = {
            "case_id": old.get("case_id") or result["case_id"],
            "case_fingerprint": signature,
            "case_snapshot": old.get("case_snapshot") or definitions[result["case_id"]],
            "design_run_id": design["run_id"],
            "successful_runs": sorted(successes),
            "archived_at": old.get("archived_at") or now(),
            "last_verified_at": now(),
            "version": old.get("version")
            or 1
            + max(
                (
                    int(e.get("version", 0))
                    for e in entries.values()
                    if e.get("case_id") == result["case_id"]
                ),
                default=0,
            ),
        }
    store.write(
        job_id,
        "e2e_regression.json",
        {"schema_version": "1.0", "job_id": job_id, "entries": list(entries.values())},
    )
