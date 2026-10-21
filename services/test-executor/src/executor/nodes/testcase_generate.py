"""testcase_generate 节点：接口 + 需求 → 测试用例与脚本（F-GEN-01~04 / R-GEN-01~04）。

归属（docs/11 泳道 / docs/12 产物布局）：GEN 节点属丙（test-executor），
`testcases.json` 与 `scripts/` 均为丙写——本函数的 `store` 参数须传入
**test-executor 身份**的 ArtifactStore（Prompt 加载器复用甲的 prompts_loader）。
单体过渡期由编排进程驱动；服务拆分后本模块随迁 test-executor。

分工红线（脚本不经 LLM）：
- LLM 只产用例元数据（category/title/params/expected/溯源）；
- pytest 脚本由程序模板渲染（F-GEN-03：可加载性由模板保证 + py_compile 校验）；
- `id`（TC-###）/ `script_path` / `loadable` / `low_confidence` 全部由代码生成或回填。

程序化校验：
- R-GEN-02：params 参数名 ⊆ 接口定义参数集合——违规用例丢弃，带反馈重生成一次；
  二次仍违规 → 该接口用例放弃，记 X-GEN-02 告警；
- R-GEN-05/06：expected/assertions 必须与 Code Review 的源码 oracle 完全一致，且请求满足 oracle 前置条件；
  无 oracle 或不匹配的断言不会成为可执行预期；
- R-GEN-03：脚本 py_compile，失败记 loadable=false；
- X-GEN-01：源码已证实的场景漏产时定向补产；其余场景留在评审缺口中；
- X-GEN-03：接口含 unknown 参数 → 用例标 low_confidence。

完成判据（docs/12 §1.3）：loadable 全真 → completed；存在不可加载脚本 →
mark_degraded（manual_intervention，D1：不阻塞下游）。零用例 → X-GEN-02 节点失败。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
from functools import partial
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, unquote, urljoin, urlparse

from common.artifacts import ArtifactStore
from common.design_coverage import oracle_request_bindings
from common.case_validation import (
    expected_client_error,
    inspect_case_parameters,
    is_runtime_injected_param,
    materialize_declared_defaults,
)
from common.dto_validation import (
    dto_precondition_reachability_issue,
    dto_validation_oracle_issue,
)
from common.engine import Engine, JobState
from common.errors import X_LLM_02, X_ORCH_01, PipelineError
from common.field_value_contracts import documented_codes
from common.llm import LlmResult
from common.llm_batch import chunk_json_items
from common.llm_repair import complete_with_json_repair
from common.llm_text import strip_code_fence
from common.oracle_conditions import execution_blockers, normalize_expected_behavior, normalize_state_conditions, required_control_kinds
from common.prompts_loader import PromptSpec, load_prompt
from common.response_assertions import (
    evaluate_response_assertions,
    legacy_response_assertions,
    normalize_response_assertions,
    response_assertion_scope,
)
from executor.nodes.e2e_surface import ProbeOptions
from executor.nodes.e2e_surface import inspect_e2e_surface as probe_surface
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

NODE_NAME = "testcase_generate"

_VALID_CATEGORIES = ("normal", "boundary", "exception")
# 单批接口数上限：从 3 收紧到 2。解除「每点 1 条、整批 40 条」软封顶后，单个 REQ
# 测试点要按维度充分展开（枚举/边界/正反路径），单批输出会变长；把每批接口数降一档，
# 给展开腾出生成长度空间，避免撞上限被截断。批次数变多不影响续跑——检查点按批次
# index 记录完成项（见 _load_generation_checkpoint），批次总数变化不会使旧检查点失效。
_MAX_CASE_INTERFACES_PER_BATCH = 2
_CHECKPOINT_FILENAME = "testcase_generate_checkpoint.json"
# 参数一致性违规明细：内部排查文件（非产物契约）。原有的 X-GEN-02 告警只有接口号，
# 参数名与违规原因只活在内存与检查点里，而检查点成功后即被清除——真机排查时无从
# 判断是「清单外参数」「缺必填」还是「body 不是具体 JSON 对象」，只能靠探针反推。
_VIOLATIONS_FILENAME = "case_param_violations.json"
# 测试点拆分维度补全：按字符预算分批，避免单批测试点过多导致长 JSON 截断。
# 12k 字符/批实测单批推理 250~780s，直接顶穿 LLM_TIMEOUT_S（300s）——11 批里 6 批
# 三次尝试全部超时、白等 926s，55% 测试点退回模板 hints。故两步收紧：
#   ① 单批降到 4k（约 8 个测试点），把单批思考时长压到超时线以内；
#   ② 批次间有界并发，串行会让墙钟等于各批之和（实测 11 批串行 1h59m）。
_TP_HINTS_PROMPT = "testpoint_derive"
_TP_HINTS_MAX_CHARS = 4_000
# 中转站对同一 job 的瞬时并发有限，并发度过高会让各批互相挤占、集体超时。
# 实测 4 并发下网关开始返回 429（约 7% 调用被限流），故降到 2：让限制在入口不发生，
# 而不是靠下游重试补救（重试期间的等待同样计入墙钟）。代价是墙钟约翻倍，
# 若网关额度放宽可上调；429 已归为可重试，偶尔越限不会直接判死节点。
_TP_HINTS_CONCURRENCY = 2
# 本子调用失败即退化为模板 hints（非正确性问题），故收窄重试上限：
# 3 次尝试最坏要 3×超时+25s，在并发槽有限的情况下会把一个槽占满十几分钟，而收益仅是
# 少数批次被救回；1 次重试把最坏等待砍到 2×超时+5s。
_TP_HINTS_MAX_ATTEMPTS = 2

# 「超长字符串」紧凑记号：模型只写 __LONG_<n>__，由 _expand_long_markers 展开成真实长度。
# 背景：既有的 boundary 口径要求「字符串取超长（>1024）」，于是模型逐字符去数字面量
# （实测原始输出里出现 `"xxx…x" - that's maybe 256? Need 1025` 的自问自答），推理量爆炸
# 并撞生成长度上限——8 批里 6 批整批作废。把「数 1025 个字符」这种非语言任务移回程序，
# 是这个节点拿回输出稳定性的关键。
_LONG_MARKER_RE = re.compile(r"^__LONG_(\d{1,7})__$")
# 展开上限：防模型写错位数（如 __LONG_9999999__）把产物撑爆
_LONG_MARKER_MAX = 65_536

# 实现片段按完整源码范围注入；不得按行数或字符数裁掉方法尾部。
# 源码里可能带示例口令；与 code_review 的证据采集同一口径先脱敏再给模型。
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;)]+)"
)

# 模型常把同一集合命名为 testcases/items/results，或包进 data/result/output，
# 或把「只输出 cases 数组」误解为裸数组（drift 登记表 testcase 行已验证）。
# 仅归一化容器名；条目内容仍走下方 interface_id/category 硬校验与 Schema 终审。
_CASES_CONTAINER_KEYS = (
    "cases",
    "testcases",
    "test_cases",
    "testCases",
    "case_list",
    "testcase_list",
    "items",
    "results",
)
# 部分本地/兼容网关把模型正文再包为 {"body": "{...}"}；body 只在
# 顶层响应包裹语义中处理，绝不扫描用例 params 内同名字段。
_CASES_WRAPPER_KEYS = ("data", "result", "output", "response", "payload", "content", "body")

_SCRIPT_TEMPLATE = '''"""{case_id}: {title!r}

interface: {interface_id}
requirement: {requirement_id}
generated-by: LLM-AutoTest job {job_id}（脚本由程序模板渲染，元数据来自 LLM 用例设计）
"""

PARAMS = {params!r}
EXPECTED = {expected!r}


def {func_name}(run_context):
    """{case_id}：由 test-executor 提供运行上下文并按 EXPECTED 断言。"""
    response = run_context.invoke(PARAMS)
    run_context.assert_result(response, EXPECTED)
'''


class SupportsComplete(Protocol):
    """LLM 客户端协议（生产为 LlmClient，测试可注入桩）。"""

    async def complete(
        self,
        node: str,
        prompt: PromptSpec,
        user_content: str,
        max_attempts: int | None = None,
    ) -> LlmResult:
        """按协议执行一次补全；max_attempts 覆盖重试上限（None 用客户端默认）。"""
        ...


async def run_testcase_generate(
    engine: Engine,
    state: JobState,
    store: ArtifactStore,
    llm: SupportsComplete,
    prompts_dir: Path,
    auth: dict[str, Any] | None = None,
    e2e: dict[str, Any] | None = None,
) -> Path:
    """执行 testcase_generate 全流程：读前置 → LLM 两轮 → 校验渲染 → 落盘。

    auth（C-17）：鉴权凭据随上下文发给 LLM——登录类接口 normal 用例用真实
    凭据、exception 用错误凭据（用户裁决；凭据暴露面扩大已记录 docs/13）。

    Returns:
        testcases.json 产物路径。

    Raises:
        PipelineError: 零用例（X-GEN-02）/ LLM 输出不可解析（X-LLM-02）/ 前置缺失（X-ORCH-01）。
    """
    engine.check_runnable(state, NODE_NAME)
    engine.mark_running(state, NODE_NAME)
    try:
        requirements = _load_json_array(store, state.job_id, "requirement_items.json", "items")
        interfaces = _load_json_array(store, state.job_id, "interfaces.json", "interfaces")
        repo_roots = _load_repo_roots(store, state.job_id)
        try:
            job_input = store.read_json(state.job_id, "job_input.json")
        except FileNotFoundError:
            job_input = {}
        auto_record_fixtures = bool(job_input.get("auto_record_fixtures", False))
        review = store.read_json(state.job_id, "review_mapping.json")
        contracts = review.get("field_value_contracts")
        test_oracles, oracle_input_warnings = _validated_review_test_oracles(
            review, interfaces
        )
        # 让模型仍按源码 oracle 展开用例维度，但不把执行器专用造数计划交给模型改写。
        model_oracles = [
            {key: value for key, value in oracle.items() if key != "fixture_plan"}
            for oracle in test_oracles
            if "observed_params" not in oracle
        ]
        if isinstance(contracts, list):
            interfaces = _apply_field_value_contracts(interfaces, contracts)
        else:
            # 旧任务没有评审阶段的字段契约，兼容读取目标提交注解以供本地预览。
            interfaces = _enrich_field_descriptions(
                interfaces, repo_roots, _load_repo_target_shas(store, state.job_id)
            )
        mappings = _load_json_array(store, state.job_id, "review_mapping.json", "mappings")
        # 测试点是用例设计的中间证据，而不是另一次不稳定的长 JSON 模型输出。
        # 结构（编号/溯源/场景）由程序按需求、接口映射及评审风险确定性派生，
        # 测试点仅作可追溯的需求清单，不再按提示维度条数补产用例。
        testpoints = _derive_testpoints(requirements, interfaces, mappings)

        prompt = load_prompt(prompts_dir, NODE_NAME)
        # 以接口为分批边界：每批只携带相关需求与映射，避免用例 JSON 随接口数线性膨胀。
        valid: list[dict[str, Any]] = []
        retry_feedback: list[dict[str, Any]] = []
        # 参数与 oracle 违规按「批次 × 轮次」留痕：两轮都收，便于区分
        # 参数结构问题、源码预期不匹配及模型重试是否修复。
        param_violation_rounds: list[dict[str, Any]] = []
        oracle_repair_count = 0
        # 需求覆盖缺口仅统计，不用重复请求填补。
        coverage_gaps: list[tuple[str, int]] = []
        # 兼容检查点字段；不再计算维度条数配额。
        expansion_gaps: list[tuple[str, int, int, int]] = []
        # 只要求预期有证据；执行准备不限制设计。所有条件化 oracle 均交给模型，
        # 缺少夹具的条目也必须生成，并由执行阶段重新检查环境条件。
        executable_interface_ids = {
            str(oracle.get("interface_id", "")) for oracle in model_oracles
        }
        executable_interfaces = [
            interface for interface in interfaces
            if str(interface.get("id", "")) in executable_interface_ids
        ]
        if not executable_interfaces and not test_oracles:
            source_gap = next((
                str(warning) for warning in review.get("test_oracle_warnings") or []
                if isinstance(warning, str) and "异常响应映射缺口" in warning
            ), "")
            detail = f"；{source_gap}" if source_gap else "；请检查 review_mapping.json 的 test_oracle_warnings"
            raise PipelineError(
                code=X_ORCH_01,
                message="Code Review 未形成任何源码可证的执行预期" + detail,
                node_id=NODE_NAME,
            )
        interface_batches = _chunk_case_interfaces(executable_interfaces)
        # 实现片段（按 code_location 读源码）在构造批上下文时按需注入，不进指纹：
        # 它由仓库文件派生且只读本批接口，纳入指纹需要全量预读，收益不足。
        # 指纹只覆盖程序确定性的输入（不含模型补全的 hints）：拆分维度每次调用本就
        # 不同，纳入指纹会让任何续跑都判定「输入已变更」而重头生成。
        checkpoint_fingerprint = _generation_fingerprint(
            requirements, interfaces, mappings, testpoints, test_oracles
        )
        checkpoint = _load_generation_checkpoint(store, state.job_id, checkpoint_fingerprint)
        # Model suggestions are advisory. Do not turn their count into mandatory
        # requests or expand generic oracles to cover unrelated requirements.
        completed_batches = {
            int(str(item.get("index")))
            for item in checkpoint.get("batches", [])
            if isinstance(item, dict) and str(item.get("index", "")).isdigit()
        }
        if checkpoint and interface_batches:
            for item in checkpoint.get("batches", []):
                if not isinstance(item, dict) or not str(item.get("index", "")).isdigit():
                    continue
                if int(item["index"]) not in completed_batches:
                    continue
                valid.extend(
                    case for case in item.get("cases", []) if isinstance(case, dict)
                )
                if isinstance(item.get("violations"), list):
                    retry_feedback.extend(item["violations"])
        checkpoint_batches = [
            item for item in checkpoint.get("batches", []) if isinstance(item, dict)
        ]
        for index, batch in enumerate(interface_batches, start=1):
            if index in completed_batches:
                continue
            user_content = _build_batch_context(
                batch,
                requirements,
                mappings,
                testpoints,
                auth,
                index,
                len(interface_batches),
                repo_roots,
                model_oracles,
                _load_repo_target_shas(store, state.job_id),
            )
            batch_oracles = [
                oracle for oracle in model_oracles
                if str(oracle.get("interface_id", ""))
                in {str(item.get("id", "")) for item in batch}
            ]
            raw_cases = await _complete_cases_with_coverage_recovery(
                llm, prompt, user_content, batch, batch_oracles
            )
            batch_valid, violations = _filter_param_violations(raw_cases, batch, batch_oracles)
            batch_valid, oracle_violations = _filter_oracle_violations(
                batch_valid, batch, batch_oracles
            )
            oracle_repair_count += sum(
                item.get("kind") == "oracle_repaired" for item in oracle_violations
            )
            oracle_violations = [
                item for item in oracle_violations if item.get("kind") != "oracle_repaired"
            ]
            violations.extend(oracle_violations)
            if violations:
                param_violation_rounds.append(
                    {"batch": index, "round": 1, "violations": violations}
                )
            violations2: list[dict[str, Any]] = []
            if violations:
                feedback = json.dumps(
                    {
                        "violations": violations,
                        "note": (
                            "以下用例的参数名或参数结构不符合接口定义。请仅使用接口定义的参数名；"
                            "unknown 的 body/query 容器必须填写具体 JSON 对象，"
                            "不能使用语义标签或字符串占位符；源码注明枚举编码的字段"
                            "必须使用列出的编码。所有 expected/assertions 还必须和 test_oracles"
                            "中的 oracle_id、expected_status、assertions 及 preconditions 完全一致。"
                        ),
                    },
                    ensure_ascii=False,
                )
                raw_cases2 = await complete_with_json_repair(
                    llm,
                    NODE_NAME,
                    prompt,
                    f"{user_content}\n\n{feedback}",
                    partial(_parse_llm_cases, interfaces=batch),
                    # 不做覆盖硬门禁：覆盖缺口由下方 _synthesize_fallback_cases 确定性补齐，
                    # 不因重生成漏产而中断整批流程。
                )
                valid2, violations2 = _filter_param_violations(raw_cases2, batch, batch_oracles)
                valid2, oracle_violations2 = _filter_oracle_violations(
                    valid2, batch, batch_oracles
                )
                oracle_repair_count += sum(
                    item.get("kind") == "oracle_repaired" for item in oracle_violations2
                )
                oracle_violations2 = [
                    item for item in oracle_violations2
                    if item.get("kind") != "oracle_repaired"
                ]
                violations2.extend(oracle_violations2)
                batch_valid = _merge_rounds(batch_valid, valid2)
                # 空 violations2 也记：表示该批第二轮已修好（与被放弃的接口区分开）。
                param_violation_rounds.append(
                    {"batch": index, "round": 2, "violations": violations2}
                )
                retry_feedback.extend(violations2)
            valid.extend(batch_valid)
            checkpoint_batches = [
                item for item in checkpoint_batches if int(item.get("index", 0)) != index
            ]
            checkpoint_batches.append(
                {"index": index, "cases": batch_valid, "violations": violations2}
            )
            _save_generation_checkpoint(
                store,
                state.job_id,
                {
                    "schema_version": "1.0",
                    "fingerprint": checkpoint_fingerprint,
                    "batch_count": len(interface_batches),
                    # 连同批次一起固化拆分依据，供续跑复用同一份 hints。
                    "testpoints": testpoints,
                    "batches": sorted(checkpoint_batches, key=lambda item: int(item["index"])),
                },
            )

        warnings: list[str] = []
        warnings.extend(
            str(item) for item in review.get("test_oracle_warnings", []) if isinstance(item, str)
        )
        warnings.extend(oracle_input_warnings)
        unlinked_risk_points = [
            point
            for point in testpoints
            if point.get("source") == "RISK" and not point.get("interface_id")
        ]
        if unlinked_risk_points:
            claim_refs = [
                str(point.get("claim_id") or point.get("requirement_id") or "")
                for point in unlinked_risk_points
            ]
            warnings.append(
                f"P0-trace: {len(unlinked_risk_points)} 个需求/Claim 测试点没有接口映射"
                f"（{', '.join(claim_refs[:12])}）；需通过 E2E 或人工确认，不能视为 API 已覆盖。"
            )
        # F-GEN-04 溯源覆盖缺口：补产后仍未引用的 REQ 测试点如实告警（分母不缩减）。
        for interface_ids, count in coverage_gaps:
            warnings.append(
                f"P0-trace/F-GEN-04: 接口 {interface_ids} 有 {count} 个 REQ/Claim 测试点未被任何用例"
                "引用（已定向补产一次仍未覆盖，需人工确认该测试点设计）"
            )
        # X-GEN-02：二次仍违规的接口——整接口用例放弃（非部分保留）。
        # 告警必须带具体原因：只有接口号时，排查者无法区分是「清单外参数」「缺必填」
        # 还是「body 不是具体 JSON 对象」，只能写探针重新调模型反推。
        abandoned = sorted({
            v["interface_id"] for v in retry_feedback
            if v.get("kind", "parameter") == "parameter"
        })
        oracle_retry_issues = [v for v in retry_feedback if v.get("kind") == "oracle"]
        if oracle_retry_issues:
            warnings.append(
                f"P0-oracle: {len(oracle_retry_issues)} 条模型断言未通过源码契约校验；"
                "不合规断言已移除，缺少可验证预期的场景不会进入执行清单。"
            )
        for interface_id in abandoned:
            detail = _summarize_violations(
                [item for item in retry_feedback if item.get("kind", "parameter") == "parameter"],
                interface_id,
            )
            warnings.append(
                f"X-GEN-02: 接口 {interface_id} 参数一致性连续 2 次不通过，"
                f"该接口本轮模型生成用例已隔离（生成质量告警）：{detail}"
            )
        if abandoned:
            valid = [c for c in valid if c["interface_id"] not in set(abandoned)]
        # 覆盖检查点续跑/定向补产等旁路结果：最终落盘前再次绑定 oracle。
        # 有 oracle 时以源码契约替换模型预期；无 oracle 的场景随后退出执行清单。
        valid, final_oracle_results = _filter_oracle_violations(
            valid, interfaces, test_oracles
        )
        oracle_repair_count += sum(
            item.get("kind") == "oracle_repaired" for item in final_oracle_results
        )
        final_oracle_violations = [
            item for item in final_oracle_results if item.get("kind") != "oracle_repaired"
        ]
        if oracle_repair_count:
            warnings.append(
                f"P0-oracle: {oracle_repair_count} 条模型断言或状态码与源码契约不一致；"
                "已直接替换为 Code Review 验证过的预期。"
            )
        unverified_cases = [case for case in valid if case.get("oracle_status") != "verified"]
        if unverified_cases:
            by_interface: dict[str, int] = {}
            for case in unverified_cases:
                key = str(case.get("interface_id", ""))
                by_interface[key] = by_interface.get(key, 0) + 1
            valid = [case for case in valid if case.get("oracle_status") == "verified"]
        # 数据库依赖的成功分支必须使用 Code Review 认可的记录计划。
        # 保留模型拆分出的有效测试维度，但每条用例运行时单独造数并绑定真实 ID。
        fixture_plans = {
            str(oracle.get("oracle_id", "")): oracle["fixture_plan"]
            for oracle in test_oracles
            if isinstance(oracle.get("fixture_plan"), dict)
        }
        for case in valid:
            plan = fixture_plans.get(str(case.get("oracle_id", "")))
            if plan is not None:
                case["fixture_plan"] = plan

        if auto_record_fixtures:
            interface_by_id = {str(item.get("id", "")): item for item in interfaces}
            success_oracle_ids = {
                str(oracle.get("oracle_id", ""))
                for oracle in test_oracles
                if oracle.get("category") == "normal"
                and any(
                    assertion.get("path") == "returnMsg"
                    and assertion.get("op") == "equals"
                    and assertion.get("value") in {"新增成功", "更新成功", "删除成功"}
                    for assertion in oracle.get("assertions", [])
                )
            }
            unisolated = [
                case for case in valid
                if str(case.get("oracle_id", "")) in success_oracle_ids
                and str((interface_by_id.get(str(case.get("interface_id", "")), {}).get("signature") or {}).get("path", ""))
                .rsplit("/", 1)[-1] in {"add", "update", "delete"}
                and not isinstance(case.get("fixture_plan"), dict)
            ]
            if unisolated:
                warnings.append(
                    "P0-fixture: 自动造数已启用，但 Code Review 未提供可核验的造数/清理计划；"
                    "相关新增、更新或删除成功用例已保留设计，待准备测试记录后执行："
                    + ", ".join(sorted({str(case.get("interface_id", "")) for case in unisolated}))
                )
                for case in unisolated:
                    case["execution_blockers"] = list(case.get("execution_blockers") or []) + [
                        "待准备源码认可的测试记录与清理计划"
                    ]
                    case["execution_readiness"] = "blocked"

        fixture_oracles = [oracle for oracle in test_oracles if oracle.get("fixture_plan")]
        for oracle in fixture_oracles:
            oracle_id = str(oracle.get("oracle_id", ""))
            if any(str(case.get("oracle_id", "")) == oracle_id for case in valid):
                continue
            plan = oracle["fixture_plan"]
            valid.append({
                "interface_id": oracle["interface_id"],
                "category": oracle["category"],
                "title": oracle["scenario"],
                "params": {"request": dict(plan["target_body"])},
                "expected": oracle["expected_status"],
                "assertions": oracle["assertions"],
                "oracle_id": oracle_id,
                "oracle_status": "verified",
                "oracle_evidence_refs": oracle["evidence_refs"],
                "fixture_plan": plan,
            })
        # Runtime observations are already tied to source-validated inputs and stable
        # responses. Recreate those exact requests deterministically so a malformed or
        # omitted LLM case cannot discard a verified executable contract.
        runtime_cases, runtime_case_warnings = _synthesize_runtime_observation_cases(
            test_oracles,
            interfaces,
            review.get("evidence_catalog") or [],
        )
        runtime_cases, runtime_param_violations = _filter_param_violations(
            runtime_cases, interfaces, test_oracles
        )
        runtime_cases, runtime_oracle_results = _filter_oracle_violations(
            runtime_cases, interfaces, test_oracles
        )
        runtime_rejected = runtime_param_violations + [
            item for item in runtime_oracle_results
            if item.get("kind") != "oracle_repaired"
        ]
        if runtime_rejected:
            warnings.append(
                "P0-oracle: 运行实测请求未能转换为可执行用例："
                + "; ".join(
                    f"{item.get('interface_id', '')} {item.get('message', '')}"
                    for item in runtime_rejected
                )
            )
        warnings.extend(runtime_case_warnings)
        valid = _merge_rounds(valid, runtime_cases)
        covered_oracle_ids = {str(case.get("oracle_id", "")) for case in valid}
        missing_oracles = [oracle for oracle in test_oracles
                           if str(oracle.get("oracle_id", "")) not in covered_oracle_ids]
        for oracle in missing_oracles:
            interface = next(item for item in interfaces if item["id"] == oracle["interface_id"])
            content = _build_batch_context(
                [interface], requirements, mappings, testpoints, auth, 1, 1,
                repo_roots, [oracle], _load_repo_target_shas(store, state.job_id),
            ) + "\n仅补齐这一个 oracle 的测试设计。执行环境尚未准备不能成为省略用例的理由。"

            def parse_missing(response: str) -> list[dict[str, Any]]:
                additions = _parse_llm_cases(response, [interface])
                additions, parameter_issues = _filter_param_violations(additions, [interface], [oracle])
                additions, oracle_issues = _filter_oracle_violations(additions, [interface], [oracle])
                additions = [case for case in additions if case.get("oracle_status") == "verified"]
                if not additions:
                    raise PipelineError(code=X_LLM_02, node_id=NODE_NAME,
                                        message="漏产场景未补齐有效请求：" + str(parameter_issues + oracle_issues))
                return additions

            try:
                recovered = await complete_with_json_repair(llm, NODE_NAME, prompt, content, parse_missing)
                valid = _merge_rounds(valid, recovered)
            except PipelineError as exc:
                # Do not convert a transport outage into a successful partial design.
                if exc.code != X_LLM_02:
                    raise
                warnings.append(f"P0-oracle: {oracle['interface_id']} {oracle['scenario']} 待补齐有效请求；已保留条件及预期：{exc.message}")
                valid.append({
                    "interface_id": oracle["interface_id"], "category": oracle["category"],
                    "title": oracle["scenario"], "params": {},
                    "expected": oracle["expected_status"], "assertions": oracle["assertions"],
                    "oracle_id": oracle["oracle_id"], "oracle_status": "verified",
                    "oracle_evidence_refs": oracle["evidence_refs"],
                    "oracle_scenario": oracle["scenario"],
                    "oracle_control_kinds": list(oracle.get("control_kinds") or []),
                    "request_conditions": oracle.get("preconditions") or [],
                    "state_conditions": oracle.get("state_conditions") or [],
                    **({"expected_behavior": oracle["expected_behavior"]} if oracle.get("expected_behavior") else {}),
                    "execution_readiness": "blocked",
                    "execution_blockers": execution_blockers(oracle) + ["有效 HTTP 请求尚未补齐，禁止发送占位请求"],
                })
        valid = _merge_rounds([], valid)
        expansion_gaps = []
        # 明细落盘放在 raise 之前：失败路径反而最需要它，而检查点会被成功路径清除，
        # 只在检查点里留明细等于「成功时被删、失败时才可能看到」。
        _save_param_violations(store, state.job_id, param_violation_rounds, abandoned)
        if not valid:
            raise PipelineError(
                code="X-GEN-02",
                message="全部接口用例生成失败（参数一致性二次不过或漏产）",
                node_id=NODE_NAME,
                details={"warnings": warnings},
            )

        # 元数据补全 + 脚本渲染（TC-### 编号、模板脚本、loadable、low_confidence）
        cases, any_unloadable = _finalize_cases(
            valid, state.job_id, store, interfaces, testpoints
        )
        bound_oracle_ids, missing_request_ids = oracle_request_bindings(test_oracles, cases)
        if unverified_cases:
            discarded_note = (
                f"{len(unverified_cases)} 条候选未绑定适用预期，已剔除（"
                + ", ".join(f"{key} {count} 条" for key, count in sorted(by_interface.items()))
                + "）"
            )
            if missing_request_ids:
                warnings.append(
                    f"P0-oracle: {discarded_note}；定向补产后仍有 {len(missing_request_ids)} 个源码场景"
                    "待补有效请求：" + ", ".join(sorted(missing_request_ids))
                )
            else:
                warnings.append(
                    f"生成修复：{discarded_note}；最终 {len(bound_oracle_ids)}/{len(test_oracles)} 个源码场景"
                    "均已绑定有效请求设计。执行条件另行统计。"
                )
        remaining_uncovered = _uncovered_req_testpoints(cases, testpoints, interfaces)
        if remaining_uncovered:
            claim_count = len({str(point["claim_id"]) for point in remaining_uncovered if point.get("claim_id")})
            warnings.append(
                f"P0-oracle/F-GEN-04: {len(remaining_uncovered)} 个接口维度的需求测试点"
                f"（涉及 {claim_count} 条需求声明）尚未绑定可核验的业务断言及用例关联；"
                "需按 Claim 补查源码行为、业务预期和关联，不按测试点数量凑请求。设计覆盖与实际执行覆盖分别统计。"
            )
        points_by_id = {str(point.get("id", "")): point for point in testpoints}
        status_only_claim_cases = [
            case
            for case in cases
            if case.get("category") == "normal"
            and case.get("oracle_status") != "unverified"
            and points_by_id.get(str(case.get("testpoint_id", "")), {}).get("source") == "REQ"
            and not case.get("assertions")
            and not legacy_response_assertions(str(case.get("expected", "")))
        ]
        if status_only_claim_cases:
            warnings.append(
                f"P0-assertion: {len(status_only_claim_cases)} 条需求主流程接口用例没有可执行的响应字段断言，"
                "当前只能判定 HTTP 状态；需补充字段级 assertions 后才能作为业务结果通过。"
            )
        e2e_cases: list[dict[str, Any]] = []
        if e2e and bool(e2e.get("generate_cases")):
            # E2E 是可选产物：接口用例已在上方定型，探测或生成失败一律降级为告警，
            # 不让它把本节点打成失败并连带打断下游（曾因一条坏用例导致 4 节点降级）。
            try:
                from executor.e2e.service import generate_job
                from executor.nodes.auto_execute import login_for_headers
                e2e_headers = await login_for_headers(auth) if auth and e2e.get("auth_mode") == "reuse" else None
                design = await generate_job(store, state.job_id, llm, prompts_dir, e2e, auth, e2e_headers)
                e2e_cases = design.get("cases", [])
                if design.get("status") != "completed":
                    warnings.append(f"E2E 设计尚有缺口：{len(design.get('gaps', []))} 项，详见独立设计清单；已保留有效业务用例")
            except Exception as exc:
                message = exc.message if isinstance(exc, PipelineError) else str(exc)
                logger.warning("E2E 用例生成失败，跳过（不影响接口用例）：%s", message)
                warnings.append(f"X-E2E-02: E2E 用例生成未完成：{message}；独立工作台保留设计进度")
        if any_unloadable:
            warnings.append("R-GEN-03: 存在不可加载脚本（loadable=false），详见用例清单")

        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "job_id": state.job_id,
            "cases": cases,
            "testpoints": testpoints,
        }
        if e2e_cases:
            payload["e2e_cases"] = e2e_cases
        if warnings:
            payload["warnings"] = warnings
        try:
            path = store.write(state.job_id, "testcases.json", payload)
        except PipelineError as exc:
            if exc.code == X_ORCH_01:
                raise PipelineError(
                    code=X_LLM_02,
                    message=f"LLM 输出未过 Schema 校验：{exc.message}",
                    node_id=NODE_NAME,
                    details=exc.details,
                ) from exc
            raise
        if any_unloadable:
            # docs/12 §1.3：loadable 非全真 → 降级（D1：不阻塞下游）
            engine.mark_degraded(
                state, NODE_NAME, "存在不可加载脚本（R-GEN-03），相关用例需人工修复后复跑"
            )
        else:
            engine.mark_completed(state, NODE_NAME)
        _clear_generation_checkpoint(store, state.job_id)
        return path
    except PipelineError as exc:
        engine.mark_failed(state, NODE_NAME, exc)
        raise


def _generation_fingerprint(
    requirements: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    testpoints: list[dict[str, Any]],
    test_oracles: list[dict[str, Any]] | None = None,
) -> str:
    """为批次检查点生成输入指纹，防止需求或接口变更时复用旧用例。"""
    payload = {
        # 变更测试点溯源契约时使旧未完成检查点失效，避免续跑复用无 Claim 关联的用例。
        "contract_version": "source-oracle-v6-source-bound-boundary",
        "requirements": requirements,
        "interfaces": interfaces,
        "mappings": mappings,
        "testpoints": testpoints,
        "test_oracles": test_oracles or [],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validated_review_test_oracles(
    review: dict[str, Any], interfaces: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """二次核对 Review 契约的证据、接口归属和输入路径后再交给生成模型。"""
    raw_oracles = review.get("test_oracles")
    if raw_oracles is None:
        return [], [
            "review_mapping 缺少 test_oracles；请从 Code Review 重跑以取得源码预期"
        ]
    if not isinstance(raw_oracles, list):
        return [], ["review_mapping.test_oracles 结构不合法，请在 Code Review 修复"]
    interface_by_id = {
        str(item.get("id", "")): item for item in interfaces if item.get("id")
    }
    evidence_by_id = {
        str(item.get("evidence_id", "")): item
        for item in (review.get("evidence_catalog") or [])
        if isinstance(item, dict) and item.get("evidence_id")
    }
    valid: list[dict[str, Any]] = []
    rejected = 0
    rejected_dto_binding = 0
    rejected_unreachable = 0
    for raw in raw_oracles:
        if not isinstance(raw, dict):
            rejected += 1
            continue
        oracle = dict(raw)
        interface_id = str(oracle.get("interface_id", ""))
        interface = interface_by_id.get(interface_id)
        refs = oracle.get("evidence_refs")
        if (
            not interface
            or not re.fullmatch(r"ORC-IF-[A-Z]+-\d{3}-\d{3}", str(oracle.get("oracle_id", "")))
            or oracle.get("category") not in _VALID_CATEGORIES
            or not re.fullmatch(r"(?:status=[1-5]\d{2}|status_class=[1-5]xx|unresolved)", str(oracle.get("expected_status", "")))
            or not isinstance(refs, list)
            or not refs
            or any(
                not isinstance(ref, str)
                or ref not in evidence_by_id
                or interface_id not in (evidence_by_id[ref].get("interface_ids") or [])
                for ref in refs
            )
        ):
            rejected += 1
            continue
        try:
            oracle["preconditions"] = normalize_response_assertions(oracle.get("preconditions"), label="preconditions")
            oracle["assertions"] = normalize_response_assertions(oracle.get("assertions"))
        except ValueError:
            rejected += 1
            continue
        raw_quotes = oracle.get("evidence_quotes")
        if not isinstance(raw_quotes, list):
            rejected += 1
            continue
        required_targets = {"expected_status"}
        if oracle.get("expected_behavior"):
            required_targets.add("expected_behavior")
        if isinstance(oracle.get("state_conditions"), list):
            required_targets.update(f"state_conditions[{index}]" for index in range(len(oracle["state_conditions"])))
        if oracle.get("branch_id"):
            required_targets.add("branch")
        required_targets.update(
            f"preconditions[{index}]" for index in range(len(oracle["preconditions"]))
        )
        required_targets.update(
            f"assertions[{index}]" for index in range(len(oracle["assertions"]))
        )
        quoted_targets: set[str] = set()
        quotes_valid = True
        for quote in raw_quotes:
            if not isinstance(quote, dict):
                quotes_valid = False
                break
            target = str(quote.get("target", ""))
            ref = str(quote.get("evidence_ref", ""))
            source = evidence_by_id.get(ref)
            quoted_text = str(quote.get("quote", ""))
            if (
                target not in required_targets
                or ref not in refs
                or not source
                or interface_id not in (source.get("interface_ids") or [])
                or not quoted_text
                or quoted_text not in str(source.get("excerpt", ""))
            ):
                quotes_valid = False
                break
            quoted_targets.add(target)
        if not quotes_valid or not required_targets.issubset(quoted_targets):
            rejected += 1
            continue
        parameter_names = {
            str(param.get("name", ""))
            for param in interface.get("params", [])
            if isinstance(param, dict) and param.get("name")
        }
        if any(
            str(condition.get("path", "")).split(".", 1)[0].split("[", 1)[0]
            not in parameter_names
            for condition in oracle["preconditions"]
        ):
            rejected += 1
            continue
        if dto_validation_oracle_issue(oracle, interface, evidence_by_id):
            rejected += 1
            rejected_dto_binding += 1
            continue
        if dto_precondition_reachability_issue(
            oracle, interface, list(evidence_by_id.values())
        ):
            rejected += 1
            rejected_unreachable += 1
            continue
        try:
            behavior = normalize_expected_behavior(oracle.get("expected_behavior"), evidence_by_id, interface_id, refs)
            if oracle.get("expected_status") == "unresolved" and behavior is None:
                raise ValueError("缺少本接口处理行为证据")
            if behavior:
                oracle["expected_behavior"] = behavior
            oracle["state_conditions"] = normalize_state_conditions(
                oracle.get("state_conditions"), evidence_by_id, interface_id, refs,
            )
        except ValueError:
            rejected += 1
            continue
        oracle["control_kinds"] = sorted(
            set(oracle.get("control_kinds") or []) | required_control_kinds(oracle)
        )
        plan = oracle.get("fixture_plan")
        if plan is not None and not _valid_record_fixture_plan(
            plan, interface_id, interface_by_id, evidence_by_id
        ):
            rejected += 1
            continue
        if any(evidence_by_id[ref].get("category") == "runtime_observation" for ref in refs):
            observed_cases, _ = _synthesize_runtime_observation_cases(
                [oracle], interfaces, list(evidence_by_id.values())
            )
            if len(observed_cases) != 1:
                rejected += 1
                continue
            # 运行观测只证明这一份精确输入，不授权模型扩展其它参数，
            # 也不自动证明它覆盖了某个需求/Claim。
            oracle["observed_params"] = observed_cases[0]["params"]
        oracle["assertion_scope"] = "framework_boundary" if oracle.get("oracle_basis") == "spring_required_body" else response_assertion_scope(oracle["assertions"])
        oracle["execution_blockers"] = execution_blockers(oracle)
        if oracle["execution_blockers"]:
            oracle["execution_readiness"] = "blocked"
        valid.append(oracle)
    warnings = []
    if rejected:
        warnings.append(
            f"{rejected} 条 Code Review oracle 未通过接口/证据/路径复核，已丢弃；相关场景不进入执行清单"
        )
    if rejected_dto_binding:
        warnings.append(
            f"其中 {rejected_dto_binding} 条 DTO 校验预期的字段与源码注解不匹配，已丢弃；请从 Code Review 重跑"
        )
    if rejected_unreachable:
        warnings.append(
            f"其中 {rejected_unreachable} 条场景违反请求 DTO 源码校验约束，目标 Controller/Service 分支不可达，已丢弃"
        )
    return valid, warnings


def _synthesize_runtime_observation_cases(
    test_oracles: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
    evidence_catalog: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Restore executable cases from source-validated, repeatable HTTP observations."""
    interface_by_id = {
        str(item.get("id", "")): item for item in interfaces if item.get("id")
    }
    evidence_by_id = {
        str(item.get("evidence_id", "")): item
        for item in evidence_catalog
        if isinstance(item, dict) and item.get("evidence_id")
    }
    cases: list[dict[str, Any]] = []
    warnings: list[str] = []
    for oracle in test_oracles:
        runtime_evidence = [
            evidence_by_id.get(str(ref))
            for ref in oracle.get("evidence_refs", [])
            if isinstance(ref, str)
        ]
        runtime_evidence = [
            item for item in runtime_evidence
            if isinstance(item, dict)
            and item.get("category") == "runtime_observation"
            and item.get("role") == "runtime_contract"
        ]
        if not runtime_evidence:
            continue
        interface_id = str(oracle.get("interface_id", ""))
        interface = interface_by_id.get(interface_id)
        if not interface:
            continue
        restored = False
        for evidence in runtime_evidence:
            if not isinstance(evidence, dict):
                continue
            observation = _parse_runtime_observation(str(evidence.get("excerpt", "")))
            if observation is None:
                continue
            if not _runtime_status_matches(
                int(observation["status"]), str(oracle.get("expected_status", ""))
            ):
                continue
            response_outcomes = evaluate_response_assertions(
                observation["response"], oracle.get("assertions", []), _runtime_observation_params(interface, observation)
            )
            if not response_outcomes or any(
                outcome.get("passed") is not True for outcome in response_outcomes
            ):
                continue
            params = _runtime_observation_params(interface, observation)
            if params is None:
                continue
            precondition_outcomes = evaluate_response_assertions(
                params, oracle.get("preconditions", [])
            )
            if any(outcome.get("passed") is not True for outcome in precondition_outcomes):
                continue
            cases.append({
                "interface_id": interface_id,
                "category": str(oracle["category"]),
                "title": str(oracle.get("scenario", "运行实测场景")),
                "params": params,
                "expected": str(oracle["expected_status"]),
                "assertions": list(oracle.get("assertions") or []),
                "oracle_id": str(oracle["oracle_id"]),
                "oracle_status": "verified",
                "oracle_evidence_refs": list(oracle.get("evidence_refs") or []),
            })
            restored = True
            break
        if not restored:
            warnings.append(
                f"运行实测 oracle {interface_id} {oracle.get('scenario', '')} "
                "未能从证据还原为符合接口定义且满足前置条件的 HTTP 请求"
            )
    return cases, warnings


def _parse_runtime_observation(excerpt: str) -> dict[str, Any] | None:
    """Parse only the bounded evidence format written by the runtime probe."""
    request_line = next(
        (line for line in excerpt.splitlines() if line.startswith("请求：")), None
    )
    request_json_line = next(
        (line for line in excerpt.splitlines() if line.startswith("请求 JSON：")), None
    )
    response_json_line = next(
        (line for line in excerpt.splitlines() if line.startswith("响应 JSON：")), None
    )
    status_line = next(
        (line for line in excerpt.splitlines() if line.startswith("HTTP status=")), None
    )
    content_type_line = next(
        (line for line in excerpt.splitlines() if line.lower().startswith("content-type:")), None
    )
    repeat_line = next(
        (line for line in excerpt.splitlines() if line.startswith("重复观测次数：")), None
    )
    if (request_line is None or request_json_line is None or response_json_line is None
            or status_line is None or content_type_line is None or repeat_line is None):
        return None
    request_match = re.fullmatch(r"请求：([A-Z]+)\s+(\S+)", request_line)
    status_match = re.fullmatch(r"HTTP status=(\d{3})", status_line)
    repeat_match = re.fullmatch(r"重复观测次数：(\d+)", repeat_line)
    content_type = content_type_line.partition(":")[2].strip()
    if (
        not request_match
        or not status_match
        or not repeat_match
        or int(repeat_match.group(1)) < 2
        or not content_type.lower().startswith("application/json")
    ):
        return None
    try:
        request_body = json.loads(request_json_line.partition("：")[2])
        response_body = json.loads(response_json_line.partition("：")[2])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(request_body, dict) or not isinstance(response_body, dict):
        return None
    return {
        "method": request_match.group(1),
        "target": request_match.group(2),
        "request": request_body,
        "status": int(status_match.group(1)),
        "response": response_body,
    }


def _runtime_status_matches(status: int, expected: str) -> bool:
    if expected.startswith("status="):
        return expected.removeprefix("status=") == str(status)
    if expected.startswith("status_class=") and expected.endswith("xx"):
        return expected.removeprefix("status_class=")[0] == str(status)[0]
    return False


def _runtime_observation_params(
    interface: dict[str, Any], observation: dict[str, Any]
) -> dict[str, Any] | None:
    """Map an observed route/query/body to the interface's declared parameter names."""
    signature = interface.get("signature")
    if not isinstance(signature, dict):
        return None
    if str(signature.get("method", "")).upper() != str(observation.get("method", "")).upper():
        return None
    template = urlparse(str(signature.get("path", "")))
    observed = urlparse(str(observation.get("target", "")))
    route_parts = re.split(r"(\{[^{}]+\})", template.path)
    group_names: dict[str, str] = {}
    pattern_parts: list[str] = []
    for part in route_parts:
        placeholder = re.fullmatch(r"\{([^{}]+)\}", part)
        if placeholder:
            group = f"path_{len(group_names)}"
            group_names[placeholder.group(1)] = group
            pattern_parts.append(f"(?P<{group}>[^/]+)")
        else:
            pattern_parts.append(re.escape(part))
    path_match = re.fullmatch("".join(pattern_parts), observed.path)
    if not path_match:
        return None
    observed_path_values = {
        name: unquote(path_match.group(group)) for name, group in group_names.items()
    }
    query_pairs = parse_qsl(observed.query, keep_blank_values=True)
    query_values: dict[str, str] = {}
    for name, value in query_pairs:
        if name in query_values:
            return None
        query_values[name] = value
    definitions = [
        item for item in interface.get("params", []) if isinstance(item, dict)
    ]
    body_definitions = [item for item in definitions if item.get("in") == "body"]
    query_names = {
        str(item.get("name", "")) for item in definitions if item.get("in") == "query"
    }
    if not set(query_values).issubset(query_names):
        return None
    if observation.get("request") is not None and len(body_definitions) != 1:
        return None
    params: dict[str, Any] = {}
    for definition in definitions:
        name = str(definition.get("name", ""))
        if not name:
            continue
        location = str(definition.get("in", ""))
        if location == "body":
            params[name] = observation["request"]
        elif location == "query" and name in query_values:
            params[name] = query_values[name]
        elif location == "path" and name in observed_path_values:
            params[name] = observed_path_values[name]
        elif definition.get("required") is True and not is_runtime_injected_param(definition):
            return None
    return params


def _valid_record_fixture_plan(
    plan: Any, target_id: str, interfaces: dict[str, dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
) -> bool:
    if not isinstance(plan, dict) or plan.get("kind") != "create_query_soft_delete":
        return False
    if plan.get("cleanup_mode") != "soft_delete" or plan.get("id_field") not in {"id", "ids"}:
        return False
    target = interfaces.get(target_id, {})
    target_path = str((target.get("signature") or {}).get("path", ""))
    if not re.fullmatch(r"/[^/]+/(?:add|update|delete)", target_path):
        return False
    target_action = target_path.rsplit("/", 1)[-1]
    if target_action == "update" and plan["id_field"] != "id":
        return False
    if target_action == "delete" and plan["id_field"] != "ids":
        return False
    if target_action == "add" and (
        plan["id_field"] != "id"
        or str(plan.get("add_interface_id")) != target_id
    ):
        return False
    prefix = target_path.rsplit("/", 1)[0]
    target_repo = str((target.get("code_location") or {}).get("repo", ""))
    for action in ("add", "query", "delete"):
        item = interfaces.get(str(plan.get(f"{action}_interface_id", "")))
        if not item or (item.get("signature") or {}).get("path") != f"{prefix}/{action}":
            return False
        if not target_repo or str((item.get("code_location") or {}).get("repo", "")) != target_repo:
            return False
        if str((item.get("signature") or {}).get("method", "")).upper() != "POST":
            return False
    refs = plan.get("source_refs")
    relevant_ids = {target_id} | {
        str(plan[f"{action}_interface_id"]) for action in ("add", "query", "delete")
    }
    if (not isinstance(refs, list) or len(refs) < 6
            or any(ref not in evidence or not relevant_ids.intersection(
                evidence[ref].get("interface_ids") or []) for ref in refs)):
        return False
    add_body, query_body, target_body = (plan.get(key) for key in
                                         ("add_body", "query_body", "target_body"))
    if (not isinstance(add_body, dict) or not isinstance(query_body, dict)
            or not isinstance(target_body, dict)):
        return False
    marker_field = plan.get("marker_field")
    if marker_field not in add_body or add_body[marker_field] != "__AUTOTEST_MARKER__":
        return False
    placeholder = "__AUTOTEST_RECORD_ID__"
    if target_action == "add":
        if target_body.get(marker_field) != "__AUTOTEST_MARKER__":
            return False
    elif target_body.get(plan["id_field"]) != (
        placeholder if plan["id_field"] == "id" else [placeholder]
    ):
        return False
    return True


def _load_generation_checkpoint(
    store: ArtifactStore, job_id: str, fingerprint: str
) -> dict[str, Any]:
    """读取未完成的批次检查点；不存在、损坏或输入已变更时从头开始。"""
    path = store.job_dir(job_id) / _CHECKPOINT_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("fingerprint") != fingerprint:
        return {}
    batches = payload.get("batches")
    if not isinstance(batches, list):
        return {}
    points = payload.get("testpoints")
    # 老检查点没有 testpoints：留空让上层重新补全，不因缺字段整体判为不可用。
    return {"batches": batches, "testpoints": points if isinstance(points, list) else []}


def _save_generation_checkpoint(
    store: ArtifactStore, job_id: str, payload: dict[str, Any]
) -> None:
    """原子保存内部检查点（不作为公开产物，不参与节点完成判据）。"""
    path = store.job_dir(job_id) / _CHECKPOINT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _clear_generation_checkpoint(store: ArtifactStore, job_id: str) -> None:
    """节点完整成功后删除检查点，避免用户主动重跑时误跳过全部批次。"""
    try:
        (store.job_dir(job_id) / _CHECKPOINT_FILENAME).unlink()
    except FileNotFoundError:
        pass


def _summarize_violations(
    violations: list[dict[str, Any]], interface_id: str, *, max_chars: int = 300
) -> str:
    """把某接口的违规记录压成一行可读原因（按分句去重、保序）。

    `message` 是「；」连接的复合文本（如「清单外参数：foo；缺少必填参数：bar」），
    同一接口两轮往往重复，去重后告警才不会又长又重复。
    """
    seen: list[str] = []
    for item in violations:
        if str(item.get("interface_id", "")) != interface_id:
            continue
        text = str(item.get("message", "")).replace("；", ";")
        for part in text.split(";"):
            fragment = part.strip()
            if fragment and fragment not in seen:
                seen.append(fragment)
    if not seen:
        return "（无明细）"
    summary = "；".join(seen)
    return summary if len(summary) <= max_chars else summary[:max_chars] + "…"


def _save_param_violations(
    store: ArtifactStore,
    job_id: str,
    rounds: list[dict[str, Any]],
    abandoned: list[str],
) -> None:
    """原子写参数与 oracle 一致性违规明细（内部排查文件，不进产物契约与节点完成判据）。

    无任何违规时不留文件——「文件不存在」本身就是「本节点无参数违规」的明确信号，
    不给每个 job 增加一个空文件。写入失败只告警：告警文本里已带原因，此处是补充。
    """
    if not rounds:
        (store.job_dir(job_id) / _VIOLATIONS_FILENAME).unlink(missing_ok=True)
        return
    payload = {
        "schema_version": "1.0",
        "job_id": job_id,
        "note": (
            "内部排查文件（非产物契约）：参数结构（R-GEN-02）和源码 oracle（R-GEN-05）校验违规明细。"
            "testcases.json 的 X-GEN-02 告警只给接口号，具体原因看这里的 message；"
            "params 是归一化后（含程序补的默认值）的实际入参快照。"
        ),
        "abandoned_interfaces": abandoned,
        "rounds": rounds,
    }
    path = store.job_dir(job_id) / _VIOLATIONS_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
    except OSError as exc:  # 明细是排查辅助，写不进去不该让整个节点失败
        logger.warning("参数违规明细写入失败（不影响产物）：%s", exc)


def _load_json_array(
    store: ArtifactStore, job_id: str, filename: str, key: str
) -> list[dict[str, Any]]:
    """读前置产物数组（缺失/结构不符 → X-ORCH-01）。"""
    try:
        payload = store.read_json(job_id, filename)
        entries = payload.get(key)
    except FileNotFoundError as exc:
        raise PipelineError(
            code=X_ORCH_01,
            message=f"前置产物 {filename} 缺失",
            node_id=NODE_NAME,
        ) from exc
    if not isinstance(entries, list):
        raise PipelineError(
            code=X_ORCH_01,
            message=f"前置产物 {filename} 缺 {key} 数组",
            node_id=NODE_NAME,
        )
    return [dict(entry) for entry in entries]


def _load_repo_roots(store: ArtifactStore, job_id: str) -> dict[str, Path]:
    """从 code_inventory.json 取「仓库名 → 本地根目录」，供按 code_location 读源码。

    code_scan 是本节点的前置节点、产物必然存在；但读失败仍只告警不中断——实现片段是
    增强输入，缺了退化为「无代码依据」的既有行为，不该让节点失败。
    """
    try:
        inventory = store.read_json(job_id, "code_inventory.json")
    except (FileNotFoundError, PipelineError) as exc:
        logger.warning("读取 code_inventory.json 失败，本批不注入实现片段：%s", exc)
        return {}
    roots: dict[str, Path] = {}
    try:
        change_set = store.read_json(job_id, "change_set.json")
    except (FileNotFoundError, PipelineError):
        change_set = {}
    snapshot_roots = {
        str(repo.get("name", "")): str(repo.get("source_path", ""))
        for repo in change_set.get("repositories", [])
        if isinstance(repo, dict) and repo.get("name") and repo.get("source_path")
    }
    for repo in inventory.get("repositories") or []:
        if not isinstance(repo, dict):
            continue
        name = str(repo.get("name", ""))
        # 接口行号来自 target 快照。优先读接口还原阶段实际解析的同一份快照，
        # 避免工作区已切分支/有未提交改动时按旧行号取到另一段实现。
        raw_path = snapshot_roots.get(name) or str(repo.get("path", ""))
        if name and raw_path:
            roots[name] = Path(raw_path)
    return roots


def _load_repo_target_shas(store: ArtifactStore, job_id: str) -> dict[str, str]:
    try:
        inventory = store.read_json(job_id, "code_inventory.json")
    except (FileNotFoundError, PipelineError):
        return {}
    return {
        str(repo["name"]): str(repo["target_sha"])
        for repo in inventory.get("repositories") or []
        if isinstance(repo, dict)
        and repo.get("name")
        and re.fullmatch(r"[0-9a-fA-F]{40}", str(repo.get("target_sha", "")))
    }


_FIELD_ANNOTATION_RE = re.compile(
    r'@(?:ApiModelProperty|Schema)\s*\(\s*(?:(?:value|description)\s*=\s*)?("(?:\\.|[^"\\])*")'
)


def _enrich_field_descriptions(
    interfaces: list[dict[str, Any]], repo_roots: dict[str, Path],
    target_shas: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """从字段来源行读取注解值域，补回旧扫描产物丢失的编码说明。"""
    enriched: list[dict[str, Any]] = []
    source_cache: dict[tuple[Path, str, str], list[str] | None] = {}
    for interface in interfaces:
        entry = dict(interface)
        repo = str((entry.get("code_location") or {}).get("repo", ""))
        root = repo_roots.get(repo)
        params: list[dict[str, Any]] = []
        for raw_param in entry.get("params") or []:
            if not isinstance(raw_param, dict):
                continue
            param = dict(raw_param)
            fields: list[dict[str, Any]] = []
            for raw_field in param.get("fields") or []:
                if not isinstance(raw_field, dict):
                    continue
                field = dict(raw_field)
                description = _field_annotation_description(
                    field, root, (target_shas or {}).get(repo), source_cache
                )
                if description:
                    field["description"] = description
                    field["description_origin"] = "source"
                fields.append(field)
            if fields:
                param["fields"] = fields
            params.append(param)
        if params:
            entry["params"] = params
        enriched.append(entry)
    return enriched


def _field_annotation_description(
    field: dict[str, Any], root: Path | None, target_sha: str | None,
    source_cache: dict[tuple[Path, str, str], list[str] | None],
) -> str | None:
    if root is None:
        return None
    name = str(field.get("name", ""))
    for ref in field.get("source_refs") or []:
        if not isinstance(ref, dict):
            continue
        relative = str(ref.get("file", ""))
        try:
            start_line = int(ref.get("line", 0))
            end_line = int(ref.get("end_line", 0))
            resolved_root = root.resolve()
            path = (resolved_root / relative).resolve()
        except (OSError, TypeError, ValueError):
            continue
        # 旧产物只有起始行，无法证明注解属于该字段；宁可保留原 DTO 描述，
        # 也不再向后猜测固定行数并误取相邻字段。
        if resolved_root not in path.parents or start_line < 1 or end_line < start_line:
            continue
        cache_key = (resolved_root, relative, target_sha or "")
        if cache_key not in source_cache:
            source_cache[cache_key] = _read_field_source(resolved_root, path, relative, target_sha)
        lines = source_cache[cache_key]
        if lines is None:
            continue
        if end_line > len(lines):
            continue
        declaration = lines[start_line - 1 : end_line]
        if not re.search(rf"\b{re.escape(name)}\s*(?:=|;)", "\n".join(declaration)):
            continue
        for line in declaration:
            match = _FIELD_ANNOTATION_RE.search(line)
            if match:
                try:
                    return str(json.loads(match.group(1))).strip() or None
                except json.JSONDecodeError:
                    continue
    return None


def _read_field_source(
    root: Path, path: Path, relative: str, target_sha: str | None
) -> list[str] | None:
    """优先读目标提交；已物化的无 .git 快照则直接读快照文件。"""
    if target_sha and (root / ".git").exists():
        git_path = shutil.which("git")
        if git_path is None:
            return None
        try:
            # 参数列表调用；SHA 已校验为 40 位十六进制，路径已限制在仓库根内。
            result = subprocess.run(  # noqa: S603
                [git_path, "-C", str(root), "show", f"{target_sha}:{relative}"],
                capture_output=True, text=True, encoding="utf-8", timeout=5, check=False,
            )
        except (OSError, UnicodeDecodeError, subprocess.TimeoutExpired):
            return None
        return result.stdout.splitlines() if result.returncode == 0 else None
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None


def _coded_choices(description: str) -> dict[str, str]:
    """只识别注解中明确的两位编码及其展示名，不猜业务映射。"""
    return documented_codes(description)[0]


def _allowed_codes(description: str) -> set[str]:
    return set(documented_codes(description)[1])


def _apply_field_value_contracts(
    interfaces: list[dict[str, Any]], contracts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """用 Code Review 已定稿的字段说明；生成阶段不再重新猜取值规则。"""
    by_field = {
        (str(item.get("interface_id")), str(item.get("parameter")), str(item.get("field"))): item
        for item in contracts if isinstance(item, dict)
    }
    result: list[dict[str, Any]] = []
    for interface in interfaces:
        entry = dict(interface)
        params: list[dict[str, Any]] = []
        for raw_param in entry.get("params") or []:
            if not isinstance(raw_param, dict):
                continue
            param = dict(raw_param)
            fields: list[dict[str, Any]] = []
            for raw_field in param.get("fields") or []:
                if not isinstance(raw_field, dict):
                    continue
                field = dict(raw_field)
                contract = by_field.get((
                    str(entry.get("id")), str(param.get("name")), str(field.get("name"))
                ))
                if contract is not None:
                    field["description"] = str(contract.get("description") or "")
                    field["documented_values"] = contract.get("documented_values") or []
                    field["example_codes"] = contract.get("example_codes") or []
                    field["value_rule_status"] = contract.get("value_rule_status")
                fields.append(field)
            if fields:
                param["fields"] = fields
            params.append(param)
        if params:
            entry["params"] = params
        result.append(entry)
    return result


def _field_choices(field: dict[str, Any]) -> dict[str, str]:
    if "documented_values" in field:
        return {
            str(item["label"]): str(item["code"])
            for item in field["documented_values"]
            if isinstance(item, dict) and item.get("label") and item.get("code")
        }
    return _coded_choices(str(field.get("description", "")))


def _field_allowed_codes(field: dict[str, Any]) -> set[str]:
    if "documented_values" in field:
        return {
            str(item["code"])
            for item in field["documented_values"]
            if isinstance(item, dict) and item.get("code")
        } | {str(code) for code in field.get("example_codes") or []}
    return _allowed_codes(str(field.get("description", "")))


def _normalize_coded_case_values(
    case: dict[str, Any], interface: dict[str, Any]
) -> dict[str, Any]:
    """成功场景使用源码编码，避免把“一级投标”写入 VARCHAR2(2) 列。"""
    if case.get("category") not in {"normal", "boundary"} or expected_client_error(
        str(case.get("expected", ""))
    ):
        return case
    params = case.get("params")
    if not isinstance(params, dict):
        return case
    updated = dict(params)
    changed = False
    for param in interface.get("params") or []:
        if not isinstance(param, dict) or param.get("in") != "body":
            continue
        name = str(param.get("name", ""))
        payload = updated.get(name)
        if not isinstance(payload, dict):
            continue
        body = dict(payload)
        for field in param.get("fields") or []:
            if not isinstance(field, dict):
                continue
            field_name = str(field.get("name", ""))
            value = body.get(field_name)
            choices = _field_choices(field)
            if isinstance(value, str):
                code = choices.get(value.strip())
                if code is not None:
                    body[field_name] = code
                    changed = True
            elif isinstance(value, list):
                coded = [
                    choices.get(item.strip(), item) if isinstance(item, str) else item
                    for item in value
                ]
                if coded != value:
                    body[field_name] = coded
                    changed = True
        updated[name] = body
    return {**case, "params": updated} if changed else case


def _coded_value_issues(case: dict[str, Any], interface: dict[str, Any]) -> list[str]:
    """正常/边界用例不能把源码枚举外的展示值当作合法输入。"""
    if case.get("category") not in {"normal", "boundary"}:
        return []
    params = case.get("params")
    if not isinstance(params, dict):
        return []
    issues: list[str] = []
    for param in interface.get("params") or []:
        if not isinstance(param, dict) or param.get("in") != "body":
            continue
        payload = params.get(str(param.get("name", "")))
        if not isinstance(payload, dict):
            continue
        for field in param.get("fields") or []:
            if not isinstance(field, dict):
                continue
            allowed = _field_allowed_codes(field)
            if not allowed:
                continue
            name = str(field.get("name", ""))
            value = payload.get(name)
            values = value if isinstance(value, list) else [value]
            invalid = [
                item for item in values
                if isinstance(item, str) and item.strip() and item not in allowed
            ]
            if invalid:
                codes = ", ".join(sorted(allowed))
                issues.append(f"body.{name} 的正常用例须使用源码枚举编码（可用：{codes}）")
    return issues


def _implementation_excerpt(
    interface: dict[str, Any], repo_roots: dict[str, Path],
    target_shas: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """接口的「代码依据」：优先**本次实际改动的代码**，退化到整个方法体（#52）。

    越出仓库根、文件不存在、编码不可解、行号越界都返回 None——宁可不注入，也不猜实现。
    """
    changed = _changed_excerpt(interface)
    if changed is not None:
        return changed
    location = interface.get("code_location") or {}
    repo = str(location.get("repo", ""))
    relative = str(location.get("file", ""))
    raw_root = repo_roots.get(repo)
    if raw_root is None or not relative:
        return None
    try:
        root = raw_root.resolve()
        path = (root / relative).resolve()
    except OSError:
        return None
    # 与 code_review 的证据采集同一防线：只读仓库根之内的文件
    if root not in path.parents or not path.is_file():
        return None
    lines = _read_field_source(root, path, relative, (target_shas or {}).get(repo))
    if lines is None:
        return None
    try:
        start = int(location.get("start_line", 0))
        raw_end = int(location.get("end_line", start))
    except (TypeError, ValueError):
        return None
    if start < 1 or start > len(lines):
        return None
    if raw_end < start or raw_end > len(lines):
        return None
    end = raw_end
    excerpt = _SECRET_VALUE_RE.sub(r"\1\2<redacted>", "\n".join(lines[start - 1 : end]))
    if not excerpt.strip():
        return None
    return {
        "source": "method_body",
        "file": relative,
        "start_line": start,
        "end_line": end,
        "excerpt": excerpt,
        "truncated": False,
    }


def _changed_excerpt(interface: dict[str, Any]) -> dict[str, Any] | None:
    """本次**实际改动**的代码片段（#52）：来自接口还原阶段的 target 版本只读快照。

    为什么不改写工作区文件取窗口：工作区与 target 提交可能不是同一版本，按行号取窗口会
    取错行；片段本身已经是权威内容（行号与候选同版本）。旧产物若已经截断，
    回退读取目标版本完整方法，不把残缺变更片段传给模型。
    """
    evidence = interface.get("change_evidence")
    if not isinstance(evidence, dict):
        return None
    if evidence.get("truncated"):
        return None
    snippets = evidence.get("changed_snippets")
    if not isinstance(snippets, list):
        return None
    parts: list[str] = []
    files: list[str] = []
    first_line = 0
    last_line = 0
    for item in snippets:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        if not text.strip():
            continue
        header = f"// {item.get('file')}:{item.get('start_line')}-{item.get('end_line')}"
        block = f"{header}\n{text.rstrip()}"
        parts.append(block)
        files.append(str(item.get("file", "")))
        if not first_line:
            first_line = int(item.get("start_line") or 0)
        last_line = int(item.get("end_line") or 0)
    if not parts:
        return None
    excerpt = _SECRET_VALUE_RE.sub(r"\1\2<redacted>", "\n\n".join(parts))
    return {
        "source": "changed_lines",
        "level": str(evidence.get("level", "")),
        "files": list(dict.fromkeys(files)),
        "file": files[0],
        "start_line": first_line,
        "end_line": last_line,
        "excerpt": excerpt,
        "truncated": False,
    }


def _build_batch_context(
    interfaces: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    testpoints: list[dict[str, Any]],
    auth: dict[str, Any] | None,
    index: int,
    total: int,
    repo_roots: dict[str, Path] | None = None,
    test_oracles: list[dict[str, Any]] | None = None,
    target_shas: dict[str, str] | None = None,
) -> str:
    """构造一批用例生成上下文，只携带本批接口相关的需求。

    每个接口额外带 `implementation`（按 code_location 读到的实现片段）——正常用例的
    expected 必须落在实现逻辑上，不能只凭 signature 编。读不到就不带该字段。
    """
    interface_ids = {str(item.get("id", "")) for item in interfaces}
    batch_mappings: list[dict[str, Any]] = []
    for mapping in mappings:
        raw_assessments = [
            assessment
            for assessment in (mapping.get("claim_assessments") or [])
            if isinstance(assessment, dict)
        ]
        linked_ids = [str(value) for value in (mapping.get("interface_ids") or [])]
        if not linked_ids and mapping.get("interface_id"):
            linked_ids = [str(mapping["interface_id"])]
        linked_ids.extend(
            str(value)
            for assessment in raw_assessments
            for value in (assessment.get("interface_ids") or [])
        )
        linked_ids = list(dict.fromkeys(linked_ids))
        selected_ids = [value for value in linked_ids if value in interface_ids]
        if not selected_ids:
            continue
        assessments = [
            {
                **dict(assessment),
                "interface_ids": [
                    str(value)
                    for value in (assessment.get("interface_ids") or [])
                    if str(value) in interface_ids
                ],
            }
            for assessment in raw_assessments
        ]
        batch_mappings.append(
            {
                "requirement_id": mapping.get("requirement_id"),
                "interface_id": selected_ids[0],
                "interface_ids": selected_ids,
                "status": mapping.get("status"),
                "claim_assessments": assessments,
            }
        )
    requirement_ids = {str(mapping.get("requirement_id", "")) for mapping in batch_mappings}
    interfaces_payload: list[dict[str, Any]] = []
    for interface in interfaces:
        entry = dict(interface)
        excerpt = _implementation_excerpt(interface, repo_roots or {}, target_shas)
        if excerpt is not None:
            entry["implementation"] = excerpt
        interfaces_payload.append(entry)
    context: dict[str, Any] = {
        "batch": {"index": index, "total": total},
        "interfaces": interfaces_payload,
        "requirements": [
            item for item in requirements if str(item.get("id", "")) in requirement_ids
        ],
        "mappings": batch_mappings,
        "testpoints": [
            point for point in testpoints if str(point.get("interface_id", "")) in interface_ids
        ],
        "test_oracles": [
            oracle for oracle in (test_oracles or [])
            if str(oracle.get("interface_id", "")) in interface_ids
        ],
    }
    if auth:
        # 凭据只出现在本次模型请求内，调用留痕仅记录摘要；不写入用例产物。
        context["auth"] = {
            "login_url": auth.get("login_url"),
            "username": auth.get("username"),
            "password": auth.get("password"),
        }
    return json.dumps(context, ensure_ascii=False)


def _chunk_case_interfaces(interfaces: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """按字符预算后再按 `_MAX_CASE_INTERFACES_PER_BATCH` 个接口拆分，控制用例输出规模。"""
    batches: list[list[dict[str, Any]]] = []
    for char_batch in chunk_json_items(interfaces):
        for index in range(0, len(char_batch), _MAX_CASE_INTERFACES_PER_BATCH):
            batches.append(char_batch[index : index + _MAX_CASE_INTERFACES_PER_BATCH])
    return batches


def _validate_batch_coverage(
    cases: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
    test_oracles: list[dict[str, Any]] | None = None,
) -> None:
    """只要求 Code Review 已证明的场景；旧调用沿用三类门禁。

    覆盖缺失进入 JSON 修复轮，而不是让节点带着“跳过并记录”的残缺产物完成。
    """
    missing = [
        f"{interface.get('id', '')}: {', '.join(categories)}"
        for interface, categories in _missing_categories(cases, interfaces, test_oracles)
    ]
    if missing:
        raise PipelineError(
            code=X_LLM_02,
            message="当前批接口/场景漏产，必须补齐后再提交：" + "; ".join(missing),
            node_id=NODE_NAME,
        )


async def _complete_cases_with_coverage_recovery(
    llm: SupportsComplete,
    prompt: PromptSpec,
    user_content: str,
    interfaces: list[dict[str, Any]],
    test_oracles: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Generate source-backed designs; missing oracle requests recover individually."""
    return await complete_with_json_repair(
        llm, NODE_NAME, prompt, user_content,
        partial(_parse_llm_cases, interfaces=interfaces),
    )


def _missing_categories(
    cases: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
    test_oracles: list[dict[str, Any]] | None = None,
) -> list[tuple[dict[str, Any], list[str]]]:
    """返回缺少的源码可证场景；历史调用保留三类覆盖口径。"""
    categories_by_interface: dict[str, set[str]] = {}
    for case in cases:
        if test_oracles is not None and case.get("oracle_status") == "unverified":
            continue
        categories_by_interface.setdefault(str(case.get("interface_id", "")), set()).add(
            str(case.get("category", ""))
        )
    result: list[tuple[dict[str, Any], list[str]]] = []
    for interface in interfaces:
        interface_id = str(interface.get("id", ""))
        required_categories = (
            {
                str(oracle.get("category", ""))
                for oracle in test_oracles
                if str(oracle.get("interface_id", "")) == interface_id
            }
            if test_oracles is not None else set(_VALID_CATEGORIES)
        )
        missing = [
            category
            for category in _VALID_CATEGORIES
            if category in required_categories
            and category not in categories_by_interface.get(interface_id, set())
        ]
        if missing:
            result.append((interface, missing))
    return result


def _case_dedup_key(case: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """同一测试点的不同输入维度必须保留；相同请求和 oracle 才视为重复。"""
    params = case.get("params") if isinstance(case.get("params"), dict) else {}
    return (
        str(case.get("interface_id", "")),
        str(case.get("category", "")),
        "",
        str(case.get("oracle_id", "")),
        json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str),
    )


def _uncovered_req_testpoints(
    cases: list[dict[str, Any]],
    testpoints: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """返回本批内有接口归属、但没有任何用例回填其编号的 REQ 测试点。

    只统计 `source=REQ`：RISK 点是同一需求下机械派生的同构场景（prompt 明确要求
    按需求归并），对它们做逐点覆盖会逼迫模型产出雷同用例。无接口归属的测试点属
    E2E/人工范畴，不在本节点覆盖面内。
    """
    interface_ids = {str(item.get("id", "")) for item in interfaces}
    referenced: set[tuple[str, str]] = set()
    for case in cases:
        point_ids = case.get("testpoint_ids")
        if not isinstance(point_ids, list):
            point_ids = []
        for point_id in [case.get("testpoint_id"), *point_ids]:
            if point_id:
                referenced.add((str(case.get("interface_id", "")), str(point_id)))
    return [
        point
        for point in testpoints
        if str(point.get("source", "")) == "REQ"
        and str(point.get("interface_id", "")) in interface_ids
        and (str(point.get("interface_id", "")), str(point.get("id", ""))) not in referenced
    ]


# 展开充分性阈值：实际用例数低于 ceil(hints 条数 × 系数) 判为展开不足。
# 系数 < 1 留出余量——不是每条拆分维度都必然独立成一条用例（如「展示验证」
# 可能并入正常路径断言），但显著低于维度数就说明模型没按维度展开，退回了
# 「每点凑一条」的偷懒口径。0.6 由真实 hints 分布（中位 4~5 条）反推：5 条
# 维度至少该产 3 条，低于此几乎必是漏展开。
_EXPANSION_RATIO = 0.6
# hints 条数低于此值不校验：1~2 条维度本就该产 1~2 条，F-GEN-04 的「≥1」已覆盖，
# 再叠加展开校验只会制造噪声告警。
_EXPANSION_MIN_HINTS = 3


def _underexpanded_testpoints(
    cases: list[dict[str, Any]],
    testpoints: list[dict[str, Any]],
) -> list[tuple[str, int, int, int]]:
    """兼容旧调用；拆分维度条数不再构成用例数量要求。"""
    return []


async def _recover_cases_with_targeted_prompt(
    llm: SupportsComplete,
    prompt: PromptSpec,
    user_content: str,
    interfaces: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    instruction: str,
    wanted_testpoints: set[str],
    log_label: str,
) -> list[dict[str, Any]]:
    """定向补产通用骨架：回喂缺口清单 → 解析校验 → 只收目标测试点 → 去重合并。

    F-GEN-04（溯源缺口）与 F-GEN-05（展开不足）共用此骨架，差异只在
    ``instruction``（缺口说明与补产要求）。补产是增强路径：模型输出损坏、
    参数违规、补产跑偏（引用了目标之外的测试点）都不让整批失败，原样返回
    已有用例，缺口最终以告警形式进产物交人工判断。
    """
    targeted_content = f"{user_content}\n\n{instruction}"
    try:
        result = await llm.complete(NODE_NAME, prompt, targeted_content)
        additions = _parse_llm_cases(result.text, interfaces)
    except PipelineError as exc:
        logger.warning("%s补产调用失败，保留已有用例：%s", log_label, exc.message)
        return cases
    accepted, _ = _filter_param_violations(additions, interfaces)
    # 只接受确实指向目标测试点的条目，避免把无关用例混入产物。
    accepted = [
        case
        for case in accepted
        if str(case.get("testpoint_id", "")) in wanted_testpoints
    ]
    if not accepted:
        return cases
    return _merge_case_entries(cases, accepted)


async def _recover_uncovered_testpoints(
    llm: SupportsComplete,
    prompt: PromptSpec,
    user_content: str,
    interfaces: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    uncovered: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """F-GEN-04：为未被任何用例引用的 REQ 测试点做一次定向补产。"""
    instruction = (
        "这是测试点覆盖补充请求。下列 REQ 测试点尚无对应用例，请每个测试点各产出"
        "至少一条有适用 test_oracles 的用例，并在用例中回填其 testpoint_id；"
        "若没有适用 oracle，该测试点保持覆盖缺口，不要编造场景。"
        "所有 expected/assertions 必须逐字复制 oracle；不要根据接口名臆造返回字段或固定值。"
        "只为下面列出的测试点与接口输出，"
        "禁止输出其它接口：\n"
        f"未覆盖测试点：{json.dumps(uncovered, ensure_ascii=False)}\n"
        '输出格式：{"cases":[{"interface_id":"...","requirement_id":"...",'
        '"testpoint_id":"...","category":"normal|boundary|exception","title":"...",'
        '"oracle_id":"输入中的编号","params":{},"expected":"取自 oracle",'
        '"assertions":[]}]}。'
    )
    return await _recover_cases_with_targeted_prompt(
        llm,
        prompt,
        user_content,
        interfaces,
        cases,
        instruction,
        wanted_testpoints={str(point.get("id", "")) for point in uncovered},
        log_label="测试点覆盖",
    )


async def _recover_underexpanded_testpoints(
    llm: SupportsComplete,
    prompt: PromptSpec,
    user_content: str,
    interfaces: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    underexpanded: list[tuple[str, int, int, int]],
    points_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """兼容旧调用；不再按维度条数补产。"""
    return cases


def _merge_case_entries(
    existing: list[dict[str, Any]], additions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """只合并请求与 oracle 相同的条目，保留同一测试点的不同输入维度。"""
    seen = {_case_dedup_key(item) for item in existing}
    merged = list(existing)
    for item in additions:
        key = _case_dedup_key(item)
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged


def _synthesize_fallback_cases(
    missing: list[tuple[dict[str, Any], list[str]]],
) -> list[dict[str, Any]]:
    """程序确定性兜底：LLM 多轮补不齐时，按接口参数定义生成通用用例。

    只生成确定性可推导的场景，不猜业务数据：
    - normal：body 第一个字段给类型默认值，或简单参数给类型默认值，预期 2xx；
    - boundary：body 第一个字段给边界值（空串/负数），或简单参数给边界值，预期 4xx；
    - exception：缺必填 body（保留 path 参数占位），预期 4xx。

    所有兜底用例带 synthesized=True，由 _finalize_cases 统一标 low_confidence，
    提示人工复核（程序兜底语义上不如 LLM 设计的业务场景精确）。
    """
    cases: list[dict[str, Any]] = []
    for interface, categories in missing:
        if_id = str(interface.get("id", ""))
        params_defs = [
            p for p in interface.get("params", []) if isinstance(p, dict)
        ]
        for category in categories:
            case = _synthesize_fallback_case(if_id, category, params_defs)
            if case is not None:
                cases.append(case)
    return cases


def _synthesize_fallback_case(
    if_id: str, category: str, params_defs: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """为单个接口的单个缺失场景构造一条确定性兜底用例；无法推导返回 None。"""
    bodies = [p for p in params_defs if str(p.get("in", "")) == "body"]
    simple = next(
        (p for p in params_defs if str(p.get("in", "")) in {"query", "path"}), None
    )
    path_placeholder = {
        str(p.get("name")): "test"
        for p in params_defs
        if str(p.get("in", "")) == "path"
    }
    # normal 兜底 expected 为 2xx、不豁免缺参校验，须补齐所有必填的非 body 参数。
    required_extra = [
        p
        for p in params_defs
        if p.get("required") is True
        and str(p.get("in", "")) in {"query", "path", "header"}
    ]

    def _body_fields(body: dict[str, Any]) -> list[dict[str, Any]]:
        return [f for f in body.get("fields", []) if isinstance(f, dict)]

    def _body_default(body: dict[str, Any]) -> dict[str, Any]:
        return {
            str(f.get("name")): _type_default_value(
                str(f.get("type", "")), str(f.get("name", ""))
            )
            for f in _body_fields(body)
        }

    def _fill_required_extra(params: dict[str, Any]) -> dict[str, Any]:
        for p in required_extra:
            pname = str(p.get("name"))
            if pname not in params:
                params[pname] = _type_default_value(str(p.get("type", "")), pname)
        return params

    if category == "exception":
        # 缺必填 body（若存在）；无 body 则缺必填 path/query，由 4xx 豁免缺参校验
        if bodies:
            params: dict[str, Any] = dict(path_placeholder)
            title = f"{if_id} 异常用例（必填 body 缺失，程序兜底）"
        else:
            params = {}
            title = f"{if_id} 异常用例（必填参数缺失，程序兜底）"
        return {
            "interface_id": if_id,
            "category": "exception",
            "title": title,
            "params": params,
            "expected": "status_class=4xx",
            "synthesized": True,
        }

    if category not in {"normal", "boundary"}:
        return None

    if bodies:
        # 多个 body 参数（如 response + request）都要给值，避免缺必填被判违规
        params = dict(path_placeholder)
        boundary_applied = False
        for body in bodies:
            name = str(body.get("name"))
            fields = _body_fields(body)
            if category == "boundary" and fields and not boundary_applied:
                first = fields[0]
                params[name] = {
                    str(first.get("name")): _type_boundary_value(str(first.get("type", "")))
                }
                boundary_applied = True
            else:
                params[name] = _body_default(body)
        params = _fill_required_extra(params)
        if category == "normal":
            title = f"{if_id} 正常用例（字段默认值，程序兜底）"
            expected = "status_class=2xx"
        else:
            title = f"{if_id} 边界用例（字段边界值，程序兜底）"
            expected = "status_class=4xx"
    elif simple is not None:
        # 无 body：用简单参数（query/path）给类型默认值/边界值
        sname = str(simple.get("name"))
        stype = str(simple.get("type", ""))
        if category == "normal":
            params = _fill_required_extra(
                {**path_placeholder, sname: _type_default_value(stype, sname)}
            )
            title = f"{if_id} 正常用例（最小请求，程序兜底）"
            expected = "status_class=2xx"
        else:  # boundary
            params = {**path_placeholder, sname: _type_boundary_value(stype)}
            title = f"{if_id} 边界用例（参数 {sname} 边界值，程序兜底）"
            expected = "status_class=4xx"
    else:
        return None

    return {
        "interface_id": if_id,
        "category": category,
        "title": title,
        "params": params,
        "expected": expected,
        "synthesized": True,
    }


def _type_default_value(type_name: str, field_name: str = "") -> Any:
    """按字段类型 + 字段名语义给一个最小但更真实的正常值（程序兜底 normal 用）。"""
    t = (type_name or "").lower()
    name = (field_name or "").lower()
    if any(k in t for k in ("int", "long", "short", "byte")):
        return 1
    if "decimal" in t or "double" in t or "float" in t:
        return 1
    if "bool" in t:
        return True
    if "list" in t or "set" in t or t == "array":
        # 集合字段给非空数组（空数组易被服务端当作缺参拒绝）
        if any(k in name for k in ("to", "cc", "bcc", "recipient", "email", "mail")):
            return ["test@example.com"]
        return ["test"]
    if t == "object":
        return {}
    # String：按字段名语义给更真实的值
    if any(k in name for k in ("email", "mail")):
        return "test@example.com"
    if any(k in name for k in ("phone", "mobile")):
        return "13800000000"
    if any(
        k in name
        for k in ("requestid", "serialno", "request_no", "serial_no", "trace", "uuid")
    ):
        return "test-request-001"
    if any(k in name for k in ("date", "time")):
        return "2026-01-01"
    return "test"


def _type_boundary_value(type_name: str) -> Any:
    """按字段类型给一个边界值（程序兜底 boundary 用，预期触发服务端校验拒绝）。"""
    t = (type_name or "").lower()
    if any(k in t for k in ("int", "long", "short", "byte", "decimal", "double", "float")):
        return -1
    if "bool" in t:
        return "not-a-boolean"
    if t == "array" or "list" in t or "set" in t:
        return []
    if t == "object":
        return {}
    return ""


def _looks_like_cross_node_payload(
    text: str, interfaces: list[dict[str, Any]] | None = None
) -> bool:
    """识别跨节点产物或请求参数对象误串到用例节点的常见形态。"""
    try:
        payload = json.loads(strip_code_fence(text))
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict) or any(
        isinstance(payload.get(key), list) for key in _CASES_CONTAINER_KEYS
    ):
        return False
    keys = {str(key) for key in payload}
    if not keys or keys & {
        "recon_date",
        "candidate_id",
        "signature",
        "code_location",
        "interfaces",
    }:
        return True
    if interfaces:
        parameter_names = {
            str(parameter.get("name"))
            for interface in interfaces
            for parameter in interface.get("params", [])
            if isinstance(parameter, dict) and parameter.get("name")
        }
        return bool(keys & parameter_names)
    return False


def _derive_testpoints(
    requirements: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 Claim 而非 REQ 聚合粒度派生测试点，并保留每条声明的接口归属。"""
    interface_ids = {str(item.get("id", "")) for item in interfaces}
    requirement_by_id = {str(item.get("id", "")): item for item in requirements}
    points: list[dict[str, Any]] = []

    def append_point(
        requirement_id: str,
        interface_id: str | None,
        source: str,
        scenario: str,
        hints: list[str],
        source_ref: str,
        claim: dict[str, Any] | None = None,
    ) -> None:
        claim_id = str((claim or {}).get("claim_id", "")).strip()
        raw_criteria = (claim or {}).get("acceptance_criteria", [])
        if not isinstance(raw_criteria, list):
            raw_criteria = [raw_criteria]
        acceptance_criteria = [str(value) for value in raw_criteria if str(value).strip()]
        points.append(
            {
                "id": f"TP-{len(points) + 1:03d}",
                "requirement_id": requirement_id,
                **({"claim_id": claim_id} if claim_id else {}),
                **(
                    {"claim_statement": str(claim.get("statement", ""))}
                    if claim and claim.get("statement")
                    else {}
                ),
                **({"acceptance_criteria": acceptance_criteria} if acceptance_criteria else {}),
                **({"interface_id": interface_id} if interface_id else {}),
                "source": source,
                "source_ref": source_ref,
                "scenario": scenario,
                "case_split_hints": hints,
            }
        )

    requirements_with_mapping: set[str] = set()
    for mapping in mappings:
        requirement_id = str(mapping.get("requirement_id", ""))
        if requirement_id not in requirement_by_id:
            continue
        requirements_with_mapping.add(requirement_id)
        # 一个需求可映射多个接口：逐接口派生测试点，避免只覆盖首个接口（C-12）。
        mapped_ids = [str(value) for value in (mapping.get("interface_ids") or [])]
        if not mapped_ids:
            legacy = str(mapping.get("interface_id", ""))
            if legacy:
                mapped_ids = [legacy]
        valid_ids = [value for value in mapped_ids if value in interface_ids]
        requirement = requirement_by_id[requirement_id]
        title = str(requirement.get("title", requirement_id))
        claims = requirement.get("claims")
        if not isinstance(claims, list) or not claims:
            claims = [
                {
                    "statement": str(requirement.get("description", "")).strip()
                    or title
                }
            ]
        assessment_by_claim = {
            str(item.get("claim_id", "")): item
            for item in mapping.get("claim_assessments", [])
            if isinstance(item, dict) and item.get("claim_id")
        }
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            claim_id = str(claim.get("claim_id", ""))
            assessment = assessment_by_claim.get(claim_id, {})
            claim_interface_ids = [
                str(value)
                for value in assessment.get("interface_ids", [])
                if str(value) in interface_ids
            ] or valid_ids
            statement = str(claim.get("statement", "")).strip() or title
            criteria = claim.get("acceptance_criteria", [])
            if not isinstance(criteria, list):
                criteria = [str(criteria)]
            criteria = [str(value).strip() for value in criteria if str(value).strip()]
            criteria_note = f"；验收条件：{'；'.join(criteria)}" if criteria else ""
            status = str(assessment.get("status", mapping.get("status", "")))
            if claim_interface_ids:
                for interface_id in claim_interface_ids:
                    append_point(
                        requirement_id,
                        interface_id,
                        "REQ",
                        f"验证需求声明“{statement}”{criteria_note}",
                        ["按声明验证正常结果", *criteria, "验证该接口对声明结果的响应"],
                        f"声明 {claim_id or requirement_id} ↔ 接口 {interface_id}",
                        claim,
                    )
                    if status in {"partial", "missing", "undetermined"}:
                        append_point(
                            requirement_id,
                            interface_id,
                            "RISK",
                            f"复核声明“{statement}”在代码评审状态 {status} 下的行为",
                            ["核对声明对应分支", "检查结果与验收条件一致", "确认异常或降级行为"],
                            f"声明 {claim_id or requirement_id} 评审状态：{status}",
                            claim,
                        )
            else:
                append_point(
                    requirement_id,
                    None,
                    "RISK",
                    f"需求声明“{statement}”尚未关联接口，需由页面流程或人工确认{criteria_note}",
                    ["端到端主路径", "用户可见反馈", "实现与声明的可追溯性"],
                    f"声明 {claim_id or requirement_id} 无接口映射",
                    claim,
                )
    for requirement_id, requirement in requirement_by_id.items():
        if requirement_id in requirements_with_mapping:
            continue
        claims = requirement.get("claims")
        if isinstance(claims, list) and claims:
            for claim in claims:
                if not isinstance(claim, dict):
                    continue
                claim_id = str(claim.get("claim_id", ""))
                statement = str(claim.get("statement", "")).strip() or str(
                    requirement.get("title", requirement_id)
                )
                criteria = claim.get("acceptance_criteria", [])
                if not isinstance(criteria, list):
                    criteria = [str(criteria)]
                criteria_note = (
                    f"；验收条件：{'；'.join(str(value) for value in criteria if str(value).strip())}"
                    if criteria
                    else ""
                )
                append_point(
                    requirement_id,
                    None,
                    "RISK",
                    f"需求声明“{statement}”没有需求映射记录，需经页面流程或人工确认{criteria_note}",
                    ["端到端主路径", "用户可见反馈", "实现与声明的可追溯性"],
                    f"声明 {claim_id or requirement_id} 无需求映射记录",
                    claim,
                )
        else:
            append_point(
                requirement_id,
                None,
                "RISK",
                f"需求“{requirement.get('title', requirement_id)}”尚未映射到接口，需通过页面流程或人工确认实现证据",
                ["端到端主路径", "用户可见反馈", "实现与需求的可追溯性"],
                f"需求 {requirement_id} 无接口映射",
            )
    return points


def _testpoint_context(
    points: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """构造补全 hints 用的最小上下文：需求原文 + 接口定义（含 fields）。

    只带模型判断拆分维度必需的信息：需求描述说明「验证什么」，接口参数说明
    「用什么取值测」。刻意不含用例字段，避免模型越界直接生成用例。
    """
    requirement_by_id = {str(item.get("id", "")): item for item in requirements}
    interface_by_id = {str(item.get("id", "")): item for item in interfaces}
    context: list[dict[str, Any]] = []
    for point in points:
        requirement = requirement_by_id.get(str(point.get("requirement_id", "")), {})
        interface = interface_by_id.get(str(point.get("interface_id") or ""))
        context.append(
            {
                "testpoint_id": str(point.get("id", "")),
                "source": str(point.get("source", "")),
                **({"claim_id": str(point["claim_id"])} if point.get("claim_id") else {}),
                **(
                    {"claim_statement": str(point["claim_statement"])}
                    if point.get("claim_statement")
                    else {}
                ),
                **(
                    {"acceptance_criteria": list(point["acceptance_criteria"])}
                    if point.get("acceptance_criteria")
                    else {}
                ),
                "requirement": {
                    "id": str(requirement.get("id", point.get("requirement_id", ""))),
                    "title": str(requirement.get("title", "")),
                    "description": str(requirement.get("description", "")),
                },
                "interface": (
                    {
                        "id": str(interface.get("id", "")),
                        "signature": interface.get("signature", {}),
                        "params": interface.get("params") or [],
                    }
                    if interface
                    else None
                ),
                "scenario": str(point.get("scenario", "")),
            }
        )
    return context


async def _enrich_hints_batch(
    llm: SupportsComplete,
    prompt: PromptSpec,
    batch: list[dict[str, Any]],
    limiter: asyncio.Semaphore,
) -> dict[str, list[str]]:
    """补全单批测试点的拆分维度；本批失败返回空 dict（该批退回模板 hints）。

    失败绝不上抛：拆分维度是增强项，任何一批失败都只影响本批测试点，不得中断节点。
    收窄重试上限为 `_TP_HINTS_MAX_ATTEMPTS`——本调用失败即优雅退化，按默认 3 次
    尝试白等 3×超时+25s 不划算（见常量注释）。

    除 PipelineError 外还兜住任意异常：`LlmClient._request_once` 只把
    TimeoutException/TransportError 包装成 PipelineError，其余 httpx 异常会原样上抛；
    而 `asyncio.gather` 默认不隔离失败，一批抛出会让其他批已补全的 hints 一起丢失。
    与「辅助调用绝不拖垮节点」的契约对齐，这里统一收敛为「本批退回模板 hints」。
    """
    async with limiter:
        try:
            # 以子阶段名留痕（同 requirement_review 之于 req_parse 的先例）：
            # 让 llm_calls.jsonl 能区分「拆分维度补全」与「用例生成」两类调用。
            result = await llm.complete(
                _TP_HINTS_PROMPT,
                prompt,
                json.dumps({"batch": {"count": len(batch)}, "items": batch}, ensure_ascii=False),
                max_attempts=_TP_HINTS_MAX_ATTEMPTS,
            )
            return _parse_testpoint_hints(result.text, batch)
        except PipelineError as exc:
            logger.warning(
                "测试点拆分维度补全调用失败，本批 %d 个测试点沿用模板 hints：%s",
                len(batch),
                exc.message,
            )
        except Exception:
            # 非预期异常也退化为模板 hints，但带栈输出以便定位（不静默吞掉）。
            logger.warning(
                "测试点拆分维度补全本批异常，%d 个测试点沿用模板 hints",
                len(batch),
                exc_info=True,
            )
        return {}


async def _enrich_testpoint_hints(
    llm: SupportsComplete,
    prompts_dir: Path,
    points: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """用模型为每个测试点补全专属拆分维度，失败时保留程序模板 hints。

    拆分维度是纯输入侧细化，不参与产物 Schema：任何批次失败都退化为模板 hints，
    绝不能因为这一次调用失败让整个用例生成节点失败或产出为空。

    批次之间互不依赖（各批只按自己的 testpoint_id 回填），故有界并发执行：串行会让
    墙钟等于各批之和（实测 11 批串行 1h59m）。并发度用信号量兜住，不做无上限并发，
    避免中转站限流导致各批互相挤占、集体超时。
    """
    if not points:
        return points
    try:
        prompt = load_prompt(prompts_dir, _TP_HINTS_PROMPT)
    except PipelineError as exc:
        logger.warning("测试点拆分维度 Prompt 加载失败，沿用模板 hints：%s", exc.message)
        return points

    context = _testpoint_context(points, requirements, interfaces)
    batches = chunk_json_items(context, _TP_HINTS_MAX_CHARS)
    limiter = asyncio.Semaphore(_TP_HINTS_CONCURRENCY)
    parsed_batches = await asyncio.gather(
        *(_enrich_hints_batch(llm, prompt, batch, limiter) for batch in batches)
    )
    hints_by_id: dict[str, list[str]] = {}
    for parsed in parsed_batches:
        hints_by_id.update(parsed)
    if not hints_by_id:
        logger.warning(
            "测试点拆分维度补全全部失败（%d 批），%d 个测试点沿用模板 hints",
            len(batches),
            len(points),
        )
        return points
    enriched: list[dict[str, Any]] = []
    for point in points:
        hints = hints_by_id.get(str(point.get("id", "")))
        enriched.append({**point, "case_split_hints": hints} if hints else point)
    return enriched


def _parse_testpoint_hints(text: str, batch: list[dict[str, Any]]) -> dict[str, list[str]]:
    """解析补齐结果：只接受本批 testpoint_id 的合法字符串数组，其余静默丢弃。

    模型漏项、改号或返回空数组时该测试点保留模板 hints——拆分维度是增强项，
    不因模型输出残缺而中断节点，故这里对不合格条目逐个忽略而非整体报错。
    """
    try:
        data = json.loads(strip_code_fence(text))
    except json.JSONDecodeError:
        return {}
    entries = data.get("hints") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return {}
    allowed = {str(item["testpoint_id"]) for item in batch}
    parsed: dict[str, list[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        point_id = str(entry.get("testpoint_id", ""))
        if point_id not in allowed or point_id in parsed:
            continue
        raw_hints = entry.get("case_split_hints")
        if not isinstance(raw_hints, list):
            continue
        hints = [hint.strip() for hint in raw_hints if isinstance(hint, str) and hint.strip()]
        if hints:
            parsed[point_id] = hints
    return parsed


async def _generate_e2e_cases(
    llm: SupportsComplete,
    prompts_dir: Path,
    requirements: list[dict[str, Any]],
    testpoints: list[dict[str, Any]],
    surface: dict[str, Any],
    auth_mode: str = "browser",
) -> tuple[list[dict[str, Any]], list[str]]:
    """Compatibility entry point delegates to the requirement-based v2 planner."""
    from executor.e2e.planner import generate_cases

    cases, design = await generate_cases(
        llm, prompts_dir, requirements, surface, {"auth_mode": auth_mode}
    )
    return cases, [str(gap) for gap in design.get("gaps", [])]


async def _inspect_e2e_surface(
    base_url: str,
    ignore_https_errors: bool,
    auth: dict[str, Any] | None = None,
    options: ProbeOptions | None = None,
) -> dict[str, Any]:
    """探测前端可操作面（委托 e2e_surface 模块，见其模块文档）。

    保持原签名兼容（新增可选参数 options）；返回结构不变——仍是
    ``{"base_url", "path_basis", "routes"}``，白名单消费方无需改动。
    """
    result = await probe_surface(base_url, ignore_https_errors, auth, options)
    surface = result.to_surface(base_url)
    # 探测告警挂在返回值上：调用方原以 surface["routes"] 为空判定 X-E2E-01，
    # 这里追加 warnings 键不改变该判定，只让深层探测的降级原因可被上报。
    if result.warnings:
        surface["warnings"] = result.warnings
    return surface

def _parse_e2e_cases(
    text: str,
    allowed_selectors: set[str] | None = None,
    allowed_paths: set[str] | None = None,
    allowed_testpoints: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """严格收敛 E2E 动作白名单，避免模型产生任意 JS 或危险浏览器操作。

    校验分两级，粒度不同：

    - **整体结构**错误（非 JSON、定位不到 cases 数组）→ 抛 X-LLM-02，交修复轮重出；
    - **单条用例**问题（缺字段、动作不在白名单、selector/路由未探测到）→ **丢弃该条**
      并记录原因。

    白名单的语义是「不执行未验证的动作」，丢弃该条即已达成；为一条坏用例让整个节点
    失败会连带打断下游全部节点（实测：一条 `goto: /home` 使 4 个下游节点降级）。

    Returns:
        (可用的 E2E 用例, 被丢弃用例的原因列表)
    """
    try:
        payload = json.loads(strip_code_fence(text))
    except json.JSONDecodeError as exc:
        raise PipelineError(
            code=X_LLM_02, message=f"E2E 用例输出不是合法 JSON：{exc}", node_id=NODE_NAME
        ) from exc
    # 不同模型常把同一集合命名为 e2e_cases/items/results，或直接输出数组。
    # 仅归一化容器名；条目字段、动作和真实 DOM 白名单仍在下方严格校验。
    entries: Any
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = next(
            (
                payload.get(key)
                for key in ("cases", "e2e_cases", "items", "results")
                if isinstance(payload.get(key), list)
            ),
            None,
        )
        if entries is None and isinstance(payload.get("data"), dict):
            data = payload["data"]
            entries = next(
                (
                    data.get(key)
                    for key in ("cases", "e2e_cases", "items")
                    if isinstance(data.get(key), list)
                ),
                None,
            )
    else:
        entries = None
    if not isinstance(entries, list):
        raise PipelineError(
            code=X_LLM_02,
            message="E2E 用例输出缺少 cases 数组（或结构不符）",
            node_id=NODE_NAME,
        )
    allowed_actions = {"goto", "click", "fill", "check", "select", "expect_text", "expect_url"}
    cases: list[dict[str, Any]] = []
    dropped: list[str] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            dropped.append(f"第 {index} 条不是对象")
            continue
        missing = [
            key
            for key in ("requirement_id", "title", "expected")
            if not (isinstance(entry.get(key), str) and entry[key].strip())
        ]
        if missing:
            dropped.append(f"第 {index} 条缺少{'/'.join(missing)}")
            continue
        copied = dict(entry)
        if allowed_testpoints is not None:
            requirement_id = str(entry.get("requirement_id", ""))
            point_id = str(entry.get("testpoint_id", ""))
            point = allowed_testpoints.get(point_id)
            if point is None and not point_id:
                matching = [
                    item
                    for item in allowed_testpoints.values()
                    if str(item.get("requirement_id", "")) == requirement_id
                ]
                if len(matching) == 1:
                    point = matching[0]
                    point_id = str(point.get("id", ""))
            if point is None or str(point.get("requirement_id", "")) != requirement_id:
                dropped.append(f"第 {index} 条缺少有效的需求测试点溯源")
                continue
            copied["testpoint_id"] = point_id
            if point.get("claim_id"):
                copied["claim_id"] = str(point["claim_id"])
        steps = entry.get("steps")
        if not isinstance(steps, list) or not steps:
            dropped.append(f"第 {index} 条缺少步骤")
            continue
        normalized_steps: list[dict[str, str]] = []
        rejection = ""
        for step in steps:
            if not isinstance(step, dict) or str(step.get("action", "")) not in allowed_actions:
                rejection = "含不支持的浏览器动作"
                break
            action = str(step["action"])
            value = step.get("value")
            selector = step.get("selector")
            if action == "goto":
                if not isinstance(value, str) or not value.startswith("/"):
                    rejection = "goto 仅允许站内相对路径"
                    break
                if allowed_paths is not None and value not in allowed_paths:
                    rejection = f"goto 不是已探测路由：{value}"
                    break
                normalized_steps.append({"action": action, "value": value})
            elif action == "expect_url":
                # URL 断言不定位单个元素；Prompt 明确允许 selector 为空。
                if not isinstance(value, str) or not value.startswith("/"):
                    rejection = "expect_url 仅允许站内相对路径"
                    break
                normalized_steps.append({"action": action, "value": value})
            elif not isinstance(selector, str) or not selector.strip():
                rejection = "步骤缺少 selector"
                break
            elif allowed_selectors is not None and selector not in allowed_selectors:
                rejection = f"selector 未在真实页面探测结果中出现：{selector}"
                break
            else:
                normalized = {"action": action, "selector": selector}
                if value is not None:
                    normalized["value"] = str(value)
                normalized_steps.append(normalized)
        if rejection:
            title = str(entry.get("title", "")).strip()
            prefix = f"第 {index} 条（{title}）" if title else f"第 {index} 条"
            dropped.append(f"{prefix}：{rejection}")
            continue
        copied["steps"] = normalized_steps
        cases.append(copied)
    return cases, dropped


def _expand_long_markers(value: Any) -> Any:
    """递归展开 `__LONG_<n>__` 紧凑记号为真实长度的字符串。

    只认「整个值恰好是记号」的形态（前后不留别的文本），避免把普通字符串误伤；
    n 超出 `_LONG_MARKER_MAX` 时原样保留（宁可不展开，也不让产物被撑爆）。
    展开结果用固定填充字符 `x`——边界用例校验的是「长度超限」这一契约，不是具体内容。
    """
    if isinstance(value, str):
        match = _LONG_MARKER_RE.match(value.strip())
        if match is None:
            return value
        length = int(match.group(1))
        return "x" * length if 0 < length <= _LONG_MARKER_MAX else value
    if isinstance(value, dict):
        return {key: _expand_long_markers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_long_markers(item) for item in value]
    return value


def _parse_llm_cases(text: str, interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """解析 LLM 输出 cases：结构校验 + interface_id 虚构防护。

    Raises:
        PipelineError: X-LLM-02——不可解析 / 缺 cases / 虚构 interface_id / 非法 category。
    """
    cleaned = strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise PipelineError(
            code=X_LLM_02,
            message=f"LLM 输出不是合法 JSON：{exc}",
            node_id=NODE_NAME,
        ) from exc
    raw_entries = _normalize_cases_container(data)
    if raw_entries is None:
        raise PipelineError(
            code=X_LLM_02,
            message=f"LLM 输出缺少 cases 数组（或结构不符；{_safe_json_shape(data)}）",
            node_id=NODE_NAME,
        )
    if_ids = {str(i.get("id", "")) for i in interfaces}
    cases: list[dict[str, Any]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise PipelineError(
                code=X_LLM_02,
                message="LLM 输出的 cases 含非对象条目",
                node_id=NODE_NAME,
            )
        if_id = str(raw.get("interface_id", ""))
        if if_id not in if_ids:
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 引用了不存在的接口：{if_id}（禁止虚构）",
                node_id=NODE_NAME,
            )
        category = str(raw.get("category", ""))
        if category not in _VALID_CATEGORIES:
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 输出了非法 category：{category}（三类之外）",
                node_id=NODE_NAME,
            )
        entry = dict(raw)
        # F-GEN-04 溯源扩展：testpoint_id 为可选增强字段。虚构编号由下游
        # _referenced_testpoint_ids 按本批 testpoints 过滤，此处只做归一化，
        # 不因可选字段异常而中断整批（与 interface_id 的硬校验分层）。
        testpoint_id = str(raw.get("testpoint_id", "") or "").strip()
        if testpoint_id:
            entry["testpoint_id"] = testpoint_id
        else:
            entry.pop("testpoint_id", None)
        # 断言不能在这里硬失败：后续有源码 oracle 时，以 oracle 的已验证断言
        # 替换模型输出；没有 oracle 时则移除断言并将用例标为 unverified。
        # 否则一个模型误写的 op（例如不支持的 `empty`）会阻断整批及检查点续跑。
        if "assertions" in raw:
            entry["assertions"] = raw["assertions"]
        # 超长字符串记号展开：把「数 1025 个字符」从模型侧移回程序侧（见 _LONG_MARKER_RE 注释）。
        # 放在参数一致性校验之前，确保后续校验/渲染/执行拿到的是真实字面量而非记号。
        if "params" in entry:
            entry["params"] = _expand_long_markers(entry["params"])
        cases.append(entry)
    return cases


def _normalize_cases_container(data: Any) -> list[Any] | None:
    """提取已登记容器形态的用例数组；无法无歧义定位时返回 None（宁停不猜）。

    部分兼容网关会把嵌套 JSON 序列化成字符串；仅在已知案例容器字段下解码该字符串，
    不从任意文本猜测数组，避免把模型回显的需求/接口清单误当测试用例。
    """
    if isinstance(data, list):
        return list(data)
    if not isinstance(data, dict):
        return None
    # 某些模型在当前批次只有一条时会直接返回 case 对象。所需字段齐全且
    # 语义唯一时可无损提升为单元素数组；后续仍校验 interface_id/category。
    if _looks_like_case_entry(data):
        return [data]
    for key in _CASES_CONTAINER_KEYS:
        entries = _decode_case_entries(data.get(key))
        if entries is not None:
            return entries
    for key in _CASES_WRAPPER_KEYS:
        nested = _decode_wrapper(data.get(key))
        if isinstance(nested, (dict, list)):
            inner = _normalize_cases_container(nested)
            if inner is not None:
                return inner
    return None


def _looks_like_case_entry(value: dict[str, Any]) -> bool:
    """仅识别完整的单条接口用例对象，避免把任意模型对象误当 cases。"""
    return (
        isinstance(value.get("interface_id"), str)
        and isinstance(value.get("category"), str)
        and isinstance(value.get("title"), str)
        and isinstance(value.get("params"), dict)
        and isinstance(value.get("expected"), str)
    )


def _decode_case_entries(value: Any) -> list[Any] | None:
    """将已知 cases 字段的裸数组或 JSON 字符串数组归一化。"""
    if isinstance(value, list):
        return list(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return list(parsed) if isinstance(parsed, list) else None


def _decode_wrapper(value: Any) -> dict[str, Any] | list[Any] | None:
    """解码已知响应包裹层中的 JSON 对象/数组，不处理任意业务字段。"""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(strip_code_fence(value))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _safe_json_shape(data: Any) -> str:
    """仅提供结构诊断，不记录或暴露模型返回内容。"""
    if isinstance(data, dict):
        keys = [str(key) for key in data.keys()][:12]
        return f"顶层字段：{', '.join(keys) or '<空对象>'}"
    if isinstance(data, list):
        return "顶层为数组，但条目结构不符合要求"
    return f"顶层类型：{type(data).__name__}"


def _filter_param_violations(
    cases: list[dict[str, Any]], interfaces: list[dict[str, Any]],
    test_oracles: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """R-GEN-02：参数名、必填项和 unknown 容器均与接口定义一致。

    Returns:
        (通过校验的用例, 违规记录清单[{interface_id, params, message}]).
    """
    interface_by_id = {str(interface.get("id", "")): interface for interface in interfaces}
    valid: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for case in cases:
        if_id = str(case.get("interface_id", ""))
        interface = interface_by_id.get(if_id, {})
        params = case.get("params")
        boundaries = [oracle for oracle in test_oracles or []
                      if oracle.get("interface_id") == if_id and oracle.get("oracle_basis") == "spring_required_body"
                      and (not case.get("oracle_id") or case.get("oracle_id") == oracle.get("oracle_id"))]
        if (case.get("category") == "boundary" and isinstance(params, dict) and len(boundaries) == 1
                and all(params.get(str(condition["path"])) is None
                        for condition in boundaries[0].get("preconditions") or [])):
            boundary = boundaries[0]
            case = {**case, "oracle_id": boundary["oracle_id"], "oracle_status": "verified",
                    "oracle_basis": "spring_required_body", "request_conditions": boundary["preconditions"]}
        normalized = materialize_declared_defaults(
            _normalize_coded_case_values(
                _normalize_single_body_alias(case, interface), interface
            ),
            interface,
        )
        issues = inspect_case_parameters(normalized, interface)
        messages = issues.messages(
            allow_missing_for_error=expected_client_error(str(normalized.get("expected", "")))
        )
        messages.extend(_coded_value_issues(normalized, interface))
        if messages:
            if issues.invalid_structured:
                messages = [
                    message.replace(
                        "具体 JSON 对象", "禁止语义占位符，必须是具体 JSON 对象"
                    )
                    for message in messages
                ]
            violations.append(
                {
                    "kind": "parameter",
                    "interface_id": if_id,
                    "params": normalized.get("params"),
                    "message": "；".join(messages),
                }
            )
        else:
            valid.append(normalized)
    return valid, violations


def _normalize_single_body_alias(
    case: dict[str, Any], interface: dict[str, Any]
) -> dict[str, Any]:
    """Map a generic LLM `body` key to the sole declared body parameter when unambiguous."""
    raw_params = case.get("params")
    if not isinstance(raw_params, dict) or "body" not in raw_params:
        return case
    definitions = [
        item for item in interface.get("params", [])
        if isinstance(item, dict) and item.get("in") == "body" and item.get("name")
    ]
    if len(definitions) != 1:
        return case
    declared_name = str(definitions[0]["name"])
    declared_names = {
        str(item.get("name", ""))
        for item in interface.get("params", []) if isinstance(item, dict)
    }
    if declared_name == "body" or "body" in declared_names or declared_name in raw_params:
        return case
    normalized = dict(case)
    params = dict(raw_params)
    params[declared_name] = params.pop("body")
    normalized["params"] = params
    return normalized


def _filter_oracle_violations(
    cases: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
    test_oracles: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """用源码 oracle 定稿预期；无 oracle 时保留用例但移除不可执行断言。"""
    interface_ids = {str(item.get("id", "")) for item in interfaces}
    oracle_by_id = {
        str(item.get("oracle_id", "")): item
        for item in test_oracles if item.get("oracle_id")
    }
    valid: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for raw_case in cases:
        case = dict(raw_case)
        interface_id = str(case.get("interface_id", ""))
        if interface_id not in interface_ids:
            continue
        oracle_id = str(case.get("oracle_id", "") or "")
        assertion_error: str | None = None
        try:
            assertions = normalize_response_assertions(case.get("assertions"))
        except ValueError as exc:
            # LLM assertions are untrusted. A bad op must not abort the whole batch
            # before we can replace it with a verified oracle or mark it unverified.
            assertions = []
            assertion_error = str(exc)
        legacy_assertions = legacy_response_assertions(str(case.get("expected", "")))

        def mark_unverified(message: str | None = None) -> None:
            case["expected"] = "待源码预期确认"
            case.pop("assertions", None)
            case.pop("oracle_id", None)
            case.pop("oracle_evidence_refs", None)
            case.pop("oracle_scenario", None)
            case.pop("oracle_control_kinds", None)
            for metadata_key in ("state_conditions", "execution_blockers", "execution_readiness", "request_conditions", "expected_behavior", "oracle_basis", "assertion_scope", "design_status"):
                case.pop(metadata_key, None)
            case["oracle_status"] = "unverified"
            valid.append(case)
            if message:
                violations.append({
                    "kind": "oracle",
                    "interface_id": interface_id,
                    "params": case.get("params"),
                    "message": message,
                })

        if not oracle_id:
            message = None
            if assertion_error:
                message = (
                    f"响应断言格式不合法（{assertion_error}）；没有源码 oracle，"
                    "已移除断言并标记待确认"
                )
            elif assertions or legacy_assertions:
                message = (
                    "含响应断言但未引用 Code Review 的 oracle_id；"
                    "已移除断言并标记待确认"
                )
            mark_unverified(message)
            continue

        oracle = oracle_by_id.get(oracle_id)
        if oracle is None:
            mark_unverified("oracle_id 不存在于本次 Code Review 预期契约；已移除预期并标记待确认")
            continue
        elif str(oracle.get("interface_id", "")) != interface_id:
            mark_unverified("oracle_id 属于其它接口；已移除预期并标记待确认")
            continue
        elif str(oracle.get("category", "")) != str(case.get("category", "")):
            mark_unverified("oracle 场景类别与用例类别不一致；已移除预期并标记待确认")
            continue

        try:
            expected_assertions = normalize_response_assertions(oracle.get("assertions"))
            preconditions = normalize_response_assertions(oracle.get("preconditions"), label="preconditions")
        except ValueError as exc:
            mark_unverified(f"Code Review oracle 格式不合法：{exc}；已移除预期并标记待确认")
            continue

        params = case.get("params") if isinstance(case.get("params"), dict) else {}
        if "observed_params" in oracle:
            if params != oracle["observed_params"]:
                mark_unverified("运行观测契约仅支持已观测的精确请求；参数变体需另行证明")
                continue
            case["title"] = str(oracle.get("scenario", "运行实测场景"))
            for trace_key in ("requirement_id", "claim_id", "testpoint_id", "testpoint_ids"):
                case.pop(trace_key, None)
        outcomes = evaluate_response_assertions(params, preconditions)
        if any(item.get("passed") is not True for item in outcomes):
            mark_unverified("用例 params 不满足源码 oracle 的前置条件；已移除预期并标记待确认")
            continue

        repair_reasons: list[str] = []
        if assertion_error:
            repair_reasons.append(f"模型断言格式不合法：{assertion_error}")
        elif _canonical_assertions(assertions) != _canonical_assertions(expected_assertions):
            repair_reasons.append("模型断言与源码 oracle 不一致")
        if str(case.get("expected", "")).strip() != str(oracle.get("expected_status", "")):
            repair_reasons.append("模型状态码与源码 oracle 不一致")

        # The verified Code Review oracle is authoritative. Copying it here is
        # deterministic and cannot invent behavior; it also prevents malformed
        # model operators such as `empty` from discarding an otherwise valid case.
        case["expected"] = str(oracle["expected_status"])
        case["title"] = str(oracle.get("scenario", case.get("title", "")))
        covered_claims = set(oracle.get("covered_claim_ids") or [])
        if str(case.get("claim_id", "")) not in covered_claims or response_assertion_scope(expected_assertions) != "business":
            for trace_key in ("requirement_id", "claim_id", "testpoint_id", "testpoint_ids"):
                case.pop(trace_key, None)
        case["assertions"] = expected_assertions
        case["oracle_status"] = "verified"
        case["oracle_evidence_refs"] = list(oracle.get("evidence_refs") or [])
        case["oracle_scenario"] = str(oracle.get("scenario", ""))
        case["oracle_control_kinds"] = sorted(
            set(oracle.get("control_kinds") or []) | required_control_kinds(oracle)
        )
        if oracle.get("expected_behavior"):
            case["expected_behavior"] = oracle["expected_behavior"]
        case["state_conditions"] = list(oracle.get("state_conditions") or [])
        case["request_conditions"] = preconditions
        if isinstance(oracle.get("fixture_plan"), dict):
            case["fixture_plan"] = oracle["fixture_plan"]
        case["execution_blockers"] = execution_blockers(oracle)
        case["execution_readiness"] = str(oracle.get("execution_readiness", "ready"))
        for key in ("oracle_basis", "assertion_scope", "design_status"):
            if key in oracle:
                case[key] = oracle[key]
        valid.append(case)
        if repair_reasons:
            violations.append({
                "kind": "oracle_repaired",
                "interface_id": interface_id,
                "params": case.get("params"),
                "message": "；".join(repair_reasons),
            })
    return valid, violations


def _canonical_assertions(value: list[dict[str, Any]]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _merge_rounds(
    first: list[dict[str, Any]], second: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """合并两轮用例；同测试点的不同请求参数保留。"""
    seen: set[tuple[str, str, str, str, str]] = set()
    merged: list[dict[str, Any]] = []
    for case in first + second:
        key = _case_dedup_key(case)
        if key in seen:
            continue
        seen.add(key)
        merged.append(case)
    return merged


def _valid_testpoint_ids(testpoints: list[dict[str, Any]]) -> set[str]:
    """本批合法测试点编号集合，用于过滤模型虚构的溯源引用。

    testpoint_id 是可选增强字段：只保留确实存在于输入 testpoints 的编号，
    虚构/越批引用一律剥离（不中断批次），保持与 F-GEN-04「禁止虚构」一致而不误伤。
    """
    return {str(item.get("id", "")) for item in testpoints if item.get("id")}


def _finalize_cases(
    cases: list[dict[str, Any]],
    job_id: str,
    store: ArtifactStore,
    interfaces: list[dict[str, Any]],
    testpoints: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """补全元数据并渲染脚本：TC-### / script_path / loadable / low_confidence。

    Returns:
        (最终用例清单, 是否存在不可加载脚本).
    """
    # unknown 参数接口集合（X-GEN-03 判定基准：接口定义为准，不经 LLM）
    unknown_ifs: set[str] = {
        str(interface.get("id", ""))
        for interface in interfaces
        if any(str(p.get("type")) == "unknown" for p in interface.get("params") or [])
    }
    interface_by_id = {str(item.get("id", "")): item for item in interfaces}
    testpoint_by_id = {
        str(item.get("id", "")): item
        for item in (testpoints or [])
        if item.get("id")
    }
    known_points = set(testpoint_by_id)
    finalized: list[dict[str, Any]] = []
    any_unloadable = False
    for seq, case in enumerate(cases, start=1):
        case_id = f"TC-{seq:03d}"
        entry: dict[str, Any] = {
            "id": case_id,
            "interface_id": case["interface_id"],
            "category": case["category"],
            "title": str(case.get("title", f"{case['interface_id']} {case['category']} 用例")),
            "expected": str(case.get("expected", "")),
        }
        # F-GEN-04 溯源扩展：只保留合法测试点编号。程序兜底用例天然没有该字段（不伪造）；
        # 模型虚构或跨批引用（不在本批 testpoints 内）在此剥离，不使整批失败。
        testpoint_id = str(case.get("testpoint_id", "") or "")
        if testpoint_id and testpoint_id in known_points:
            entry["testpoint_id"] = testpoint_id
            point = testpoint_by_id[testpoint_id]
            if point.get("requirement_id"):
                entry["requirement_id"] = str(point["requirement_id"])
            if point.get("claim_id"):
                entry["claim_id"] = str(point["claim_id"])
        elif case.get("requirement_id"):
            entry["requirement_id"] = str(case["requirement_id"])
        response_assertions = normalize_response_assertions(case.get("assertions"))
        if not response_assertions:
            response_assertions = legacy_response_assertions(entry["expected"])
        if response_assertions:
            entry["assertions"] = response_assertions
        oracle_status = str(case.get("oracle_status", "unverified"))
        entry["oracle_status"] = oracle_status if oracle_status in {"verified", "unverified"} else "unverified"
        if case.get("oracle_id") and oracle_status == "verified":
            entry["oracle_id"] = str(case["oracle_id"])
        if case.get("oracle_evidence_refs") and oracle_status == "verified":
            entry["oracle_evidence_refs"] = [str(value) for value in case["oracle_evidence_refs"]]
        if case.get("oracle_scenario") and oracle_status == "verified":
            entry["oracle_scenario"] = str(case["oracle_scenario"])
        if case.get("oracle_control_kinds") and oracle_status == "verified":
            entry["oracle_control_kinds"] = [str(value) for value in case["oracle_control_kinds"]]
        if oracle_status == "verified":
            for metadata_key in ("state_conditions", "execution_blockers", "execution_readiness", "request_conditions", "expected_behavior", "oracle_basis", "assertion_scope", "design_status"):
                if metadata_key in case:
                    entry[metadata_key] = case[metadata_key]
        if isinstance(case.get("fixture_plan"), dict) and oracle_status == "verified":
            entry["fixture_plan"] = case["fixture_plan"]
        if isinstance(case.get("params"), dict):
            params = json.loads(json.dumps(case["params"], ensure_ascii=False))
            if isinstance(case.get("fixture_plan"), dict):
                path = str((interface_by_id.get(str(case.get("interface_id", "")), {}).get("signature") or {}).get("path", ""))
                request = params.get("request")
                if isinstance(request, dict) and path.endswith("/update"):
                    request["id"] = "__AUTOTEST_RECORD_ID__"
                elif isinstance(request, dict) and path.endswith("/delete"):
                    request["ids"] = ["__AUTOTEST_RECORD_ID__"]
            entry["params"] = params
        if str(case.get("auth_mode", "")) in {"inherit", "missing", "invalid"}:
            entry["auth_mode"] = str(case["auth_mode"])

        # low_confidence：接口定义含 unknown 参数（X-GEN-03，代码判定），
        # 或程序确定性兜底用例（synthesized，非 LLM 设计的业务场景）
        if case["interface_id"] in unknown_ifs or case.get("synthesized"):
            entry["low_confidence"] = True

        # 脚本渲染（R-GEN-03：py_compile 校验 loadable）
        # 文件名/函数名用下划线形式（test_TC_001.py / test_tc_001）：pytest 可收集
        stem = case_id.replace("-", "_")
        rel_path = f"scripts/test_{stem}.py"
        script = _SCRIPT_TEMPLATE.format(
            case_id=case_id,
            title=entry["title"],
            interface_id=entry["interface_id"],
            requirement_id=entry.get("requirement_id", "-"),
            job_id=job_id,
            params=entry.get("params", {}),
            expected=entry["expected"],
            func_name=f"test_{stem.lower()}",
        )
        store.write_raw(job_id, rel_path, script)
        entry["script_path"] = rel_path
        entry["loadable"] = _py_compile_ok(store.job_dir(job_id) / rel_path, rel_path)
        any_unloadable = any_unloadable or not entry["loadable"]
        finalized.append(entry)
    return finalized, any_unloadable


def _py_compile_ok(path: Path, rel_path: str) -> bool:
    """R-GEN-03：语法校验（py_compile 语义；失败不中断，记 loadable=false）。"""
    try:
        source = path.read_text(encoding="utf-8")
        compile(source, rel_path, "exec")
    except (OSError, SyntaxError, ValueError):
        return False
    return True
