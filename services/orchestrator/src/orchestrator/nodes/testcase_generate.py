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

import hashlib
import json
from functools import partial
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from orchestrator.artifacts import ArtifactStore
from orchestrator.case_validation import (
    expected_client_error,
    inspect_case_parameters,
    materialize_declared_defaults,
)
from orchestrator.engine import Engine, JobState
from orchestrator.errors import X_LLM_02, X_ORCH_01, PipelineError
from orchestrator.llm import LlmResult
from orchestrator.llm_batch import chunk_json_items
from orchestrator.llm_repair import complete_with_json_repair
from orchestrator.nodes.req_parse import _strip_code_fence
from orchestrator.prompts_loader import PromptSpec, load_prompt

NODE_NAME = "testcase_generate"

_VALID_CATEGORIES = ("normal", "boundary", "exception")
_MAX_CASE_INTERFACES_PER_BATCH = 3
_CHECKPOINT_FILENAME = "testcase_generate_checkpoint.json"

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

    async def complete(self, node: str, prompt: PromptSpec, user_content: str) -> LlmResult:
        """按协议执行一次补全。"""
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
        # 它按需求、接口映射及评审风险程序化派生，随后随同批上下文交给模型扩展成可执行案例。
        testpoints = _derive_testpoints(requirements, interfaces, mappings)

        prompt = load_prompt(prompts_dir, NODE_NAME)
        # 以接口为分批边界：每批只携带相关需求与映射，避免用例 JSON 随接口数线性膨胀。
        valid: list[dict[str, Any]] = []
        retry_feedback: list[dict[str, Any]] = []
        # 用例输出随接口数 × 三类场景增长。最多三个接口一批，避免模型只回答
        # 批次首项而把其余接口静默漏掉。
        interface_batches = _chunk_case_interfaces(interfaces)
        checkpoint_fingerprint = _generation_fingerprint(
            requirements, interfaces, mappings, testpoints
        )
        checkpoint = _load_generation_checkpoint(store, state.job_id, checkpoint_fingerprint)
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
                batch, requirements, mappings, testpoints, auth, index, len(interface_batches)
            )
            raw_cases = await _complete_cases_with_coverage_recovery(
                llm, prompt, user_content, batch
            )
            batch_valid, violations = _filter_param_violations(raw_cases, batch)
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
                    validate=partial(_validate_batch_coverage, interfaces=batch),
                )
                valid2, violations2 = _filter_param_violations(raw_cases2, batch)
                batch_valid = _merge_rounds(batch_valid, valid2)
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
                    "batches": sorted(checkpoint_batches, key=lambda item: int(item["index"])),
                },
            )

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
        if not valid:
            raise PipelineError(
                code="X-GEN-02",
                message="全部接口用例生成失败（参数一致性二次不过或漏产）",
                node_id=NODE_NAME,
                details={"warnings": warnings},
            )

        # 元数据补全 + 脚本渲染（TC-### 编号、模板脚本、loadable、low_confidence）
        cases, any_unloadable = _finalize_cases(valid, state.job_id, store, interfaces)
        e2e_cases: list[dict[str, Any]] = []
        if e2e and bool(e2e.get("generate_cases")):
            surface = await _inspect_e2e_surface(
                str(e2e.get("base_url", "")), bool(e2e.get("insecure_tls", True)), auth
            )
            if surface["routes"]:
                e2e_cases = await _generate_e2e_cases(
                    llm, prompts_dir, requirements, testpoints, surface
                )
            else:
                warnings.append("X-E2E-01: 未探测到可操作的前端页面，跳过 E2E 用例生成")
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
    return {"batches": batches}


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


def _build_batch_context(
    interfaces: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    testpoints: list[dict[str, Any]],
    auth: dict[str, Any] | None,
    index: int,
    total: int,
) -> str:
    """构造一批用例生成上下文，只携带本批接口相关的需求。"""
    interface_ids = {str(item.get("id", "")) for item in interfaces}
    batch_mappings = [
        {
            "requirement_id": mapping.get("requirement_id"),
            "interface_id": mapping.get("interface_id"),
            "status": mapping.get("status"),
        }
        for mapping in mappings
        if str(mapping.get("interface_id", "")) in interface_ids
    ]
    requirement_ids = {str(mapping.get("requirement_id", "")) for mapping in batch_mappings}
    context: dict[str, Any] = {
        "batch": {"index": index, "total": total},
        "interfaces": interfaces,
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
    """按字符预算后再按最多三个接口拆分，控制用例输出规模。"""
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
                raise second_error from first_error
            merged = _merge_case_entries(first_cases or [], second_cases)
            missing = _missing_categories(merged, interfaces)
            for interface, categories in missing:
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
                additions = _parse_llm_cases(targeted.text, [interface])
                wanted = [
                    case for case in additions if str(case.get("category", "")) in categories
                ]
                if {str(case.get("category", "")) for case in wanted} != set(categories):
                    raise second_error
                merged = _merge_case_entries(merged, wanted)
            _validate_batch_coverage(merged, interfaces)
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


def _merge_case_entries(
    existing: list[dict[str, Any]], additions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按接口+类别去重合并，补产结果不覆盖已有完整场景。"""
    seen = {(str(item.get("interface_id", "")), str(item.get("category", ""))) for item in existing}
    merged = list(existing)
    for item in additions:
        key = (str(item.get("interface_id", "")), str(item.get("category", "")))
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged


def _looks_like_cross_node_payload(
    text: str, interfaces: list[dict[str, Any]] | None = None
) -> bool:
    """识别跨节点产物或请求参数对象误串到用例节点的常见形态。"""
    try:
        payload = json.loads(_strip_code_fence(text))
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
    """从需求映射派生可追溯测试点，避免模型只围绕 HTTP 参数拼接口用例。

    每个已映射的需求-接口关系至少有一个主业务测试点；评审为 partial、missing
    或 undetermined 时额外生成风险测试点。没有映射的需求也保留为需求级风险点，
    供后续 E2E/人工测试设计使用，绝不伪造接口归属。
    """
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
    ) -> None:
        points.append(
            {
                "id": f"TP-{len(points) + 1:03d}",
                "requirement_id": requirement_id,
                **({"interface_id": interface_id} if interface_id else {}),
                "source": source,
                "source_ref": source_ref,
                "scenario": scenario,
                "case_split_hints": hints,
            }
        )

    mapped_requirements: set[str] = set()
    for mapping in mappings:
        requirement_id = str(mapping.get("requirement_id", ""))
        interface_id = str(mapping.get("interface_id", ""))
        if requirement_id not in requirement_by_id or interface_id not in interface_ids:
            continue
        mapped_requirements.add(requirement_id)
        requirement = requirement_by_id[requirement_id]
        title = str(requirement.get("title", requirement_id))
        description = str(requirement.get("description", "")).strip()
        append_point(
            requirement_id,
            interface_id,
            "REQ",
            f"验证“{title}”主业务流程" + (f"：{description}" if description else ""),
            ["正常主流程", "输入边界与空值", "异常/权限/业务拒绝"],
            f"需求 {requirement_id} ↔ 接口 {interface_id}",
        )
        status = str(mapping.get("status", ""))
        if status in {"partial", "missing", "undetermined"}:
            append_point(
                requirement_id,
                interface_id,
                "RISK",
                f"确认“{title}”在实现状态为 {status} 时的遗漏、降级或不一致行为",
                ["关键业务路径", "异常提示与数据一致性", "配置/会话/权限等服务端行为"],
                f"代码评审映射状态：{status}",
            )
    for requirement_id, requirement in requirement_by_id.items():
        if requirement_id in mapped_requirements:
            continue
        append_point(
            requirement_id,
            None,
            "RISK",
            (
                f"需求“{requirement.get('title', requirement_id)}”尚未映射到接口，"
                "需通过页面流程或人工确认实现证据"
            ),
            ["端到端主路径", "用户可见反馈", "实现与需求的可追溯性"],
            f"需求 {requirement_id} 无接口映射",
        )
    return points


async def _generate_e2e_cases(
    llm: SupportsComplete,
    prompts_dir: Path,
    requirements: list[dict[str, Any]],
    testpoints: list[dict[str, Any]],
    surface: dict[str, Any],
) -> list[dict[str, Any]]:
    """基于真实页面探测结果生成，禁止模型杜撰路由或 selector。"""
    prompt = load_prompt(prompts_dir, "e2e_testcase_generate")
    allowed_selectors = {selector for route in surface["routes"] for selector in route["selectors"]}
    allowed_paths = {route["path"] for route in surface["routes"]}
    content = json.dumps(
        {
            "surface": surface,
            "requirements": requirements,
            "testpoints": testpoints,
            "max_cases": 12,
        },
        ensure_ascii=False,
    )
    raw_cases = await complete_with_json_repair(
        llm,
        "testcase_generate",
        prompt,
        content,
        partial(_parse_e2e_cases, allowed_selectors=allowed_selectors, allowed_paths=allowed_paths),
    )
    result: list[dict[str, Any]] = []
    for sequence, case in enumerate(raw_cases[:12], start=1):
        entry = {
            "id": f"E2E-{sequence:03d}",
            "case_type": "e2e",
            "requirement_id": str(case["requirement_id"]),
            "title": str(case["title"]),
            "preconditions": [str(x) for x in case.get("preconditions", [])],
            "steps": case["steps"],
            "expected": str(case["expected"]),
        }
        result.append(entry)
    return result


async def _inspect_e2e_surface(
    base_url: str, ignore_https_errors: bool, auth: dict[str, Any] | None = None
) -> dict[str, Any]:
    """只读探测根路径与登录页，产出可验证的路由和语义定位器白名单。"""
    base = base_url.rstrip("/") + "/"
    routes: list[dict[str, Any]] = []
    for path in ("/", "/login"):
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                context = await browser.new_context(ignore_https_errors=ignore_https_errors)
                page = await context.new_page()
                try:
                    await page.goto(
                        urljoin(base, path.lstrip("/")),
                        wait_until="domcontentloaded",
                        timeout=15_000,
                    )
                    await page.wait_for_timeout(300)
                    elements = await page.locator(
                        "input, textarea, select, button, a"
                    ).evaluate_all(
                        """nodes => nodes.slice(0, 80).map(n => ({tag:n.tagName.toLowerCase(),
                        text:(n.innerText || n.textContent || '').trim(),
                        aria:n.getAttribute('aria-label'),
                        placeholder:n.getAttribute('placeholder')}))"""
                    )
                    selectors = _observed_selectors(elements)
                    if selectors:
                        routes.append(
                            {
                                "path": path,
                                "title": await page.title(),
                                "selectors": selectors,
                                "visible_text": (await page.locator("body").inner_text())[:800],
                            }
                        )
                finally:
                    await context.close()
                    await browser.close()
        except PlaywrightError:
            continue
    if auth:
        routes.extend(await _inspect_authenticated_routes(base_url, ignore_https_errors, auth))
    unique = {str(route["path"]): route for route in routes}
    return {"base_url": base_url.rstrip("/"), "routes": list(unique.values())}


async def _inspect_authenticated_routes(
    base_url: str, ignore_https_errors: bool, auth: dict[str, Any]
) -> list[dict[str, Any]]:
    """真实登录后探测首页与导航链接；只读访问，不提交任何业务表单。"""
    base = base_url.rstrip("/") + "/"
    discovered: list[dict[str, Any]] = []
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context(ignore_https_errors=ignore_https_errors)
            page = await context.new_page()
            try:
                await page.goto(
                    urljoin(base, "login"), wait_until="domcontentloaded", timeout=15_000
                )
                await page.get_by_placeholder("用户名", exact=False).fill(
                    str(auth.get("username", ""))
                )
                await page.get_by_placeholder("密码", exact=False).fill(
                    str(auth.get("password", ""))
                )
                await page.get_by_role("button", name="登 录", exact=False).click()
                await page.wait_for_timeout(800)
                paths = {urlparse(page.url).path or "/"}
                for href in await page.locator("a").evaluate_all(
                    "nodes => nodes.map(n => n.getAttribute('href'))"
                ):
                    if isinstance(href, str) and href.startswith("/") and not href.startswith("//"):
                        paths.add(href.split("?", 1)[0])
                for path in sorted(paths)[:10]:
                    await page.goto(
                        urljoin(base, path.lstrip("/")),
                        wait_until="domcontentloaded",
                        timeout=15_000,
                    )
                    await page.wait_for_timeout(200)
                    elements = await page.locator(
                        "input, textarea, select, button, a"
                    ).evaluate_all(
                        """nodes => nodes.slice(0, 80).map(n => ({
                        tag:n.tagName.toLowerCase(),
                        text:(n.innerText || n.textContent || '').trim(),
                        aria:n.getAttribute('aria-label'),
                        placeholder:n.getAttribute('placeholder')}))"""
                    )
                    selectors = _observed_selectors(elements)
                    if selectors:
                        discovered.append(
                            {
                                "path": path,
                                "title": await page.title(),
                                "selectors": selectors,
                                "visible_text": (await page.locator("body").inner_text())[:800],
                            }
                        )
            finally:
                await context.close()
                await browser.close()
    except PlaywrightError:
        return []
    return discovered


def _observed_selectors(elements: list[dict[str, Any]]) -> list[str]:
    selectors: list[str] = []
    for element in elements:
        tag = str(element.get("tag", ""))
        text = " ".join(str(element.get("text") or "").split())
        placeholder = str(element.get("placeholder") or "").strip()
        aria = str(element.get("aria") or "").strip()
        if tag in {"input", "textarea"} and placeholder:
            selectors.append(f"placeholder={placeholder}")
        if aria:
            selectors.append(f"label={aria}")
        if tag == "button" and text:
            selectors.append(f"role=button[name={text}]")
        if tag == "a" and text:
            selectors.append(f"role=link[name={text}]")
    return list(dict.fromkeys(selectors))


def _parse_e2e_cases(
    text: str,
    allowed_selectors: set[str] | None = None,
    allowed_paths: set[str] | None = None,
) -> list[dict[str, Any]]:
    """严格收敛 E2E 动作白名单，避免模型产生任意 JS 或危险浏览器操作。"""
    try:
        payload = json.loads(_strip_code_fence(text))
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
    for entry in entries:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(key), str) and entry[key].strip()
            for key in ("requirement_id", "title", "expected")
        ):
            raise PipelineError(
                code=X_LLM_02, message="E2E 用例缺少需求、标题或预期", node_id=NODE_NAME
            )
        steps = entry.get("steps")
        if not isinstance(steps, list) or not steps:
            raise PipelineError(code=X_LLM_02, message="E2E 用例缺少步骤", node_id=NODE_NAME)
        normalized_steps: list[dict[str, str]] = []
        for step in steps:
            if not isinstance(step, dict) or str(step.get("action", "")) not in allowed_actions:
                raise PipelineError(
                    code=X_LLM_02, message="E2E 用例含不支持的浏览器动作", node_id=NODE_NAME
                )
            action = str(step["action"])
            value = step.get("value")
            selector = step.get("selector")
            if action == "goto":
                if not isinstance(value, str) or not value.startswith("/"):
                    raise PipelineError(
                        code=X_LLM_02,
                        message="E2E goto 仅允许站内相对路径",
                        node_id=NODE_NAME,
                    )
                if allowed_paths is not None and value not in allowed_paths:
                    raise PipelineError(
                        code=X_LLM_02,
                        message=f"E2E goto 不是已探测路由：{value}",
                        node_id=NODE_NAME,
                    )
                normalized_steps.append({"action": action, "value": value})
            elif action == "expect_url":
                # URL 断言不定位单个元素；Prompt 明确允许 selector 为空。
                if not isinstance(value, str) or not value.startswith("/"):
                    raise PipelineError(
                        code=X_LLM_02,
                        message="E2E expect_url 仅允许站内相对路径",
                        node_id=NODE_NAME,
                    )
                normalized_steps.append({"action": action, "value": value})
            elif not isinstance(selector, str) or not selector.strip():
                raise PipelineError(
                    code=X_LLM_02, message="E2E 步骤缺少 selector", node_id=NODE_NAME
                )
            else:
                if allowed_selectors is not None and selector not in allowed_selectors:
                    raise PipelineError(
                        code=X_LLM_02,
                        message=f"E2E selector 未在真实页面探测结果中出现：{selector}",
                        node_id=NODE_NAME,
                    )
                normalized = {"action": action, "selector": selector}
                if value is not None:
                    normalized["value"] = str(value)
                normalized_steps.append(normalized)
        copied = dict(entry)
        copied["steps"] = normalized_steps
        cases.append(copied)
    return cases


def _parse_llm_cases(text: str, interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        cases.append(dict(raw))
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
        parsed = json.loads(_strip_code_fence(value))
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
        if isinstance(case.get("params"), dict):
            entry["params"] = case["params"]
        if str(case.get("auth_mode", "")) in {"inherit", "missing", "invalid"}:
            entry["auth_mode"] = str(case["auth_mode"])

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
