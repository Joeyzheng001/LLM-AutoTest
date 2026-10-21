"""Deterministic E2E summaries, reproducible defects and portable HTML evidence."""

from __future__ import annotations

import json
import re
from collections import Counter
from html import escape
from typing import Any

from common.e2e_contracts import fingerprint


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    states = Counter(r.get("outcome", "unjudged") for r in results)
    return {
        "total": len(results),
        "passed": states["passed"],
        "failed_assertion": states["assertion_failed"],
        "failed_environment": states["environment_failed"] + states["automation_error"],
        "blocked": states["blocked"],
        "pending": states["not_executed"] + states["running"],
        "unjudged": states["unjudged"],
        "automation_errors": states["automation_error"],
        "observed_unjudged": sum(r.get("execution_state") == "observed_unjudged" for r in results),
        "not_executed": sum(r.get("execution_state") == "not_executed" for r in results),
        "actual_requests": sum(r.get("network_requests", 0) for r in results),
        "executed_cases": sum(bool(r.get("step_results")) for r in results),
        "steps": sum(len(r.get("step_results", [])) for r in results),
        "assertions": sum(len(r.get("assertion_results", [])) for r in results),
        "evidence_incomplete": sum(r.get("evidence_complete") is False for r in results),
        "outcomes": dict(states),
    }


def defects_for_run(
    job_id: str,
    results: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    old = {d.get("fingerprint"): d for d in (previous or {}).get("defects", [])}
    definitions = {c["id"]: c for c in cases}
    updated = dict(old)
    next_id = (
        max(
            (
                int(m[1])
                for d in updated.values()
                if (m := re.fullmatch(r"DEF-(\d+)", str(d.get("id", ""))))
            ),
            default=0,
        )
        + 1
    )
    for result in results:
        if result.get("outcome") != "assertion_failed":
            continue
        case = definitions[result["case_id"]]
        failed: dict[str, Any] = next(
            (s for s in result["step_results"] if s.get("step_id") == result.get("failed_step_id")),
            {},
        )
        signature = fingerprint(
            {
                "design": case.get("design_id") or case.get("rule_refs"),
                "action": failed.get("step"),
                "scenario": case.get("scenario_type"),
            }
        )
        defect = dict(old.get(signature) or {})
        record = {
            "run_id": result["run_id"],
            "case_id": case["id"],
            "case_snapshot": case,
            "timestamp": result["timestamp"],
            "failed_step": failed,
            "evidence": result.get("evidence"),
            "evidence_files": result.get("evidence_files", []),
            "assertion_results": result.get("assertion_results", []),
        }
        history = list(defect.get("history", []))
        if not any(
            h.get("run_id") == result["run_id"] and h.get("event") == "discovered" for h in history
        ):
            history.append(
                {
                    "event": "discovered",
                    "timestamp": result["timestamp"],
                    "run_id": result["run_id"],
                }
            )
        occurrences = list(defect.get("occurrences", []))
        if not any(o.get("run_id") == record["run_id"] for o in occurrences):
            occurrences.append(record)
        defect.update(
            {
                "id": defect.get("id") or f"DEF-{next_id:03d}",
                "fingerprint": signature,
                "case_id": case["id"],
                "interface_id": "UI-E2E-001",
                "requirement_id": case["requirement_id"],
                "case_snapshot": case,
                "execution_snapshot": result,
                "testcase": {
                    "title": case["title"],
                    "expected": case["expected"],
                    "category": case.get("scenario_type", "normal"),
                },
                "title": case["title"],
                "failure_reason": result.get("failure_reason", ""),
                "severity": case.get("priority", "P1"),
                "rule_refs": case.get("rule_refs", []),
                "expected": case["expected"],
                "actual": {"assertion_results": result["assertion_results"], "failed_step": failed},
                "reproduce": {
                    "script_path": "",
                    "role": case.get("role", "default"),
                    "setup": case.get("setup", []),
                    "precondition_checks": case.get("precondition_checks", []),
                    "steps": case["steps"],
                    "cleanup": case.get("cleanup", []),
                },
                "evidence": result.get("evidence", {}),
                "evidence_files": result.get("evidence_files", []),
                "status": "discovered",
                "is_fallback": False,
                "history": history,
                "occurrences": occurrences,
                "triage": {
                    "kind": "product_suspect",
                    "confidence": "medium",
                    "reason": "已执行的业务断言与需求预期不符，复现与证据已保存",
                },
            }
        )
        if signature not in updated:
            next_id += 1
        updated[signature] = defect
    return {"schema_version": "1.0", "job_id": job_id, "defects": list(updated.values())}


def render_html(
    job_id: str, results: list[dict[str, Any]], design: dict[str, Any], defects: dict[str, Any]
) -> str:
    def e(value: Any) -> str:
        return escape(str(value))

    outcome_names = {
        "passed": "通过",
        "assertion_failed": "业务断言失败",
        "environment_failed": "环境失败",
        "automation_error": "自动化问题",
        "blocked": "前置阻断",
        "not_executed": "未执行",
        "running": "执行中",
        "unjudged": "待判定",
    }
    scenario_names = {
        "normal": "正常流程",
        "boundary": "边界场景",
        "exception": "异常分支",
        "permission": "权限",
        "state_transition": "状态流转",
        "consistency": "数据一致性",
    }
    cards = []
    for result in results:
        steps = []
        for step in result.get("step_results", []):
            images = []
            for key, label in [("before_ref", "操作前"), ("after_ref", "操作后")]:
                if step.get(key):
                    path = "../../../../" + e(step[key])
                    images.append(
                        f"<figure><figcaption>{label}</figcaption>"
                        f'<a href="{path}"><img loading="lazy" src="{path}"></a></figure>'
                    )
            opened = "open" if step.get("status") == "failed" else ""
            steps.append(
                f"<details {opened}><summary>{e(step['step_id'])} · "
                f"{e(step['intent'])} · {e(step['status'])}</summary>"
                f"<p>{e(step.get('error', ''))}</p>"
                f"<pre>{e(step.get('assertion') or step.get('actual'))}</pre>"
                f'<div class="shots">{"".join(images)}</div></details>'
            )
        files = "".join(
            f'<li><a href="../../../../{e(path)}">{e(path.rsplit("/", 1)[-1])}</a></li>'
            for path in result.get("evidence_files", [])
        )
        scenario = e(
            scenario_names.get(result.get("scenario_type", ""), result.get("scenario_type"))
        )
        outcome = e(outcome_names.get(result.get("outcome", ""), result.get("outcome")))
        css = e(result.get("scenario_type", "normal"))
        cards.append(
            f'<article class="{css}"><h2>{e(result["case_id"])} · {e(result.get("title"))}</h2>'
            f"<p>{outcome} · {scenario}</p>"
            f"<p>预期：{e(result.get('expected'))}</p>"
            f"<p>{e(result.get('failure_reason', ''))}</p>{''.join(steps)}"
            f"<details><summary>全部证据文件</summary><ul>{files}</ul></details></article>"
        )
    style = (
        "body{font:15px/1.6 system-ui;margin:24px;background:#f5f7fa;color:#172536}"
        "article,header{background:white;padding:20px;margin:16px 0;border-radius:10px}"
        "summary{cursor:pointer;padding:10px;background:#eef2f7}"
        ".shots{display:flex;gap:16px;flex-wrap:wrap}"
        "figure{margin:0;flex:1;min-width:280px}"
        "img{max-width:100%;border:1px solid #ccd4df}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere}"
    )
    counts = summarize(results)
    metrics = "".join(
        f'<span class="metric"><strong>{counts[key]}</strong>{label}</span>'
        for key, label in [
            ("total", "本次计划"),
            ("executed_cases", "已执行"),
            ("passed", "通过"),
            ("failed_assertion", "业务失败"),
            ("blocked", "前置阻断"),
            ("pending", "等待执行"),
            ("unjudged", "待判定"),
            ("failed_environment", "环境/自动化"),
            ("actual_requests", "网络请求"),
        ]
    )
    bugs = []
    for defect in defects.get("defects", []):
        details = json.dumps(
            {k: defect.get(k) for k in ("actual", "reproduce", "history")},
            ensure_ascii=False,
            indent=2,
        )
        bugs.append(
            f"<details><summary>{e(defect['id'])} · {e(defect.get('title'))} · "
            f"{e(defect.get('status'))}</summary><p>预期：{e(defect.get('expected'))}</p>"
            f"<pre>{e(details)}</pre></details>"
        )
    bug_cards = "".join(bugs) or "<p>本批次没有记录业务断言失败。</p>"
    style += (
        ".metric{display:inline-flex;flex-direction:column;padding:12px 20px;"
        "background:#eef2f7;margin:4px;border-radius:8px}.metric strong{font-size:24px}"
        ".normal{border-left:5px solid #22825a}.exception{border-left:5px solid #ca4040}"
        ".boundary{border-left:5px solid #bd861a}"
    )
    return (
        '<!doctype html><html lang="zh"><meta charset="utf-8">'
        f"<title>E2E 测试报告</title><style>{style}</style>"
        f"<header><h1>E2E 测试报告 · {e(job_id)}</h1><div>{metrics}</div>"
        f"<p>需求覆盖：{e(design.get('coverage'))}；"
        f"缺陷 {len(defects.get('defects', []))} 条</p></header>"
        + "".join(cards)
        + f"<article><h2>Bug 记录</h2>{bug_cards}</article></html>"
    )
