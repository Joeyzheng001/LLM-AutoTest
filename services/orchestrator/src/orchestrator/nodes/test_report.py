"""test_report 节点：execution.json → 报告双产物（F-RPT-01~03）。

归属（docs/12 产物布局）：RPT 节点属甲（orchestrator），`test_report.md` 与
`metrics.json` 均甲写。本函数的 `store` 须传入 **orchestrator 身份**。

核心口径：
- F-RPT-02：报告全部数值由 `execution.json` 程序化汇总，**不经 LLM**——
  本节点是流水线中第一个零 LLM 节点（无 Prompt）；
- R-RPT-02：双产物——`test_report.md`（人工阅读）+ `metrics.json`（JSON，
  面向下游与度量）；
- D5 真实/兜底分列：真实执行（REST）与 Mock 执行演示（degraded_protocols）
  分列章节，降级协议由 `interfaces.json` 的协议路由判定；
- F-RPT-03：降级说明章节——`degraded_protocols` 与流水线降级节点（state.degraded）
  如实列出；
- X-RPT-01：`execution.json` 缺失/损坏 → 节点失败，禁止产出空报告；
- X-RPT-02：全量用例均环境失败（无可判读）→ 报告标注"无可判读结果"，
  通过率不输出（而非 0%）；通过率分母 = 可判读用例（环境失败/跳过不计入）；
- X-MTR-01/02：自动化率分母固定 9 不缩减；`metrics.json` 先以报告时点快照落盘，
  流水线收尾后刷新为九节点终态版（docs/13 P-10），`data_complete=false`
  时自动化率为 null（不输出误导数值）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common.artifacts import ArtifactStore
from common.design_coverage import oracle_request_bindings
from common.engine import Engine, JobState
from common.errors import X_ORCH_01, PipelineError
from common.pipeline import NODE_ORDER, NodeStatus
from orchestrator.oracle_diagnostics import build_oracle_diagnostics

NODE_NAME = "test_report"

_JUDGE_NOTE = "执行通过率口径：环境失败、未执行和已请求待判定不计入分母；复用响应的判定计入通过或失败，不重复计数；仅状态码断言不等同业务通过。"


async def run_test_report(engine: Engine, state: JobState, store: ArtifactStore) -> Path:
    """生成报告双产物：test_report.md + metrics.json。

    Args:
        store: orchestrator 身份的产物卷（两产物均甲写）。

    Returns:
        test_report.md 产物路径。

    Raises:
        PipelineError: 前置产物缺失/损坏（X-ORCH-01，含 X-RPT-01 口径）；跳序（X-ORCH-02）。
    """
    engine.check_runnable(state, NODE_NAME)
    engine.mark_running(state, NODE_NAME)
    try:
        execution = _load_json(store, state.job_id, "execution.json")
        interfaces = _load_json(store, state.job_id, "interfaces.json")
        review = _load_optional_json(store, state.job_id, "review_mapping.json")
        testcases = _load_optional_json(store, state.job_id, "testcases.json")

        report = _render_markdown(state.job_id, execution, interfaces, state, review, testcases)
        metrics = _build_metrics(state)
        store.write_raw(state.job_id, "test_report.md", report)
        store.write(state.job_id, "metrics.json", metrics)
        engine.mark_completed(state, NODE_NAME)
        return store.job_dir(state.job_id) / "test_report.md"
    except PipelineError as exc:
        engine.mark_failed(state, NODE_NAME, exc)
        raise


def _load_json(store: ArtifactStore, job_id: str, filename: str) -> dict[str, Any]:
    """读前置产物（缺失/损坏 → X-ORCH-01，X-RPT-01：禁止空报告）。"""
    try:
        return store.read_json(job_id, filename)
    except FileNotFoundError as exc:
        raise PipelineError(
            code=X_ORCH_01,
            message=f"前置产物 {filename} 缺失（X-RPT-01：禁止产出空报告）",
            node_id=NODE_NAME,
        ) from exc
    except json.JSONDecodeError as exc:
        raise PipelineError(
            code=X_ORCH_01,
            message=f"前置产物 {filename} 损坏（X-RPT-01）：{exc}",
            node_id=NODE_NAME,
        ) from exc


def _load_optional_json(store: ArtifactStore, job_id: str, filename: str) -> dict[str, Any]:
    """历史产物可能没有评审/用例告警；报告仍可按执行结果生成。"""
    try:
        payload = store.read_json(job_id, filename)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _render_markdown(
    job_id: str,
    execution: dict[str, Any],
    interfaces: dict[str, Any],
    state: JobState,
    review: dict[str, Any] | None = None,
    testcases: dict[str, Any] | None = None,
) -> str:
    """程序化渲染报告（数值不经 LLM，F-RPT-02）。"""
    results: list[dict[str, Any]] = list(execution.get("results", []))
    summary: dict[str, Any] = dict(execution.get("summary", {}))
    degraded_protocols: list[dict[str, Any]] = list(execution.get("degraded_protocols", []))
    degraded_protocol_names = {str(d.get("protocol")) for d in degraded_protocols}
    protocol_by_interface = {
        str(i.get("id", "")): str(i.get("protocol", "")) for i in interfaces.get("interfaces", [])
    }

    judged = summary.get("passed", 0) + summary.get("failed_assertion", 0)
    total = summary.get("total", 0)
    status_only_rows = [r for r in results if r.get("assertion_scope") == "status_only"]
    structure_rows = [r for r in results if r.get("assertion_scope") == "response_structure"]
    business_passed = sum(r.get("passed") is True and r.get("assertion_scope") == "business" for r in results)
    unverified_rows = [r for r in results if r.get("assertion_scope") == "unverified"]
    lines: list[str] = [
        f"# 测试报告（{job_id}）",
        "",
        "> 数值来源：`execution.json` 程序化汇总，未经 LLM（F-RPT-02）。",
        "> 接口通过口径：HTTP 状态码 + 可机器判定的响应断言；未配置响应断言的用例仅校验状态码，不代表业务结果已验证。",
        "",
        "## 1. 执行汇总",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 总用例 | {total} |",
        f"| 断言通过（含状态/结构检查） | {summary.get('passed', 0)} |",
        f"| 其中业务预期通过 | {business_passed} |",
        f"| 断言失败（计缺陷） | {summary.get('failed_assertion', 0)} |",
        f"| 环境失败（不计缺陷） | {summary.get('failed_environment', 0)} |",
        f"| 实际发送请求 | {summary.get('actual_requests', sum((r.get('request') or {}).get('attempted') is True for r in results))} |",
        f"| 已请求待判定 | {summary.get('observed_unjudged', 0)} |",
        f"| 复用实际响应核对 | {summary.get('reused_response', 0)} |",
        f"| 未执行 | {summary.get('not_executed', summary.get('skipped', 0))} |",
    ]
    if total > 0 and judged == 0:
        lines.append("| 通过率 | 不输出——无可判读结果（X-RPT-02，全量环境失败/跳过） |")
    elif judged > 0:
        rate = summary.get("passed", 0) / judged
        lines.append(f"| 通过率 | {rate:.1%}（{summary.get('passed', 0)}/{judged} 可判读） |")
    lines += ["", "用例总数 = 断言通过 + 断言失败 + 环境失败 + 已请求待判定 + 未执行。"
              "复用响应数量已包含在相应判定中，实际请求次数单独统计。", "", _JUDGE_NOTE, ""]
    lines += [
        f"未配置业务响应断言：{len(status_only_rows)} 条。此类用例即使显示通过，也只表示 HTTP 状态符合预期，尚未验证响应字段或业务结果。",
        f"仅响应结构检查：{len(structure_rows)} 条。编号回显、字段存在性不能证明通道选择、结果组装等业务规则通过。",
        f"缺少源码确认预期、未发起请求：{len(unverified_rows)} 条。此类用例不计入通过率，也不会进入缺陷判定。",
        "",
    ]
    framework_observations = [row for row in results if row.get("skipped_reason") == "框架响应待确认"]
    if framework_observations:
        lines += ["### 框架边界响应待确认", "",
                  "以下请求已发送并保留实际响应；框架默认状态与部署环境的异常包装不同，暂不计为产品缺陷，也不按实际响应反写预期。", "",
                  "| 用例 | 接口 | 参考预期 | 实际 HTTP |", "| --- | --- | --- | --- |"]
        lines += [f"| {row.get('case_id', '')} | {row.get('interface_id', '')} | {row.get('expected', '')} | {row.get('actual_status', '')} |" for row in framework_observations]
        lines.append("")
    pending = [row for row in results if row.get("execution_state") == "observed_unjudged"
               and row.get("skipped_reason") != "框架响应待确认"]
    if pending:
        lines += ["### 已请求、尚不能判定目标业务预期", "", _results_table(pending), ""]
    reused = [row for row in results if row.get("execution_state") == "reused_response"]
    if reused:
        lines += ["### 复用真实响应的条件场景", "",
                  "仅在源码可映射的返回字段和前序路径条件均满足时，核对该场景原有断言；不新增请求，不反写预期，不代表内部调用已被跟踪。", "",
                  "| 场景 | 响应来自 | 断言结果 |", "| --- | --- | --- |"]
        lines += [f"| {row.get('case_id')} | {row.get('source_case_id')} | {'通过' if row.get('passed') else '失败'} |" for row in reused]
        lines.append("")
    designed_cases = [case for case in (testcases or {}).get("cases", []) if isinstance(case, dict)]
    source_designs = [oracle for oracle in (review or {}).get("test_oracles", []) if isinstance(oracle, dict)]
    bound_oracles, missing_request_ids = oracle_request_bindings(source_designs, designed_cases)
    result_by_case = {str(result.get("case_id", "")): result for result in results}
    if testcases is not None:
        lines += [
            "### 正常场景验证情况", "",
            "用例设计、实际执行与通过分别统计；响应契约通过不等于已证明内部路径覆盖。", "",
            "| 接口 | 正常用例设计 | 实际执行 | 业务预期通过 | 未执行 |",
            "| --- | --- | --- | --- | --- |",
        ]
        normal_gaps: list[str] = []
        for interface in interfaces.get("interfaces", []):
            interface_id = str(interface.get("id", ""))
            normal = [case for case in designed_cases
                      if case.get("interface_id") == interface_id and case.get("category") == "normal"]
            normal_results = [result_by_case.get(str(case.get("id", "")), {}) for case in normal]
            executed = [result for result in normal_results if result and (
                (result.get("request") or {}).get("attempted") is True
                or result.get("execution_state") == "reused_response"
                or (not result.get("skipped_reason") and result.get("actual_status") is not None)
            )]
            passed = sum(result.get("passed") is True and result.get("assertion_scope") == "business" for result in executed)
            lines.append(f"| {interface_id} | {len(normal)} | {len(executed)} | {passed} | {len(normal) - len(executed)} |")
            if passed == 0:
                normal_gaps.append(interface_id)
        if normal_gaps:
            lines += ["", "**正常业务验证未完成**：" + "、".join(normal_gaps)
                      + " 没有通过的正常场景；整体执行通过率不能作为这些接口的业务验收结论。"]
        stateful = [case for case in designed_cases
                    if case.get("state_conditions") or case.get("execution_blockers")]
        if stateful:
            lines += ["", "### 已设计用例的执行条件", ""]
            for case in stateful:
                conditions = [str(item.get("description", "")) for item in case.get("state_conditions", [])]
                blockers = [str(item) for item in case.get("execution_blockers", [])]
                result = result_by_case.get(str(case.get("id", "")), {})
                outcome = _result_verdict(result) if result else (
                    "已执行，通过" if result.get("passed") is True else
                    "已执行，未通过" if result else "未执行"
                )
                lines.append(f"- {case.get('id')} {case.get('title')}：条件：{'；'.join(conditions + blockers)}；结果：{outcome}")
        lines.append("")
    case_fixtures = [
        item for item in execution.get("fixtures", [])
        if isinstance(item, dict) and item.get("phase") == "case"
    ]
    if case_fixtures:
        cleaned = sum(item.get("cleanup_status") == "soft_deleted" for item in case_fixtures)
        not_created = sum(item.get("cleanup_status") == "not_created" for item in case_fixtures)
        lines += [
            "### 测试记录清理",
            "",
            f"本次为 {len(case_fixtures)} 条用例尝试自动造数；已确认软删除 {cleaned} 条，"
            f"未创建记录、无需清理 {not_created} 条。",
            "软删除会保留数据库记录和操作日志。",
            "",
        ]
        for item in case_fixtures:
            if item.get("cleanup_status") not in {"soft_deleted", "not_created"}:
                lines.append(
                    f"- {item.get('case_id', '?')} 清理未确认："
                    f"{item.get('cleanup_detail') or item.get('failure_reason') or '状态未知'}；"
                    f"标记 {item.get('marker', '?')}。"
                )
        if cleaned + not_created != len(case_fixtures):
            lines.append("")
    review_warnings = [
        str(item) for item in (review or {}).get("test_oracle_warnings", [])
        if isinstance(item, str)
    ]
    generation_warnings = [
        str(item) for item in (testcases or {}).get("warnings", [])
        if isinstance(item, str) and item.startswith(("P0-oracle", "P0-trace", "X-GEN-02"))
    ]
    if source_designs:
        lines += ["### 源码场景与请求设计", "",
                  f"源码场景 {len(source_designs)} 个，已绑定有效请求设计 {len(bound_oracles)} 个，"
                  f"待补有效请求 {len(missing_request_ids)} 个。该统计不表示环境条件已满足或请求已经执行。", ""]
        if missing_request_ids:
            lines += ["待补场景：" + "、".join(sorted(missing_request_ids)), ""]
    # Historical artifacts retain pre-recovery warnings. Report the final binding
    # outcome without rewriting the original generation artifact.
    if source_designs and not missing_request_ids:
        generation_warnings = [
            f"生成修复：初轮候选存在预期绑定问题；最终 {len(bound_oracles)}/{len(source_designs)} 个源码场景"
            "均已绑定有效请求设计，执行条件另行统计。"
            if "候选未绑定适用预期" in warning else warning
            for warning in generation_warnings
        ]
    requirement_points = [point for point in (testcases or {}).get("testpoints", [])
                          if isinstance(point, dict) and point.get("source") == "REQ"]
    if requirement_points:
        requirement_claims = {str(point["claim_id"]) for point in requirement_points if point.get("claim_id")}
        lines += ["### 需求测试点口径", "",
                  f"{len(requirement_claims)} 条需求声明按接口展开为 {len(requirement_points)} 个需求测试点。"
                  "需求测试点需有可核验的业务断言及用例关联；HTTP/结构检查不能代替需求业务验证，"
                  "测试点缺口不能直接换算成缺少同等数量的请求。", ""]
    if review_warnings or generation_warnings:
        lines += [
            "### 评审、生成过程与覆盖缺口说明",
            "",
            "以下记录包含候选校验、修复过程和覆盖缺口。是否仍阻断执行，应结合最终源码场景绑定和执行明细判断。",
            "",
        ]
        diagnostics = build_oracle_diagnostics(review_warnings, source_designs)
        labels = {
            "candidate_invalid": "本轮未形成有效预期",
            "control_required": "运行控制条件未满足",
            "execution_pending": "异常触发或数据条件未满足",
            "response_evidence_missing": "响应预期尚未确认",
            "framework_boundary": "HTTP 框架边界",
            "resolved": "系统已处理",
            "replaced": "系统已替代重复候选",
        }
        if diagnostics:
            for item in diagnostics:
                lines.append(
                    f"- Code Review [{labels.get(str(item.get('status')), '待核对')}]："
                    f"{item.get('summary') or item.get('message', '')}；系统处理结果：{item.get('action', '')}"
                )
        else:
            lines += [f"- Code Review：{warning}" for warning in review_warnings]
        lines += [f"- 用例生成：{warning}" for warning in generation_warnings]
        lines.append("")
    if status_only_rows:
        lines += [
            "### 需补充业务断言的用例",
            "",
            "| 用例 | 需求 | 声明 | 接口 | 执行结果 |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| {r.get('case_id', '')} | {r.get('requirement_id', '—')} | {r.get('claim_id', '—')} | {r.get('interface_id', '')} | {_result_verdict(r)} |"
            for r in status_only_rows
        ]
        lines.append("")

    if unverified_rows:
        lines += [
            "### 待补源码预期的用例",
            "",
            "| 用例 | 需求 | 接口 | 原因 |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| {r.get('case_id', '')} | {r.get('requirement_id', '—')} | {r.get('interface_id', '')} | {r.get('failure_reason', '缺少源码 oracle')} |"
            for r in unverified_rows
        ]
        lines.append("")

    # D5 真实/兜底分列：按接口协议路由判定
    real_rows = [
        r
        for r in results
        if protocol_by_interface.get(str(r.get("interface_id", "")), "")
        not in degraded_protocol_names
    ]
    demo_rows = [
        r
        for r in results
        if protocol_by_interface.get(str(r.get("interface_id", "")), "")
        in degraded_protocol_names
    ]

    lines += ["## 2. 真实执行明细", "", _results_table(real_rows), ""]
    lines += [
        "## 3. Mock 执行演示明细（兜底，非真实执行结果）",
        "",
        "> 以下协议 Runner 未就绪，降级为「脚本生成成功 + Mock 执行演示」（X-EXEC-03）。",
        "> 演示通过仅代表链路跑通，**不得作为真实质量结论**。",
        "",
        _results_table(demo_rows),
        "",
    ]

    # F-RPT-03：降级说明章节（协议降级 + 流水线降级节点，如实分列）
    lines += ["## 4. 降级说明（F-RPT-03）", ""]
    if degraded_protocols:
        lines += ["| 协议 | 降级模式 | 原因 |", "| --- | --- | --- |"]
        lines += [
            f"| {d.get('protocol')} | {d.get('mode')} | {d.get('reason', '')} |"
            for d in degraded_protocols
        ]
    else:
        lines.append("- 无协议降级。")
    pipeline_degraded = list(getattr(state, "degraded", []))
    if pipeline_degraded:
        lines += ["", "流水线降级节点（D1：分母不缩减，视同已处理不阻塞下游）："]
        lines += [f"- `{d.get('node')}`：{d.get('reason')}" for d in pipeline_degraded]
    lines.append("")

    # 失败明细（断言失败计缺陷口径；环境失败隔离）
    assertion_rows = [r for r in results if r.get("failure_kind") == "assertion"]
    env_rows = [r for r in results if r.get("failure_kind") == "environment"]
    lines += ["## 5. 失败明细（断言失败，进缺陷清单口径）", ""]
    lines += [_results_table(assertion_rows) if assertion_rows else "- 无。", ""]
    lines += _failure_request_details(assertion_rows)
    lines += ["## 6. 环境失败清单（不计缺陷，待环境确认）", ""]
    lines += [_results_table(env_rows) if env_rows else "- 无。", ""]
    lines += _failure_request_details(env_rows)

    # X-MTR：自动化率快照说明（收尾后 metrics.json 刷新为终态版，docs/13 P-10）
    lines += [
        "## 7. 自动化率快照",
        "",
        "> 本报告生成时 `metrics.json` 为报告时点快照：未到达的下游节点（缺陷清单/回归沉淀）",
        "> 按人工介入记录且数据不完整（`data_complete=false`），自动化率为 null，",
        "> 不输出误导数值（X-MTR-02）。分母固定 9 不缩减（X-MTR-01）。",
        "> 流水线收尾后 `metrics.json` 刷新为九节点终态版（最终自动化率以终态版为准）。",
        "",
    ]
    return "\n".join(lines) + "\n"


def _result_verdict(result: dict[str, Any]) -> str:
    if result.get("execution_state") == "reused_response":
        return f"复用 {result.get('source_case_id')}：{'通过' if result.get('passed') else '失败'}"
    if result.get("execution_state") == "observed_unjudged" or (
        result.get("skipped_reason") and (result.get("request") or {}).get("attempted") is True
    ):
        return "已请求，" + str(result.get("skipped_reason") or "待判定")
    if result.get("skipped_reason") or result.get("skipped"):
        return "未执行"
    return "通过" if result.get("passed") is True else "失败"


def _results_table(rows: list[dict[str, Any]]) -> str:
    """用例明细表（Markdown）。"""
    if not rows:
        return "- 无。"
    lines = ["| 用例 | 接口 | 结果 | 耗时(ms) | 失败原因 |", "| --- | --- | --- | --- | --- |"]
    for r in rows:
        verdict = _result_verdict(r)
        reason = str(r.get("failure_reason") or "")
        lines.append(
            f"| {r.get('case_id')} | {r.get('interface_id')} | {verdict} "
            f"| {r.get('duration_ms', 0)} | {reason} |"
        )
    return "\n".join(lines)


def _failure_request_details(rows: list[dict[str, Any]]) -> list[str]:
    """Append sanitized request/response payloads below failure summary tables."""
    detailed = [row for row in rows if isinstance(row.get("request"), dict)]
    if not detailed:
        return []
    lines = ["### 请求与响应详情", ""]
    for row in detailed:
        request = row["request"]
        method = request.get("method") or "HTTP"
        url = request.get("url") or "URL 未记录"
        sent = "已发送" if request.get("attempted") is True else "未发送（请求预览）"
        lines += [
            f"#### {row.get('case_id', '未知用例')} · {row.get('interface_id', '未知接口')}",
            "",
            f"- 请求：{method} {url}",
            f"- 状态：{sent}",
        ]
        for key, label in (
            ("query_params", "查询参数"),
            ("body", "请求体"),
            ("headers", "请求头（敏感值已脱敏）"),
        ):
            if key not in request or request[key] is None:
                continue
            lines += ["", f"**{label}**", "", "```json"]
            rendered = json.dumps(request[key], ensure_ascii=False, indent=2, default=str)
            lines.extend(rendered.replace("`", "\\u0060").splitlines())
            lines.append("```")
        if row.get("actual_status") is not None or "actual_body" in row:
            lines += ["", f"**实际响应**（HTTP {row.get('actual_status', '无响应')}）", ""]
            if "actual_body" in row:
                lines.append("```json")
                rendered = json.dumps(row["actual_body"], ensure_ascii=False, indent=2, default=str)
                lines.extend(rendered.replace("`", "\\u0060").splitlines())
                lines.append("```")
        lines.append("")
    return lines


def build_final_metrics(state: JobState) -> dict[str, Any]:
    """构造 metrics.json 终态版（流水线收尾刷新，docs/13 P-10）。

    与报告时点快照同源同口径，但全部节点按 state 终态计算：
    收尾后 defect_list/retest_regression 的终态不再被「未到达」深写覆盖，
    完整跑完的 job 自动化率不再停在报告时点值。
    """
    return _build_metrics(state, final=True)


def _build_metrics(state: JobState, final: bool = False) -> dict[str, Any]:
    """构造 metrics.json（程序化，X-MTR-01/02 口径）。

    快照口径（final=False，test_report 节点内）：已完成节点标 completed；降级节点
    （state.degraded）标 manual_intervention + degraded=true（D1 按人工介入计入）；
    本节点（test_report）产物即将落盘，按 completed 记；未到达的下游节点标
    manual_intervention + 未执行说明。
    终态口径（final=True，流水线收尾）：全部节点按 state 终态，无本节点特判。
    任一节点未真正完成 → data_complete=false → 通过率 null。
    """
    degraded_by_node = {str(d.get("node")): str(d.get("reason", "")) for d in state.degraded}
    nodes: list[dict[str, Any]] = []
    completed_count = 0
    for name in NODE_ORDER:
        status = state.node(name).status
        entry: dict[str, Any]
        if status is NodeStatus.COMPLETED:
            entry = {"name": name, "status": "completed"}
            completed_count += 1
        elif name in degraded_by_node:
            entry = {
                "name": name,
                "status": "manual_intervention",
                "degraded": True,
                "degrade_reason": degraded_by_node[name],
            }
        elif not final and name == NODE_NAME:
            entry = {"name": name, "status": "completed"}
            completed_count += 1
        else:
            entry = {
                "name": name,
                "status": "manual_intervention",
                "intervention_reason": "未执行（流水线未到达该节点）",
            }
        nodes.append(entry)

    data_complete = completed_count == len(NODE_ORDER)
    rate: float | None = (completed_count / len(NODE_ORDER)) if data_complete else None
    return {
        "schema_version": "1.0",
        "job_id": state.job_id,
        "automation_rate": {
            "value": rate,
            "numerator": completed_count,
            "denominator": len(NODE_ORDER),
            "data_complete": data_complete,
        },
        "nodes": nodes,
    }
