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
- X-GEN-01：接口零用例（LLM 漏产）→ 告警跳过，其他接口不受影响；
- X-GEN-03：接口含 unknown 参数 → 用例标 low_confidence。

完成判据（docs/12 §1.3）：loadable 全真 → completed；存在不可加载脚本 →
mark_degraded（manual_intervention，D1：不阻塞下游）。零用例 → X-GEN-02 节点失败。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from orchestrator.artifacts import ArtifactStore
from orchestrator.engine import Engine, JobState
from orchestrator.errors import X_LLM_02, X_ORCH_01, PipelineError
from orchestrator.llm import LlmResult
from orchestrator.nodes.req_parse import _strip_code_fence
from orchestrator.prompts_loader import PromptSpec, load_prompt

NODE_NAME = "testcase_generate"

_VALID_CATEGORIES = ("normal", "boundary", "exception")

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

    async def complete(self, node: str, prompt: PromptSpec, user_content: str) -> LlmResult:
        """按协议执行一次补全。"""
        ...


async def run_testcase_generate(
    engine: Engine,
    state: JobState,
    store: ArtifactStore,
    llm: SupportsComplete,
    prompts_dir: Path,
) -> Path:
    """执行 testcase_generate 全流程：读前置 → LLM 两轮 → 校验渲染 → 落盘。

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

        prompt = load_prompt(prompts_dir, NODE_NAME)
        user_content = json.dumps(
            {
                "interfaces": interfaces,
                "requirements": requirements,
                "mappings": [
                    {
                        "requirement_id": m.get("requirement_id"),
                        "interface_id": m.get("interface_id"),
                        "status": m.get("status"),
                    }
                    for m in mappings
                ],
            },
            ensure_ascii=False,
        )
        result = await llm.complete(NODE_NAME, prompt, user_content)
        raw_cases = _parse_llm_cases(result.text, interfaces)

        # R-GEN-02 第一轮：参数一致性过滤
        valid, violations = _filter_param_violations(raw_cases, interfaces)
        retry_feedback: list[dict[str, Any]] = []
        if violations:
            # 带违规反馈重生成一次（R-GEN-02：丢弃重生成）
            feedback = json.dumps(
                {
                    "violations": violations,
                    "note": "以下用例参数名不在接口定义内，请仅用接口定义的参数名重新生成这些用例",
                },
                ensure_ascii=False,
            )
            result2 = await llm.complete(NODE_NAME, prompt, f"{user_content}\n\n{feedback}")
            raw_cases2 = _parse_llm_cases(result2.text, interfaces)
            valid2, violations2 = _filter_param_violations(raw_cases2, interfaces)
            # 合并两轮中通过校验的用例（同接口去重：以第一轮为准补第二轮新增）
            valid = _merge_rounds(valid, valid2)
            retry_feedback = violations2

        warnings: list[str] = []
        # X-GEN-02：二次仍违规的接口——整接口用例放弃（非部分保留）
        abandoned = sorted({v["interface_id"] for v in retry_feedback})
        for interface_id in abandoned:
            warnings.append(
                f"X-GEN-02: 接口 {interface_id} 参数一致性连续 2 次不通过，"
                "该接口用例放弃（生成质量告警）"
            )
        if abandoned:
            valid = [c for c in valid if c["interface_id"] not in set(abandoned)]
        # X-GEN-01：零用例接口跳过并记录
        covered_interfaces = {case["interface_id"] for case in valid}
        for interface in interfaces:
            if_id = str(interface.get("id", ""))
            if if_id and if_id not in covered_interfaces:
                warnings.append(f"X-GEN-01: 接口 {if_id} 未产出用例，跳过并记录")

        if not valid:
            raise PipelineError(
                code="X-GEN-02",
                message="全部接口用例生成失败（参数一致性二次不过或漏产）",
                node_id=NODE_NAME,
                details={"warnings": warnings},
            )

        # 元数据补全 + 脚本渲染（TC-### 编号、模板脚本、loadable、low_confidence）
        cases, any_unloadable = _finalize_cases(valid, state.job_id, store, interfaces)
        if any_unloadable:
            warnings.append("R-GEN-03: 存在不可加载脚本（loadable=false），详见用例清单")

        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "job_id": state.job_id,
            "cases": cases,
        }
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
        return path
    except PipelineError as exc:
        engine.mark_failed(state, NODE_NAME, exc)
        raise


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


def _parse_llm_cases(
    text: str, interfaces: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """解析 LLM 输出 cases：结构校验 + interface_id 虚构防护。

    Raises:
        PipelineError: X-LLM-02——不可解析 / 缺 cases / 虚构 interface_id / 非法 category。
    """
    cleaned = _strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise PipelineError(
            code=X_LLM_02,
            message=f"LLM 输出不是合法 JSON：{exc}",
            node_id=NODE_NAME,
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise PipelineError(
            code=X_LLM_02,
            message="LLM 输出缺少 cases 数组（或结构不符）",
            node_id=NODE_NAME,
        )
    if_ids = {str(i.get("id", "")) for i in interfaces}
    cases: list[dict[str, Any]] = []
    for raw in data["cases"]:
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
        cases.append(dict(raw))
    return cases


def _filter_param_violations(
    cases: list[dict[str, Any]], interfaces: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """R-GEN-02：params 参数名 ⊆ 接口定义参数集合。

    Returns:
        (通过校验的用例, 违规记录清单[{interface_id, params, message}]).
    """
    params_by_if: dict[str, set[str]] = {}
    for interface in interfaces:
        if_id = str(interface.get("id", ""))
        params_by_if[if_id] = {
            str(p.get("name")) for p in interface.get("params") or [] if p.get("name")
        }
    valid: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for case in cases:
        if_id = str(case.get("interface_id", ""))
        allowed = params_by_if.get(if_id, set())
        extra = (
            {str(k) for k in (case.get("params") or {}).keys() if str(k) not in allowed}
            if isinstance(case.get("params"), dict)
            else set()
        )
        if extra:
            violations.append(
                {
                    "interface_id": if_id,
                    "params": case.get("params"),
                    "message": f"清单外参数：{sorted(extra)}（允许：{sorted(allowed) or '无'})",
                }
            )
        else:
            valid.append(case)
    return valid, violations


def _merge_rounds(
    first: list[dict[str, Any]], second: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """合并两轮通过校验的用例（同接口同类目去重，第一轮优先）。"""
    seen: set[tuple[str, str]] = set()
    merged: list[dict[str, Any]] = []
    for case in first + second:
        key = (str(case.get("interface_id", "")), str(case.get("category", "")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(case)
    return merged


def _finalize_cases(
    cases: list[dict[str, Any]],
    job_id: str,
    store: ArtifactStore,
    interfaces: list[dict[str, Any]],
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
        if case.get("requirement_id"):
            entry["requirement_id"] = str(case["requirement_id"])
        if isinstance(case.get("params"), dict) and case["params"]:
            entry["params"] = case["params"]

        # low_confidence：接口定义含 unknown 参数（X-GEN-03，代码判定）
        if case["interface_id"] in unknown_ifs:
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
