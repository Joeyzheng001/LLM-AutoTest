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
- R-GEN-03：脚本 py_compile，失败记 loadable=false；
- X-GEN-01：接口/场景漏产先进入定向补产；补产仍失败则以 X-LLM-02 阻止残缺产物；
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
from functools import partial
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

from common.artifacts import ArtifactStore
from common.case_validation import (
    expected_client_error,
    inspect_case_parameters,
    materialize_declared_defaults,
)
from common.engine import Engine, JobState
from common.errors import X_LLM_02, X_ORCH_01, PipelineError
from common.llm import LlmResult
from common.llm_batch import chunk_json_items
from common.llm_repair import complete_with_json_repair
from common.llm_text import strip_code_fence
from common.prompts_loader import PromptSpec, load_prompt
from common.response_assertions import (
    legacy_response_assertions,
    normalize_response_assertions,
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

# 实现片段注入：按 code_location 读接口方法体交给模型，让 normal 用例的 expected
# 落在真实分支/字段/返回值上，而不是只凭 signature + params 编。上限既保上下文预算，
# 也避免把整个类塞进请求（单批 3 个接口 × 60 行 ≈ 可控）。
_MAX_IMPLEMENTATION_LINES = 60
_MAX_IMPLEMENTATION_CHARS = 4_000
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
        mappings = _load_json_array(store, state.job_id, "review_mapping.json", "mappings")
        # 测试点是用例设计的中间证据，而不是另一次不稳定的长 JSON 模型输出。
        # 结构（编号/溯源/场景）由程序按需求、接口映射及评审风险确定性派生，
        # 拆分维度（case_split_hints）再由一次轻量模型调用按需求原文与接口字段补全：
        # 通用模板 hints 会让用例退化成「每接口三类」的门禁下限，模型只凑数不展开。
        testpoints = _derive_testpoints(requirements, interfaces, mappings)

        prompt = load_prompt(prompts_dir, NODE_NAME)
        # 以接口为分批边界：每批只携带相关需求与映射，避免用例 JSON 随接口数线性膨胀。
        valid: list[dict[str, Any]] = []
        retry_feedback: list[dict[str, Any]] = []
        # 违规明细按「批次 × 轮次」留痕：两轮都收，既能看出模型第二轮改没改好，
        # 也能在告警复现不出来时区分「模型输出漂移」与「代码路径不同」。
        param_violation_rounds: list[dict[str, Any]] = []
        # Claim 测试点溯源缺口（接口, 未覆盖测试点数）：无论模型是否首轮输出
        # testpoint_id，都尝试一次定向补产；仍缺则告警，不把模型漏溯源伪装成完整覆盖。
        coverage_gaps: list[tuple[str, int]] = []
        # F-GEN-05 展开不足残余（testpoint_id, hints 数, 期望, 实际）：批内补产一次
        # 后仍不足的测试点，节点末尾统一告警。
        expansion_gaps: list[tuple[str, int, int, int]] = []
        # 用例输出随接口数 × 三类场景增长。最多三个接口一批，避免模型只回答
        # 批次首项而把其余接口静默漏掉。
        interface_batches = _chunk_case_interfaces(interfaces)
        # 实现片段（按 code_location 读源码）在构造批上下文时按需注入，不进指纹：
        # 它由仓库文件派生且只读本批接口，纳入指纹需要全量预读，收益不足。
        repo_roots = _load_repo_roots(store, state.job_id)
        # 指纹只覆盖程序确定性的输入（不含模型补全的 hints）：拆分维度每次调用本就
        # 不同，纳入指纹会让任何续跑都判定「输入已变更」而重头生成。
        checkpoint_fingerprint = _generation_fingerprint(
            requirements, interfaces, mappings, testpoints
        )
        checkpoint = _load_generation_checkpoint(store, state.job_id, checkpoint_fingerprint)
        # 续跑复用检查点中的 hints：拆分维度由模型生成、每次调用结果不同，重算会让
        # 已生成批次与新批次基于不同依据，也多付一轮调用成本。
        checkpoint_points = checkpoint.get("testpoints")
        if isinstance(checkpoint_points, list) and checkpoint_points:
            testpoints = [dict(item) for item in checkpoint_points if isinstance(item, dict)]
        else:
            testpoints = await _enrich_testpoint_hints(
                llm, prompts_dir, testpoints, requirements, interfaces
            )
        completed_batches = {
            int(str(item.get("index")))
            for item in checkpoint.get("batches", [])
            if isinstance(item, dict) and str(item.get("index", "")).isdigit()
        }
        if checkpoint:
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
            )
            raw_cases = await _complete_cases_with_coverage_recovery(
                llm, prompt, user_content, batch
            )
            batch_valid, violations = _filter_param_violations(raw_cases, batch)
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
                            "不能使用语义标签或字符串占位符。"
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
                valid2, violations2 = _filter_param_violations(raw_cases2, batch)
                batch_valid = _merge_rounds(batch_valid, valid2)
                # 空 violations2 也记：表示该批第二轮已修好（与被放弃的接口区分开）。
                param_violation_rounds.append(
                    {"batch": index, "round": 2, "violations": violations2}
                )
                retry_feedback.extend(violations2)
            # 参数校验可能过滤掉 LLM 生成但参数违规的用例，导致覆盖再次缺失。
            # 这里用程序确定性兜底补齐，不再把覆盖缺口交回给不稳定的 LLM 重生成。
            missing = _missing_categories(batch_valid, batch)
            if missing:
                batch_valid = _merge_case_entries(
                    batch_valid, _synthesize_fallback_cases(missing)
                )
            # F-GEN-04/P0 溯源覆盖：即便模型首轮完全漏掉 testpoint_id，也定向补产一次；
            # 兜底用例无法伪造 Claim 归属，仍有缺口时明确告警，不宣称完整覆盖。
            uncovered = _uncovered_req_testpoints(batch_valid, testpoints, batch)
            if uncovered:
                batch_valid = await _recover_uncovered_testpoints(
                    llm, prompt, user_content, batch, batch_valid, uncovered
                )
                uncovered = _uncovered_req_testpoints(batch_valid, testpoints, batch)
            if uncovered:
                coverage_gaps.append(
                    (
                        ", ".join(sorted({str(b.get("id", "")) for b in batch})),
                        len(uncovered),
                    )
                )
            # F-GEN-05 展开充分性：与 F-GEN-04 同层做「先修再报」。检测必须在批内
            # （补产机制按批携带 user_content），只对本批接口归属的测试点计量；
            # 补产一次后仍不足的残余进 expansion_gaps，由节点末尾统一告警。
            batch_point_ids = {
                str(point.get("id", ""))
                for point in testpoints
                if str(point.get("interface_id", ""))
                in {str(b.get("id", "")) for b in batch}
            }
            batch_points = [
                point
                for point in testpoints
                if str(point.get("id", "")) in batch_point_ids
            ]
            underexpanded = _underexpanded_testpoints(batch_valid, batch_points)
            if underexpanded:
                batch_valid = await _recover_underexpanded_testpoints(
                    llm,
                    prompt,
                    user_content,
                    batch,
                    batch_valid,
                    underexpanded,
                    {str(p.get("id", "")): p for p in batch_points},
                )
                underexpanded = _underexpanded_testpoints(batch_valid, batch_points)
            expansion_gaps.extend(underexpanded)
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
        abandoned = sorted({v["interface_id"] for v in retry_feedback})
        for interface_id in abandoned:
            detail = _summarize_violations(retry_feedback, interface_id)
            warnings.append(
                f"X-GEN-02: 接口 {interface_id} 参数一致性连续 2 次不通过，"
                f"该接口用例放弃（生成质量告警）：{detail}"
            )
        if abandoned:
            valid = [c for c in valid if c["interface_id"] not in set(abandoned)]
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
        points_by_id = {str(point.get("id", "")): point for point in testpoints}
        status_only_claim_cases = [
            case
            for case in cases
            if case.get("category") == "normal"
            and points_by_id.get(str(case.get("testpoint_id", "")), {}).get("source") == "REQ"
            and not case.get("assertions")
            and not legacy_response_assertions(str(case.get("expected", "")))
        ]
        if status_only_claim_cases:
            warnings.append(
                f"P0-assertion: {len(status_only_claim_cases)} 条需求主流程接口用例没有可执行的响应字段断言，"
                "当前只能判定 HTTP 状态；需补充字段级 assertions 后才能作为业务结果通过。"
            )
        # F-GEN-05 展开不足：批内已做过一次定向补产（与 F-GEN-04 对称），这里只
        # 告警补产后仍不足的残余。不在合并结果上重新检测——跨批接口归属不同，
        # 重测会把批内已补齐的点按全局口径重复计量。
        if expansion_gaps:
            detail = "; ".join(
                f"{tid}（{hint_count}维度→期望≥{expected}，实际{actual}）"
                for tid, hint_count, expected, actual in expansion_gaps
            )
            warnings.append(
                f"F-GEN-05: {len(expansion_gaps)} 个需求测试点挂了多条拆分维度却展开不足"
                f"（已按维度清单定向补产一次仍未达标）："
                f"{detail}。请人工确认是维度冗余还是漏展开。"
            )
        e2e_cases: list[dict[str, Any]] = []
        if e2e and bool(e2e.get("generate_cases")):
            # E2E 是可选产物：接口用例已在上方定型，探测或生成失败一律降级为告警，
            # 不让它把本节点打成失败并连带打断下游（曾因一条坏用例导致 4 节点降级）。
            try:
                surface = await _inspect_e2e_surface(
                    str(e2e.get("base_url", "")),
                    bool(e2e.get("insecure_tls", True)),
                    auth,
                    ProbeOptions.from_config(e2e),
                )
                # 深层探测的降级原因（登录失败/未发现新路由）如实上报，便于定位 E2E 覆盖不足
                warnings.extend(surface.get("warnings", []))
                if surface["routes"]:
                    e2e_cases, e2e_warnings = await _generate_e2e_cases(
                        llm,
                        prompts_dir,
                        requirements,
                        testpoints,
                        surface,
                        auth_mode=str(e2e.get("auth_mode", "browser")),
                    )
                    warnings.extend(e2e_warnings)
                else:
                    warnings.append("X-E2E-01: 未探测到可操作的前端页面，跳过 E2E 用例生成")
            except PipelineError as exc:
                logger.warning("E2E 用例生成失败，跳过（不影响接口用例）：%s", exc.message)
                warnings.append(f"X-E2E-02: E2E 用例生成失败已跳过（{exc.code}）：{exc.message}")
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
) -> str:
    """为批次检查点生成输入指纹，防止需求或接口变更时复用旧用例。"""
    payload = {
        # 变更测试点溯源契约时使旧未完成检查点失效，避免续跑复用无 Claim 关联的用例。
        "contract_version": "claim-trace-v2",
        "requirements": requirements,
        "interfaces": interfaces,
        "mappings": mappings,
        "testpoints": testpoints,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    """原子写参数一致性违规明细（内部排查文件，不进产物契约与节点完成判据）。

    无任何违规时不留文件——「文件不存在」本身就是「本节点无参数违规」的明确信号，
    不给每个 job 增加一个空文件。写入失败只告警：告警文本里已带原因，此处是补充。
    """
    if not rounds:
        return
    payload = {
        "schema_version": "1.0",
        "job_id": job_id,
        "note": (
            "内部排查文件（非产物契约）：参数一致性校验（R-GEN-02）的违规明细。"
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
    for repo in inventory.get("repositories") or []:
        if not isinstance(repo, dict):
            continue
        name = str(repo.get("name", ""))
        raw_path = str(repo.get("path", ""))
        if name and raw_path:
            roots[name] = Path(raw_path)
    return roots


def _implementation_excerpt(
    interface: dict[str, Any], repo_roots: dict[str, Path]
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
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        start = int(location.get("start_line", 0))
        raw_end = int(location.get("end_line", start))
    except (TypeError, ValueError):
        return None
    if start < 1 or start > len(lines):
        return None
    end = min(max(raw_end, start), len(lines))
    truncated = False
    if end - start + 1 > _MAX_IMPLEMENTATION_LINES:
        end = start + _MAX_IMPLEMENTATION_LINES - 1
        truncated = True
    excerpt = _SECRET_VALUE_RE.sub(r"\1\2<redacted>", "\n".join(lines[start - 1 : end]))
    if len(excerpt) > _MAX_IMPLEMENTATION_CHARS:
        excerpt = excerpt[:_MAX_IMPLEMENTATION_CHARS]
        truncated = True
    if not excerpt.strip():
        return None
    return {
        "source": "method_body",
        "file": relative,
        "start_line": start,
        "end_line": end,
        "excerpt": excerpt,
        "truncated": truncated,
    }


def _changed_excerpt(interface: dict[str, Any]) -> dict[str, Any] | None:
    """本次**实际改动**的代码片段（#52）：来自接口还原阶段的 target 版本只读快照。

    为什么不改写工作区文件取窗口：工作区与 target 提交可能不是同一版本，按行号取窗口会
    取错行；片段本身已经是权威内容（行号与候选同版本），且已在 java-parser 侧做过
    行数/字符上限。信息密度也更高——直接说明"这次改了哪几行语义"。
    """
    evidence = interface.get("change_evidence")
    if not isinstance(evidence, dict):
        return None
    snippets = evidence.get("changed_snippets")
    if not isinstance(snippets, list):
        return None
    parts: list[str] = []
    files: list[str] = []
    first_line = 0
    last_line = 0
    total = 0
    for item in snippets:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        if not text.strip():
            continue
        header = f"// {item.get('file')}:{item.get('start_line')}-{item.get('end_line')}"
        block = f"{header}\n{text.rstrip()}"
        if total + len(block) > _MAX_IMPLEMENTATION_CHARS:
            break
        parts.append(block)
        total += len(block)
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
        "truncated": bool(evidence.get("truncated")),
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
        excerpt = _implementation_excerpt(interface, repo_roots or {})
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


def _validate_batch_coverage(cases: list[dict[str, Any]], interfaces: list[dict[str, Any]]) -> None:
    """F-GEN-01：每个当前批接口必须有 normal/boundary/exception 三类用例。

    覆盖缺失进入 JSON 修复轮，而不是让节点带着“跳过并记录”的残缺产物完成。
    """
    categories_by_interface: dict[str, set[str]] = {}
    for case in cases:
        interface_id = str(case.get("interface_id", ""))
        categories_by_interface.setdefault(interface_id, set()).add(str(case.get("category", "")))
    missing: list[str] = []
    for interface in interfaces:
        interface_id = str(interface.get("id", ""))
        absent = [
            category
            for category in _VALID_CATEGORIES
            if category not in categories_by_interface.get(interface_id, set())
        ]
        if absent:
            missing.append(f"{interface_id}: {', '.join(absent)}")
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
) -> list[dict[str, Any]]:
    """生成一批用例，并对漏产场景做定向补产。

    模型经常先返回批次中第一个接口，通用 JSON 修复轮只能重复提醒，仍可能
    静默漏掉后续接口。这里保留两轮通用修复；若输出本身可解析但覆盖不全，
    再按接口/场景逐项补产，最后才允许批次通过。补产失败仍抛出原始校验错误，
    不生成残缺 testcases.json。
    """
    first = await llm.complete(NODE_NAME, prompt, user_content)
    first_cases: list[dict[str, Any]] | None = None
    try:
        cases = _parse_llm_cases(first.text, interfaces)
        first_cases = cases
        _validate_batch_coverage(cases, interfaces)
        return cases
    except PipelineError as first_error:
        if first_error.code != X_LLM_02:
            raise
        repair_content = (
            f"{user_content}\n\n"
            "上一轮输出未通过机器校验。请仅输出 JSON，必须覆盖本批每一个接口，"
            "且每个接口恰好包含 normal、boundary、exception 三类用例；"
            "不得遗漏任何接口，不要解释或输出 Markdown。\n"
            f"校验错误：{first_error.message}"
        )
        second = await llm.complete(NODE_NAME, prompt, repair_content)
        try:
            repaired = _parse_llm_cases(second.text, interfaces)
            _validate_batch_coverage(repaired, interfaces)
            return repaired
        except PipelineError as second_error:
            # 只有结构可解析时才进入按缺失项补产；若结构本身损坏，继续沿用
            # 原有 X-LLM-02 口径，避免把不相关的模型输出拼进用例清单。
            try:
                second_cases = _parse_llm_cases(second.text, interfaces)
            except PipelineError:
                if _looks_like_cross_node_payload(second.text, interfaces):
                    # MiniMax 偶尔把接口还原对象（常见特征是 recon_date）原样回显。
                    # 这是节点串产物，不是普通 JSON 容器漂移；追加一次明确的节点隔离
                    # 修复，避免把同一错误在通用修复轮里重复两次。
                    shape_repair = (
                        f"{user_content}\n\n"
                        "上一次输出为空对象或错误地返回了其它节点产物，不是测试用例。"
                        "当前节点只能输出测试用例 JSON：顶层必须只有 cases 数组；"
                        "禁止输出 recon_date、interfaces、candidate_id、signature、code_location，"
                        "也禁止把 pk、close_type 等请求参数提升到根对象；"
                        "也不要解释或输出 Markdown。"
                        "每个输入接口必须包含 normal、boundary、exception。"
                    )
                    third = await llm.complete(NODE_NAME, prompt, shape_repair)
                    try:
                        third_cases = _parse_llm_cases(third.text, interfaces)
                        _validate_batch_coverage(third_cases, interfaces)
                        return third_cases
                    except PipelineError:
                        pass
                # 修复轮输出结构损坏：退回首轮用例 + 程序确定性兜底补齐覆盖，
                # 不因模型输出漂移直接降级。
                # 判据必须是 `is not None` 而非真值：首轮「能解析但零用例」时
                # first_cases == []，用真值判定会把它当成「首轮结构损坏」而跳过兜底，
                # 随后 raise second_error 让整个节点失败（线上实测 IF-REST-003 独占一批）。
                if first_cases is not None:
                    missing = _missing_categories(first_cases, interfaces)
                    if missing:
                        recovered = _merge_case_entries(
                            first_cases, _synthesize_fallback_cases(missing)
                        )
                        try:
                            _validate_batch_coverage(recovered, interfaces)
                            return recovered
                        except PipelineError:
                            pass
                raise second_error from first_error
            merged = _merge_case_entries(first_cases or [], second_cases)
            fallback_missing: list[tuple[dict[str, Any], list[str]]] = []
            for interface, categories in _missing_categories(merged, interfaces):
                targeted_content = (
                    f"{user_content}\n\n"
                    "这是漏产补充请求。只为下面指定的接口和场景输出 JSON，"
                    "禁止输出其它接口、禁止输出其它类别：\n"
                    f"接口：{json.dumps(interface, ensure_ascii=False)}\n"
                    f"必须补齐场景：{', '.join(categories)}\n"
                    '输出格式：{"cases":[{"interface_id":"...","category":"normal|boundary|exception",'
                    '"title":"...","params":{},"expected":"status=..."}]}。'
                )
                targeted = await llm.complete(NODE_NAME, prompt, targeted_content)
                try:
                    additions = _parse_llm_cases(targeted.text, [interface])
                    wanted = [
                        case
                        for case in additions
                        if str(case.get("category", "")) in categories
                    ]
                    covered = {
                        str(case.get("category", "")) for case in wanted
                    } == set(categories)
                except PipelineError:
                    # 定向补产输出损坏/非法类别：同样交由程序确定性兜底，不因模型输出漂移降级
                    wanted = []
                    covered = False
                if covered:
                    merged = _merge_case_entries(merged, wanted)
                else:
                    # 定向补产仍缺：交由程序确定性兜底，不让模型不稳定性导致漏产降级
                    fallback_missing.append((interface, categories))
            if fallback_missing:
                logger.info(
                    "覆盖兜底：%d 个接口缺场景 %s",
                    len(fallback_missing),
                    [(str(i.get("id", "")), c) for i, c in fallback_missing],
                )
                merged = _merge_case_entries(
                    merged, _synthesize_fallback_cases(fallback_missing)
                )
            try:
                _validate_batch_coverage(merged, interfaces)
            except PipelineError as exc:
                logger.warning("兜底后仍漏产：%s", exc.message)
                raise
            return merged


def _missing_categories(
    cases: list[dict[str, Any]], interfaces: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], list[str]]]:
    """返回每个接口仍缺少的场景，保持接口输入顺序。"""
    categories_by_interface: dict[str, set[str]] = {}
    for case in cases:
        categories_by_interface.setdefault(str(case.get("interface_id", "")), set()).add(
            str(case.get("category", ""))
        )
    result: list[tuple[dict[str, Any], list[str]]] = []
    for interface in interfaces:
        interface_id = str(interface.get("id", ""))
        missing = [
            category
            for category in _VALID_CATEGORIES
            if category not in categories_by_interface.get(interface_id, set())
        ]
        if missing:
            result.append((interface, missing))
    return result


def _case_dedup_key(case: dict[str, Any]) -> tuple[str, str, str]:
    """去重键：接口 + 类别 + 测试点。

    逐点展开后同一接口同一类别可以合法地有多条用例（不同测试点验证不同业务规则），
    故必须把 testpoint_id 并入键，否则修复轮合并会把展开结果塌缩回每接口三类。
    无 testpoint_id 的条目（程序兜底）退化为「接口 + 类别」，保持原有去重语义。
    """
    return (
        str(case.get("interface_id", "")),
        str(case.get("category", "")),
        str(case.get("testpoint_id", "")),
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
    referenced = {
        str(case.get("testpoint_id", ""))
        for case in cases
        if str(case.get("testpoint_id", ""))
    }
    return [
        point
        for point in testpoints
        if str(point.get("source", "")) == "REQ"
        and str(point.get("interface_id", "")) in interface_ids
        and str(point.get("id", "")) not in referenced
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
    """F-GEN-05：返回 case_split_hints 维度多、但实际用例数显著偏低的 REQ 测试点。

    解除「每点 1 条、整批 40 条」软封顶后，展开的充分性只写在 prompt 里、无任何
    机器校验——模型若对一个挂了 5 条拆分维度的测试点只产 1 条用例，F-GEN-04 的
    「≥1 条即覆盖」照样放行。本函数按 `case_split_hints` 条数估算期望展开量，
    实际显著偏低时如实告警（不强制补产，交人工确认是维度冗余还是模型偷懒）。

    只统计 `source=REQ` 且挂了 ≥``_EXPANSION_MIN_HINTS`` 条维度的测试点，与
    `_uncovered_req_testpoints` 保持同一覆盖面口径（RISK 点同构、无接口归属属
    E2E/人工范畴，均不在本节点展开校验内）。

    返回 ``[(testpoint_id, hint_count, expected, actual), ...]``。
    """
    counts: dict[str, int] = {}
    for case in cases:
        testpoint_id = str(case.get("testpoint_id", ""))
        if testpoint_id:
            counts[testpoint_id] = counts.get(testpoint_id, 0) + 1
    underexpanded: list[tuple[str, int, int, int]] = []
    for point in testpoints:
        if str(point.get("source", "")) != "REQ":
            continue
        hints = point.get("case_split_hints") or []
        hint_count = len(hints) if isinstance(hints, list) else 0
        if hint_count < _EXPANSION_MIN_HINTS:
            continue
        testpoint_id = str(point.get("id", ""))
        actual = counts.get(testpoint_id, 0)
        # 未被任何用例引用的点归 F-GEN-04 处理，这里不重复告警。
        if actual == 0:
            continue
        expected = math.ceil(hint_count * _EXPANSION_RATIO)
        if actual < expected:
            underexpanded.append((testpoint_id, hint_count, expected, actual))
    return underexpanded


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
        "至少一条用例，并在用例中回填其 testpoint_id；同时仍需满足每个接口"
        "normal/boundary/exception 三类各 ≥1。若源码证据支持，请给 normal 用例提供"
        "机器可执行的响应字段断言 assertions；不要根据接口名臆造返回字段或固定值。"
        "只为下面列出的测试点与接口输出，"
        "禁止输出其它接口：\n"
        f"未覆盖测试点：{json.dumps(uncovered, ensure_ascii=False)}\n"
        '输出格式：{"cases":[{"interface_id":"...","requirement_id":"...",'
        '"testpoint_id":"...","category":"normal|boundary|exception","title":"...",'
        '"params":{},"expected":"status=...","assertions":[{"path":"data.id",'
        '"op":"exists"}]}]}。'
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
    """F-GEN-05：为展开不足的 REQ 测试点按拆分维度做一次定向补产。

    与 F-GEN-04 的差异：缺的不是「有没有用例」而是「维度覆盖够不够」，
    因此补产清单携带每个点的 case_split_hints 原文与已有用例标题——
    要求模型只补**尚未覆盖的维度**，不重复已有场景。
    """
    gaps: list[dict[str, Any]] = []
    for testpoint_id, hint_count, expected, actual in underexpanded:
        point = points_by_id.get(testpoint_id, {})
        existing_titles = [
            str(case.get("title", ""))
            for case in cases
            if str(case.get("testpoint_id", "")) == testpoint_id
        ]
        gaps.append(
            {
                "testpoint_id": testpoint_id,
                "case_split_hints": point.get("case_split_hints") or [],
                "期望用例数": expected,
                "实际用例数": actual,
                "已有用例标题": existing_titles,
            }
        )
    instruction = (
        "这是展开充分性补充请求（F-GEN-05）。下列 REQ 测试点挂了多条拆分维度，"
        "但已产出的用例数显著低于维度数——请对照每个点的 case_split_hints 与"
        "已有用例标题，只为**尚未覆盖的维度**补产用例（每个缺失维度一条），"
        "回填 testpoint_id；禁止重复已有场景、禁止输出清单之外的测试点或接口：\n"
        f"展开不足清单：{json.dumps(gaps, ensure_ascii=False)}\n"
        '输出格式：{"cases":[{"interface_id":"...","requirement_id":"...",'
        '"testpoint_id":"...","category":"normal|boundary|exception","title":"...",'
        '"params":{},"expected":"status=...","assertions":[{"path":"data.id",'
        '"op":"exists"}]}]}。'
    )
    return await _recover_cases_with_targeted_prompt(
        llm,
        prompt,
        user_content,
        interfaces,
        cases,
        instruction,
        wanted_testpoints={row[0] for row in underexpanded},
        log_label="展开充分性",
    )


def _merge_case_entries(
    existing: list[dict[str, Any]], additions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按接口+类别+测试点去重合并，补产结果不覆盖已有完整场景。"""
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
                        ["按声明验证正常结果", *criteria[:3], "验证该接口对声明结果的响应"],
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
    """基于真实页面探测结果生成，禁止模型杜撰路由或 selector。

    未通过白名单校验的单条用例会被丢弃并汇总为告警，不影响其余用例与本节点成败：
    E2E 是可选产物，接口用例已在调用前定型。
    """
    prompt = load_prompt(prompts_dir, "e2e_testcase_generate")
    allowed_selectors = {selector for route in surface["routes"] for selector in route["selectors"]}
    allowed_paths = {route["path"] for route in surface["routes"]}
    point_by_id = {
        str(point.get("id", "")): point for point in testpoints if point.get("id")
    }
    content = json.dumps(
        {
            "surface": surface,
            "requirements": requirements,
            "testpoints": testpoints,
            "max_cases": 12,
            "auth_mode": auth_mode if auth_mode in {"reuse", "browser"} else "browser",
        },
        ensure_ascii=False,
    )
    raw_cases, dropped = await complete_with_json_repair(
        llm,
        "testcase_generate",
        prompt,
        content,
        partial(
            _parse_e2e_cases,
            allowed_selectors=allowed_selectors,
            allowed_paths=allowed_paths,
            allowed_testpoints=point_by_id,
        ),
    )
    result: list[dict[str, Any]] = []
    for sequence, case in enumerate(raw_cases[:12], start=1):
        point = point_by_id.get(str(case.get("testpoint_id", "")), {})
        entry = {
            "id": f"E2E-{sequence:03d}",
            "case_type": "e2e",
            "requirement_id": str(point.get("requirement_id", case["requirement_id"])),
            "testpoint_id": str(case.get("testpoint_id", "")),
            "title": str(case["title"]),
            "preconditions": [str(x) for x in case.get("preconditions", [])],
            "steps": case["steps"],
            "expected": str(case["expected"]),
        }
        if point.get("claim_id"):
            entry["claim_id"] = str(point["claim_id"])
        result.append(entry)
    warnings: list[str] = []
    if dropped:
        preview = "；".join(dropped[:5]) + ("…" if len(dropped) > 5 else "")
        warnings.append(
            f"X-E2E-02: {len(dropped)} 条 E2E 用例未通过白名单校验已丢弃：{preview}"
        )
    return result, warnings


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
        if "assertions" in raw:
            try:
                entry["assertions"] = normalize_response_assertions(raw["assertions"])
            except ValueError as exc:
                raise PipelineError(
                    code=X_LLM_02,
                    message=f"LLM 用例响应断言格式不合法：{exc}",
                    node_id=NODE_NAME,
                ) from exc
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
    cases: list[dict[str, Any]], interfaces: list[dict[str, Any]]
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
        normalized = materialize_declared_defaults(case, interface)
        issues = inspect_case_parameters(normalized, interface)
        messages = issues.messages(
            allow_missing_for_error=expected_client_error(str(normalized.get("expected", "")))
        )
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
                    "interface_id": if_id,
                    "params": normalized.get("params"),
                    "message": "；".join(messages),
                }
            )
        else:
            valid.append(normalized)
    return valid, violations


def _merge_rounds(
    first: list[dict[str, Any]], second: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """合并两轮通过校验的用例（同接口+类别+测试点去重，第一轮优先）。

    去重键含 testpoint_id：两轮可能各覆盖同一接口同一类别下的不同测试点，
    若只按接口+类别去重会把第二轮展开出的用例整体丢弃。
    """
    seen: set[tuple[str, str, str]] = set()
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
        if isinstance(case.get("params"), dict):
            entry["params"] = case["params"]
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
