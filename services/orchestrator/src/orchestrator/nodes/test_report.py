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
from urllib.parse import urlsplit, urlunsplit

from common.artifacts import ArtifactStore
from common.design_coverage import oracle_request_bindings
from common.engine import Engine, JobState
from common.errors import X_ORCH_01, PipelineError
from common.pipeline import NODE_ORDER, NodeStatus
from orchestrator.oracle_diagnostics import build_oracle_diagnostics

NODE_NAME = "test_report"

_JUDGE_NOTE = "断言符合率口径：环境失败、未执行和已请求待判定不计入分母；复用响应的判定计入符合或失败，不重复计数；仅状态码断言不等同业务通过。"


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
        job_input = _load_optional_json(store, state.job_id, "job_input.json")

        report = _render_markdown(state.job_id, execution, interfaces, state, review, testcases, job_input)
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
    job_input: dict[str, Any] | None = None,
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
    direct_judged = [r for r in results if (r.get("request") or {}).get("attempted") is True
                     and (r.get("passed") is True or r.get("failure_kind") == "assertion")]
    direct_passed = sum(r.get("passed") is True for r in direct_judged)
    total = summary.get("total", 0)
    status_only_rows = [r for r in results if r.get("assertion_scope") == "status_only"]
    structure_rows = [r for r in results if r.get("assertion_scope") == "response_structure"]
    business_passed = sum(r.get("passed") is True and r.get("category") == "normal"
                          and r.get("assertion_scope") == "business" for r in results)
    weak_check_errors = [r for r in results if r.get("passed") is True
                         and r.get("execution_state") != "reused_response"
                         and r.get("assertion_scope") in {"status_only", "response_structure"}
                         and _observed_business_errors(r.get("actual_body"))]
    environment_contract_rows = [r for r in results
                                 if r.get("expectation_source") == "user_confirmed_environment"
                                 and (r.get("passed") is True or r.get("failure_kind") == "assertion")]
    dependency_contract_rows = [r for r in results
                                if r.get("expectation_source") == "local_dependency_bytecode"
                                and r.get("passed") is True]
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
        f"| 断言符合（含状态/结构检查与复用响应） | {summary.get('passed', 0)} |",
        f"| 其中直接请求断言符合 | {direct_passed} |",
        f"| 其中正常场景业务字段断言符合（含复用响应） | {business_passed} |",
        f"| 按用户确认的环境响应契约判定 | {len(environment_contract_rows)} |",
        f"| 按本地依赖字节码契约判定符合 | {len(dependency_contract_rows)} |",
        f"| 断言失败（计缺陷） | {summary.get('failed_assertion', 0)} |",
        f"| 环境失败（不计缺陷） | {summary.get('failed_environment', 0)} |",
        f"| 实际发送请求 | {summary.get('actual_requests', sum((r.get('request') or {}).get('attempted') is True for r in results))} |",
        f"| 已请求待判定 | {summary.get('observed_unjudged', 0)} |",
        f"| 复用实际响应核对 | {summary.get('reused_response', 0)} |",
        f"| 未执行 | {summary.get('not_executed', summary.get('skipped', 0))} |",
        f"| 断言判读覆盖率 | {judged}/{total}（{judged / total:.1%}） |" if total else "| 断言判读覆盖率 | 0/0 |",
        f"| 直接请求断言符合率 | {direct_passed}/{len(direct_judged)}（{direct_passed / len(direct_judged):.1%}） |"
        if direct_judged else "| 直接请求断言符合率 | 不输出——无可判读的直接请求 |",
    ]
    if total > 0 and judged == 0:
        lines.append("| 断言符合率（含复用） | 不输出——无可判读结果（X-RPT-02，全量环境失败/跳过） |")
    elif judged > 0:
        rate = summary.get("passed", 0) / judged
        lines.append(f"| 断言符合率（含复用） | {rate:.1%}（{summary.get('passed', 0)}/{judged} 可判读） |")
    lines += ["", "用例总数 = 断言符合 + 断言失败 + 环境失败 + 已请求待判定 + 未执行。"
              "复用响应数量已包含在相应判定中，实际请求次数单独统计。", "", _JUDGE_NOTE, ""]
    unit_coverage = execution.get("unit_test_coverage")
    if isinstance(unit_coverage, dict):
        unit_rows = [item for repo in unit_coverage.get("repositories") or []
                     for item in repo.get("cases") or []]
        lines += [
            "### 隔离单元测试与 JaCoCo 覆盖率", "",
            "单元测试验证隔离源码副本中的内部逻辑；其通过数不计入接口请求通过数。",
            f"候选用例：{unit_coverage.get('cases_total', 0)}；"
            f"单测通过：{unit_coverage.get('cases_passed', 0)}；"
            f"其余：{unit_coverage.get('cases_failed', 0)}。", "",
        ]
        if unit_coverage.get("cases_deferred"):
            lines += [f"其中 {unit_coverage['cases_deferred']} 条因模型连接失败暂缓生成或修复，"
                      "不能计为源码断言失败。", ""]
        if unit_coverage.get("reason"):
            lines += [f"单测说明：{unit_coverage['reason']}", ""]
        existing = [(repo, suite) for repo in unit_coverage.get("repositories") or []
                    if isinstance((suite := repo.get("existing_suite")), dict)]
        if existing:
            lines += ["#### 项目现有单测基线（与平台生成单测分开）", "",
                      "现有测试按目标提交创建隔离副本运行；工作区未提交的测试文件和 pom 改动另行标注，不属于该提交。", "",
                      "| 仓库 / 模块 | 运行 | 失败 | 来源 | 状态 |",
                      "| --- | ---: | ---: | --- | --- |"]
            for repo, suite in existing:
                modules = suite.get("modules") or []
                if not modules:
                    lines.append(f"| {repo.get('repo', '')} | 0 | 0 | — | {suite.get('reason', suite.get('status', ''))} |")
                for module in modules:
                    uncommitted = (module.get("pom_provenance") == "working_tree_uncommitted"
                                   or any(test.get("provenance") == "working_tree_uncommitted"
                                          for test in module.get("tests") or []))
                    lines.append(
                        f"| {repo.get('repo', '')} / {module.get('module', '')} | "
                        f"{module.get('tests_executed', 0)} | {module.get('tests_failed', 0)} | "
                        f"{'含未提交工作区文件' if uncommitted else '目标提交'} | "
                        f"{module.get('status', '')} |"
                    )
            existing_classes = [
                (repo, module, target)
                for repo, suite in existing
                for module in suite.get("modules") or []
                for target in module.get("classes") or []
            ]
            if existing_classes:
                lines += ["", "| 现有测试覆盖目标类 | 行覆盖 | 分支覆盖 |",
                          "| --- | --- | --- |"]
                def existing_counter(value: dict | None) -> str:
                    if not value:
                        return "无数据"
                    covered = int(value.get("covered", 0))
                    total = covered + int(value.get("missed", 0))
                    return f"{covered}/{total}（{covered / total:.1%}）" if total else "无数据"
                for _, _, target in existing_classes:
                    lines.append(f"| {target.get('name', '')} | "
                                 f"{existing_counter(target.get('line_coverage'))} | "
                                 f"{existing_counter(target.get('branch_coverage'))} |")
            lines += ["", "现有单测的通过数和覆盖率不计入下方平台生成用例统计。", ""]
        class_rows = [
            (repo, module, target)
            for repo in unit_coverage.get("repositories") or []
            for module in (repo.get("suite_coverage") or {}).get("modules") or []
            for target in module.get("classes") or []
        ]
        if class_rows:
            lines += ["#### 目标类覆盖率（本轮生成单测合跑）", "",
                      "| 目标类 | 行覆盖 | 分支覆盖 | 合跑状态 |",
                      "| --- | --- | --- | --- |"]
            def coverage_cell(counter: dict | None) -> str:
                if not counter:
                    return "无数据"
                covered = int(counter.get("covered", 0))
                missed = int(counter.get("missed", 0))
                total_lines = covered + missed
                return f"{covered}/{total_lines}（{covered / total_lines:.1%}）" if total_lines else "无数据"
            for repo, module, target in sorted(
                class_rows, key=lambda row: (row[2].get("line_coverage") or {}).get("rate") or 0,
            ):
                lines.append(
                    f"| {target.get('name', '')} | {coverage_cell(target.get('line_coverage'))} | "
                    f"{coverage_cell(target.get('branch_coverage'))} | {module.get('status', '')} |"
                )
            lines += ["", "这里的覆盖率只统计列出的目标类；单条用例的模块级 JaCoCo 记录不作为目标类覆盖率。", ""]
        else:
            lines += ["本轮未产出可核验的目标类 JaCoCo 覆盖率。", ""]
        linked: dict[str, list[dict]] = {}
        for item in unit_rows:
            for case_id in item.get("linked_interface_case_ids") or []:
                linked.setdefault(str(case_id), []).append(item)
        if linked:
            verified = sum(any(row.get("status") == "passed" for row in items)
                           for items in linked.values())
            lines += ["#### 单测补充的接口测试缺口", "",
                      f"{verified}/{len(linked)} 个关联接口用例已有通过的补充单测；"
                      "单测结论不替代 HTTP 执行结论。接口未执行的关联项优先展示。", ""]
            result_by_case = {str(item.get("case_id")): item for item in results}
            groups = [
                ("not_executed", "接口测试未执行"),
                ("observed_unjudged", "接口已请求，结果待判定"),
                ("executed_failed", "接口已执行，未通过"),
                ("reused_response", "接口复用历史响应"),
                ("executed_passed", "接口已执行并通过"),
                ("no_record", "本轮无接口结果"),
            ]
            def gap_category(case_id: str) -> str:
                result = result_by_case.get(case_id)
                if result is None:
                    return "no_record"
                state = result.get("execution_state")
                if state in ("not_executed", "observed_unjudged", "reused_response"):
                    return str(state)
                return "executed_passed" if result.get("passed") else "executed_failed"
            for category, title in groups:
                items_in_group = [(case_id, items) for case_id, items in linked.items()
                                  if gap_category(case_id) == category]
                if not items_in_group:
                    continue
                items_in_group.sort(key=lambda entry: (
                    not any(item.get("status") == "passed" for item in entry[1]), entry[0]
                ))
                group_passed = sum(any(item.get("status") == "passed" for item in items)
                                   for _, items in items_in_group)
                lines += [f"##### {title}（{len(items_in_group)} 条，{group_passed} 条已有通过单测）", "",
                          "| 接口用例 | 补缺口状态 | 未执行原因 | 关联单测 |",
                          "| --- | --- | --- | --- |"]
                for case_id, items in items_in_group:
                    passed = any(item.get("status") == "passed" for item in items)
                    refs = "、".join(f"{item.get('case_id', '')}（{item.get('status', '')}）"
                                   for item in items)
                    result = result_by_case.get(case_id) or {}
                    blocker = ("；".join(map(str, result.get("execution_blockers") or []))
                               or str(result.get("skipped_reason") or "")) if category == "not_executed" else "—"
                    lines.append(f"| {case_id} | {'已有通过单测' if passed else '尚未补齐'} | "
                                 f"{blocker.replace('|', '｜')} | {refs} |")
                lines.append("")
        inconsistent = [item for item in unit_rows if item.get("consistency_warning")]
        if inconsistent:
            lines += ["#### 逐条与合跑结果不一致", "",
                      "以下用例需要复跑核对；保留逐条结果，不因合跑通过而改写。", "",
                      "| 用例 | 逐条结果 | 合跑结果 |",
                      "| --- | --- | --- |"]
            lines += [f"| {item.get('case_id', '')} | {item.get('status', '')} | {item.get('suite_status', '')} |"
                      for item in inconsistent]
            lines.append("")
        if unit_rows:
            lines += ["#### 单测明细", "",
                      "| 用例 | 单测结果 | 说明 |", "| --- | --- | --- |"]
            for item in unit_rows:
                lines.append(
                    f"| {item.get('case_id', '')} | {item.get('status', '')} | "
                    f"{str(item.get('reason', ''))[:180].replace('|', '｜')} |"
                )
            lines.append("")
    if any(r.get("expectation_source") == "local_dependency_bytecode" for r in results):
        lines += [
            "本地依赖字节码契约已核对 Maven 缓存 JAR 与自动配置导入链；"
            "部署 JAR 摘要尚未核对。符合仅表示实际响应匹配该依赖契约，"
            "不符先列为版本/环境待确认，不直接计产品缺陷。通用异常响应本身不能证明具体抛错点已命中。",
            "",
        ]
    lines += [
        f"未配置业务响应断言：{len(status_only_rows)} 条。此类用例即使显示通过，也只表示 HTTP 状态符合预期，尚未验证响应字段或业务结果。",
        f"其中 {len(weak_check_errors)} 条状态或结构检查虽符合，但响应含明确的业务错误码；需补成功条件或按入口探针展示。",
        f"仅响应结构检查：{len(structure_rows)} 条。编号回显、字段存在性不能证明通道选择、结果组装等业务规则通过。",
        f"缺少源码确认预期、未发起请求：{len(unverified_rows)} 条。此类用例不计入通过率，也不会进入缺陷判定。",
        "",
    ]
    if weak_check_errors:
        lines += ["### 状态或结构检查符合，但响应含业务错误", "",
                  "以下仅是响应诊断，不自动改写已有断言结论；需要回到 Code Review 核对执行器支持范围和成功条件。", "",
                  "| 用例 | 接口 | 观察到的错误码 |", "| --- | --- | --- |"]
        lines += [f"| {row.get('case_id', '')} | {row.get('interface_id', '')} | {', '.join(_observed_business_errors(row.get('actual_body')))} |"
                  for row in weak_check_errors]
        lines.append("")
    if environment_contract_rows:
        lines += ["### 用户确认的环境响应契约", "",
                  "这些用例按当前任务配置的 HTTP 状态、returnCode 和 body 判定。该契约只证明响应符合当前环境约定；通用错误码本身不能定位拒绝发生在绑定阶段。", "",
                  _results_table(environment_contract_rows), ""]
    if job_input:
        repo_urls = {
            str(repo.get("name", "")): _safe_route_base(str(repo.get("service_base_url") or ""))
            for repo in job_input.get("repos") or [] if isinstance(repo, dict)
        }
        default_url = _safe_route_base(str(job_input.get("rest_url") or ""))
        route_rows = []
        for interface in interfaces.get("interfaces") or []:
            if interface.get("protocol") != "rest":
                continue
            repo = str((interface.get("code_location") or {}).get("repo", ""))
            base = repo_urls.get(repo) or default_url
            path = str((interface.get("signature") or {}).get("path", ""))
            route_rows.append((str(interface.get("id", "")), repo, base + "/" + path.lstrip("/"),
                               "仓库地址" if repo_urls.get(repo) else "任务地址回退"))
        if route_rows:
            lines += ["### 接口实际路由配置", "", "| 接口 | 仓库 | 目标 URL 模板 | 地址来源 |",
                      "| --- | --- | --- | --- |"]
            lines += [f"| {if_id} | {repo} | {url} | {source} |"
                      for if_id, repo, url, source in route_rows]
            lines.append("")
    framework_observations = [row for row in results if row.get("skipped_reason") == "框架响应待确认"]
    if framework_observations:
        lines += ["### 框架边界响应待确认", "",
                  "以下请求已发送并保留实际响应；必填请求体的部署响应契约尚未确认，暂不计为产品缺陷，也不按实际响应反写预期。", "",
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
                  "| 场景 | 响应来自 | 字段核对 |", "| --- | --- | --- |"]
        lines += [f"| {row.get('case_id')} | {row.get('source_case_id')} | {'符合' if row.get('passed') else '不符合'} |" for row in reused]
        lines.append("")
    designed_cases = [case for case in (testcases or {}).get("cases", []) if isinstance(case, dict)]
    source_designs = [oracle for oracle in (review or {}).get("test_oracles", []) if isinstance(oracle, dict)]
    bound_oracles, missing_request_ids = oracle_request_bindings(source_designs, designed_cases)
    result_by_case = {str(result.get("case_id", "")): result for result in results}
    if testcases is not None:
        lines += [
            "### 正常场景验证情况", "",
            "直接请求、复用响应和业务字段断言分别统计；字段符合不代表内部调用已被跟踪。", "",
            "| 接口 | 正常用例设计 | 直接请求 | 复用响应 | 直接请求业务断言符合 | 复用字段符合 | 未执行 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        normal_gaps: list[str] = []
        for interface in interfaces.get("interfaces", []):
            interface_id = str(interface.get("id", ""))
            normal = [case for case in designed_cases
                      if case.get("interface_id") == interface_id and case.get("category") == "normal"]
            normal_results = [result_by_case.get(str(case.get("id", "")), {}) for case in normal]
            direct = [result for result in normal_results if result and
                      result.get("execution_state") != "reused_response" and
                      ((result.get("request") or {}).get("attempted") is True
                       or (not result.get("skipped_reason") and result.get("actual_status") is not None))]
            reused_normal = [result for result in normal_results if result and
                             result.get("execution_state") == "reused_response"]
            direct_business = sum(result.get("passed") is True and result.get("assertion_scope") == "business"
                                  for result in direct)
            reused_matched = sum(result.get("passed") is True for result in reused_normal)
            not_executed = len(normal) - len(direct) - len(reused_normal)
            lines.append(f"| {interface_id} | {len(normal)} | {len(direct)} | {len(reused_normal)} | "
                         f"{direct_business} | {reused_matched} | {not_executed} |")
            if direct_business == 0:
                normal_gaps.append(interface_id)
        if normal_gaps:
            lines += ["", "**正常业务验证未完成**：" + "、".join(normal_gaps)
                      + " 没有直接请求且业务字段断言符合的正常场景；整体断言符合率不能作为这些接口的业务验收结论。"]
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
                  f"最终准入的源码预期 {len(source_designs)} 个，已绑定请求设计 {len(bound_oracles)} 个，"
                  f"待补请求设计 {len(missing_request_ids)} 个。该统计不表示环境条件已满足或请求已经执行；"
                  "静态分支候选和被隔离的预期不在此分母内，需结合评审诊断核对。", ""]
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
            "environment_pending": "设计已保留，部署配置待验证",
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
        return f"复用 {result.get('source_case_id')}：响应字段{'符合' if result.get('passed') else '不符合'}"
    if result.get("execution_state") == "observed_unjudged" or (
        result.get("skipped_reason") and (result.get("request") or {}).get("attempted") is True
    ):
        return "已请求，" + str(result.get("skipped_reason") or "待判定")
    if result.get("skipped_reason") or result.get("skipped"):
        return "未执行"
    if result.get("passed") is True:
        return {
            "status_only": "HTTP 状态符合",
            "response_structure": "结构/回显符合",
            "business": "业务断言符合",
        }.get(str(result.get("assertion_scope")), "断言符合")
    return "失败"


def _safe_route_base(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))


def _observed_business_errors(payload: Any) -> list[str]:
    """Show explicit error codes without treating them as an oracle assertion."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return []
    if not isinstance(payload, dict):
        return []
    errors: list[str] = []
    for field, successes in (("returnCode", {"0", "0000", "200"}),
                             ("resultCode", {"0", "0000", "200"})):
        value = payload.get(field)
        if value is not None and str(value) not in successes:
            errors.append(f"{field}={value}")
    for object_name, field in (("icmTrialObj", "errCode"), ("o32TrialObj", "ErrorCode")):
        nested = payload.get(object_name)
        if isinstance(nested, dict):
            value = nested.get(field)
            if value is not None and str(value) not in {"0", "0000"}:
                errors.append(f"{object_name}.{field}={value}")
    return errors


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
        if request.get("body_encoding"):
            lines.append(f"- 请求体形式：{request['body_encoding']}")
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
