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
from common.engine import Engine, JobState
from common.errors import X_ORCH_01, PipelineError
from common.pipeline import NODE_ORDER, NodeStatus

NODE_NAME = "test_report"

_JUDGE_NOTE = "执行通过率口径：环境失败/跳过用例不计入分母（X-RPT-02）；仅状态码断言不等同业务通过。"


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

        report = _render_markdown(state.job_id, execution, interfaces, state)
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


def _render_markdown(
    job_id: str,
    execution: dict[str, Any],
    interfaces: dict[str, Any],
    state: JobState,
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
        f"| 通过 | {summary.get('passed', 0)} |",
        f"| 断言失败（计缺陷） | {summary.get('failed_assertion', 0)} |",
        f"| 环境失败（不计缺陷） | {summary.get('failed_environment', 0)} |",
        f"| 跳过 | {summary.get('skipped', 0)} |",
    ]
    if total > 0 and judged == 0:
        lines.append("| 通过率 | 不输出——无可判读结果（X-RPT-02，全量环境失败/跳过） |")
    elif judged > 0:
        rate = summary.get("passed", 0) / judged
        lines.append(f"| 通过率 | {rate:.1%}（{summary.get('passed', 0)}/{judged} 可判读） |")
    lines += ["", f"{_JUDGE_NOTE}_", ""]
    lines += [
        f"未配置业务响应断言：{len(status_only_rows)} 条。此类用例即使显示通过，也只表示 HTTP 状态符合预期，尚未验证响应字段或业务结果。",
        "",
    ]
    if status_only_rows:
        lines += [
            "### 需补充业务断言的用例",
            "",
            "| 用例 | 需求 | 声明 | 接口 | 执行结果 |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| {r.get('case_id', '')} | {r.get('requirement_id', '—')} | {r.get('claim_id', '—')} | {r.get('interface_id', '')} | {'通过' if r.get('passed') is True else '失败'} |"
            for r in status_only_rows
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
    lines += ["## 6. 环境失败清单（不计缺陷，待环境确认）", ""]
    lines += [_results_table(env_rows) if env_rows else "- 无。", ""]

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


def _results_table(rows: list[dict[str, Any]]) -> str:
    """用例明细表（Markdown）。"""
    if not rows:
        return "- 无。"
    lines = ["| 用例 | 接口 | 结果 | 耗时(ms) | 失败原因 |", "| --- | --- | --- | --- | --- |"]
    for r in rows:
        verdict = "通过" if r.get("passed") is True else "失败"
        reason = str(r.get("failure_reason") or "")
        lines.append(
            f"| {r.get('case_id')} | {r.get('interface_id')} | {verdict} "
            f"| {r.get('duration_ms', 0)} | {reason} |"
        )
    return "\n".join(lines)


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
