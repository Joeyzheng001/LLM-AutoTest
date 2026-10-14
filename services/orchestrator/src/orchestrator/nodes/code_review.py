"""code_review 节点：两视图产出（F-REVW-01/02/05，C-108）——按原子声明分批审查，
产出视图一·需求-代码映射表与视图二·变更范围；原四视图中的风险清单与遗漏
功能清单已停产（C-108）：其信息由视图一 claim 级状态与视图二 risk_level 取代。

LLM 影响分析的侧重是**存量功能回归**（prompt 1.1）：modified/related_file 的既有
接口从严、纯新增接口从简、盲区归因优先指向可能受影响的存量接口。

产物：review_mapping.json（映射表 + 变更范围两视图合一单产物，schema_version 2.1）。

程序化派生（事实字段不经 LLM）：
- evidence.code_location：从 interface_id 对应接口回填（LLM 给的忽略）；
- change_scope（C-09）：code_inventory.changed_files × interfaces.code_location
  文件关联 → 变更接口清单，covered_by 从映射表回填（空 = 未被需求覆盖）；
- change_scope[].risk_level：原 incremental_findings 的 P1/P2 确定性判定映射为
  条目级回归优先级下限（未被需求覆盖/评审状态异常 = P1，待核查 = P2）；
- unmatched_changed_files（C-09）：变更但未命中任何接口定义文件的文件
  （业务逻辑层变更等——接口签名未变但行为可能变更，需人工复核）；
- removed_files（C-09）：被测侧已删除文件单列（无源码可关联接口）。

LLM 影响分析（C-108，语义层增强，失败整体回退纯程序视图、节点不失败）：
- change_scope[].impact_analysis：接口级「改了什么/影响面/测试建议/风险等级」；
- unmatched_changed_files[].attribution：盲区文件归因（可能影响的接口/需求）；
- change_summary：变更总评（性质/整体风险/回归建议）。
编号引用经程序校验（接口/需求/文件必须存在于输入，虚构条目丢弃）。

code_inventory.json 无 changed_files 键（未配基线分支）时视图二为空且跳过 LLM 调用。

校验（R-REVW-01 覆盖率 100% + 虚构防护）：
- 每条输入 REQ 必须有且仅一个映射条目，缺/多/虚构 REQ → X-LLM-02；
- interface_id 必须存在于 interfaces.json → 虚构即 X-LLM-02。

失败口径（docs/12）：
- LLM 输出不可解析、claim/引用业务校验失败 → X-LLM-02；前置产物缺失/不合规或程序派生产物
  与 Schema 不一致 → X-ORCH-01。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from common.artifacts import ArtifactStore
from common.engine import Engine, JobState
from common.errors import X_LLM_02, X_ORCH_01, PipelineError
from common.llm import LlmResult
from common.llm_batch import chunk_json_items
from common.llm_repair import complete_with_json_repair
from common.llm_text import strip_code_fence
from common.paths import normalize_path
from common.prompts_loader import PromptSpec, load_prompt

NODE_NAME = "code_review"
logger = logging.getLogger(__name__)

_VALID_STATUSES = ("implemented", "partial", "missing", "undetermined")
# 模型偶尔会把「只输出 mappings 数组」误解为裸数组，或套入常见的 result/data
# 包装。只在 Code Review 入参/输出契约内做无损解包；条目内容仍走下方全部硬校验。
_MAPPING_CONTAINER_KEYS = (
    "mappings",
    "review_mappings",
    "requirement_mappings",
    "mapping_results",
    "映射",
    "映射表",
    "评审映射",
)
_MAPPING_WRAPPER_KEYS = ("data", "result", "output", "review", "code_review")

# 第一批 Review 加强：只提取可证明服务端行为的局部文本，避免把整仓库、凭据或无关源码送入 LLM。
_EVIDENCE_RULES: dict[str, re.Pattern[str]] = {
    "session": re.compile(r"session|jwt|token|expires?|expiry|ttl|max[_-]?age", re.I),
    "authorization": re.compile(
        r"preauthorize|permission|role|authorize|isauthenticated|login_required", re.I
    ),
    "password": re.compile(
        r"passwordencoder|bcrypt|argon2|make_password|check_password|hashpw", re.I
    ),
    "validation": re.compile(
        r"@valid|@notnull|@notblank|@size|serializer|validate|validators?", re.I
    ),
    "transaction": re.compile(
        r"@transactional|transaction\.atomic|begin_transaction|commit\(", re.I
    ),
    "idempotency": re.compile(
        r"idempoten|get_or_create|uniqueconstraint|unique_together|duplicate", re.I
    ),
    "state_transition": re.compile(
        r"transition|state_machine|setstate|setstatus|status\s*(?:==|=)|state\s*(?:==|=)", re.I
    ),
    "data_consistency": re.compile(
        r"select_for_update|optimistic.?lock|foreignkey|uniqueconstraint|transaction\.atomic", re.I
    ),
    "cache": re.compile(r"@cacheable|cache\.get|cache\.set|redis|expireat", re.I),
    "async": re.compile(r"@async|celery|kafka|rabbitmq|@scheduled|asyncio", re.I),
}
_REVIEWABLE_SUFFIXES = {
    ".py", ".java", ".kt", ".js", ".ts", ".yml", ".yaml", ".properties", ".xml"
}
_MAX_EVIDENCE_FILES = 2_000
# 证据池只在真正达到条目上限时触发降级；不能再把“仓库文件数大于某阈值”误报成
# “扫描范围不完整”。当前普通 Java 仓库通常几百到一千余文件，2000 是防止异常大仓库
# 无限膨胀的保护线，实际会先按变更文件和行为相关文件优先处理。
_MAX_EVIDENCE_ITEMS = 1_000
# 接口实现证据必须覆盖全部接口；该值只作为异常大任务的最后保护线，不再按接口输入顺序
# 截掉前 140 个。正常情况下所有接口都会有 interface_implementation 证据或明确缺失记录。
_MAX_INTERFACE_EVIDENCE = 1_000
_MAX_EVIDENCE_CHARS = 520
# 调用链分析的接口数保护线。正常情况下全部接口都会进入调用链摘要；触顶必须进
# scan_coverage，否则“模型没看到”会被读成“不存在”（评审 §4）。
_MAX_CALL_CHAINS = 1_000
# 语义召回（LLM 重排）用的子提示词与选入上限。中转站没有 embedding 接口
# （`/v1/embeddings` → 401 key_model_access_denied），语义只能交给模型判断。
_EVIDENCE_SELECT_PROMPT = "code_review_evidence_select"
_MAX_LLM_SELECTED_EVIDENCE = 24
# 视图二 LLM 影响分析（C-108）：单次调用，超限截断输入并记 warning，
# 截断条目保留程序派生的 risk_level 兜底。
_CHANGE_IMPACT_PROMPT = "code_review_change_impact"
_MAX_IMPACT_SCOPE_ENTRIES = 60
_MAX_IMPACT_UNMATCHED = 80
_RISK_LEVELS = {"P0", "P1", "P2"}
_SECRET_VALUE = re.compile(
    r"(?i)(password|secret|api[_-]?key|access[_-]?token)\s*([:=])\s*(['\"]?)[^\s,'\"}]+"
)


class SupportsComplete(Protocol):
    """LLM 客户端协议（生产为 LlmClient，测试可注入桩）。"""

    async def complete(self, node: str, prompt: PromptSpec, user_content: str) -> LlmResult:
        """按协议执行一次补全。"""
        ...


async def run_code_review(
    engine: Engine,
    state: JobState,
    store: ArtifactStore,
    llm: SupportsComplete,
    prompts_dir: Path,
) -> Path:
    """执行 code_review 全流程：读前置产物 → LLM 映射 → 派生四视图 → 落盘。

    Returns:
        review_mapping.json 产物路径。

    Raises:
        PipelineError: 各失败口径见模块 docstring。
    """
    engine.check_runnable(state, NODE_NAME)
    engine.mark_running(state, NODE_NAME)
    try:
        requirements = _load_requirements(store, state.job_id)
        interfaces = _load_interfaces(store, state.job_id)
        inventory = _load_code_inventory(store, state.job_id)
        _attach_scan_source_paths(inventory, store, state.job_id)
        # 在 LLM 提示词中按需求选择接口入口，控制语义上下文规模；但确定性证据不能
        # 复用这个 36 条的相关性截断，否则未被需求召回的接口会没有任何源码证据。
        interface_index = _compact_interfaces(interfaces)
        all_claims = _flatten_requirement_claims(requirements)
        review_batches = chunk_json_items(all_claims, max_chars=5_000)
        behavior_evidence = _behavior_evidence(inventory)
        # 所有接口都进入确定性证据提取；后续 LLM 仍可按需求做有限召回，但不能改变
        # “接口是否被源码扫描覆盖”的事实。
        code_evidence = _collect_code_evidence(inventory, interfaces)
        for evidence_index, evidence in enumerate(code_evidence, start=1):
            evidence["evidence_id"] = f"EVD-{evidence_index:04d}"
        call_chains = _derive_call_chains(inventory, interfaces)
        incremental_focus = _collect_incremental_focus(inventory, interfaces)
        deterministic_checks = _derive_deterministic_checks(requirements, code_evidence)
        scan_coverage = _scan_coverage(
            inventory, code_evidence, interfaces, _load_scan_integrity(store, state.job_id)
        )

        prompt = load_prompt(prompts_dir, NODE_NAME)
        # 历史 1.0 需求产物没有 claims。保留一条受控兼容路径读取旧版
        # REQ→interface mappings；新产物仍只接受 claim assessments，不降低新任务闸门。
        legacy_mode = not any(
            isinstance(item.get("claims"), list) and item.get("claims")
            for item in requirements
        )
        claim_assessments: list[dict[str, Any]] = []
        claim_batches = [all_claims] if legacy_mode else review_batches
        mappings: list[dict[str, Any]] = []
        for index, batch in enumerate(claim_batches, start=1):
            batch_claim_ids = {str(claim["claim_id"]) for claim in batch}
            batch_interfaces = _select_interfaces_for_claims(batch, interface_index)
            batch_interface_ids = {str(item["id"]) for item in batch_interfaces if item.get("id")}
            # 接口清单是否完整：全量召回时 LLM 可确信「这些就是全部接口」，
            # 从而对无匹配的 claim 给出更明确的 missing 判定而非一律 undetermined。
            interfaces_complete = len(batch_interfaces) == len(interface_index)
            # 先按语义召回（LLM 重排），无效再退回词面重合。词面召回对「中文 claim
            # vs Java 标识符」基本失效（overlap == 0 直接丢弃），语义层是本节点的关键。
            selected_evidence = await _select_evidence_with_llm(
                llm,
                prompts_dir,
                batch,
                code_evidence,
                batch_interface_ids,
            )
            if selected_evidence is None:
                selected_evidence = _select_evidence_for_claims(
                    batch,
                    code_evidence,
                    preferred_interface_ids=batch_interface_ids,
                )
            selected_locations = {
                (str(item.get("repo", "")), str(item.get("file", "")))
                for item in selected_evidence
            }
            batch_behavior_evidence = [
                item for item in behavior_evidence
                if (str(item.get("repo", "")), str(item.get("file", ""))) in selected_locations
            ][:24]
            batch_call_chains = [
                chain for chain in call_chains
                if str(chain.get("interface_id", "")) in batch_interface_ids
            ]
            batch_incremental_focus = [
                item for item in incremental_focus
                if str(item.get("interface_id", "")) in batch_interface_ids
            ]
            batch_checks = [
                check for check in deterministic_checks
                if str(check.get("claim_id", "")) in batch_claim_ids
            ]
            _append_review_progress(
                store, state.job_id, index, len(claim_batches), len(batch)
            )
            user_content = json.dumps(
                {
                    "batch": {"index": index, "total": len(claim_batches)},
                    "claims": batch,
                    "interfaces": batch_interfaces,
                    "interfaces_complete": interfaces_complete,
                    "behavior_evidence": batch_behavior_evidence,
                    "code_evidence": selected_evidence,
                    "call_chains": batch_call_chains,
                    "incremental_focus": batch_incremental_focus,
                    "deterministic_checks": batch_checks,
                    "scan_coverage": scan_coverage,
                },
                ensure_ascii=False,
            )

            if legacy_mode:
                response = await llm.complete(NODE_NAME, prompt, user_content)
                mappings = _merge_legacy_mappings(response.text, requirements, interfaces)
                deterministic_checks = []
                break
            claim_assessments.extend(
                await complete_with_json_repair(
                    llm,
                    NODE_NAME,
                    prompt,
                    user_content,
                    partial(
                        _merge_claim_assessments_compat,
                        claims=batch,
                        interfaces=batch_interfaces,
                        code_evidence=selected_evidence,
                    ),
                )
            )

        if not legacy_mode:
            claim_assessments = _apply_claim_deterministic_gates(
                claim_assessments, deterministic_checks, bool(scan_coverage["complete"])
            )
            mappings = _aggregate_claim_assessments(requirements, claim_assessments)
        _annotate_evidence_quality(mappings, deterministic_checks, call_chains)

        change_scope, removed_files, unmatched = _derive_change_scope(
            inventory, interfaces, mappings
        )
        incremental_findings = _derive_incremental_findings(
            change_scope, removed_files, unmatched, mappings
        )
        # C-108：incremental_findings 不再独立成视图，其确定性判定映射为条目级
        # risk_level 兜底；LLM 影响分析在其上做语义增强，失败回退纯程序视图。
        _attach_fallback_risk_levels(change_scope, incremental_findings)
        change_summary: dict[str, Any] | None = None
        if _change_scope_available(inventory) and (change_scope or unmatched or removed_files):
            impact = await _analyze_change_impact_with_llm(
                llm,
                prompts_dir,
                change_scope,
                unmatched,
                removed_files,
                call_chains,
                incremental_findings,
                interfaces,
                requirements,
                mappings,
            )
            if impact is not None:
                change_summary = impact.get("change_summary")
                _attach_impact_analysis(change_scope, impact.get("impacts") or [])
                _attach_unmatched_attributions(
                    unmatched, impact.get("unmatched_attributions") or []
                )
        payload: dict[str, Any] = {
            "schema_version": "2.1",
            "job_id": state.job_id,
            "mappings": mappings,
            "evidence_catalog": code_evidence,
            "scan_coverage": scan_coverage,
            "deterministic_checks": deterministic_checks,
            "change_scope": change_scope,
            "change_scope_available": _change_scope_available(inventory),
            "removed_files": removed_files,
            "unmatched_changed_files": unmatched,
            "call_chains": call_chains,
        }
        if change_summary is not None:
            # 顶层 additionalProperties:false 且该键可选：LLM 失败时整键缺省
            payload["change_summary"] = change_summary
        try:
            path = store.write(state.job_id, "review_mapping.json", payload)
        except PipelineError as exc:
            # 到这里 LLM 输出已经过了解析、引用和业务校验；此处失败只能说明
            # 程序派生字段或冻结 Schema 不同步，不能误报成 LLM 输出错误。
            if exc.code == X_ORCH_01:
                schema_errors = exc.details.get("errors", [])
                detail = "；".join(str(item) for item in schema_errors[:3])
                suffix = f"：{detail}" if detail else ""
                raise PipelineError(
                    code=X_ORCH_01,
                    message=f"review_mapping.json 程序派生产物未通过 Schema 校验{suffix}",
                    node_id=NODE_NAME,
                    details=exc.details,
                ) from exc
            raise
        engine.mark_completed(state, NODE_NAME)
        return path
    except PipelineError as exc:
        engine.mark_failed(state, NODE_NAME, exc)
        raise


def _load_requirements(store: ArtifactStore, job_id: str) -> list[dict[str, Any]]:
    """读 requirement_items.json 的 items（前置节点产物，读失败即 X-ORCH-01）。"""
    try:
        payload = store.read_json(job_id, "requirement_items.json")
        items = payload.get("items")
    except FileNotFoundError as exc:
        raise PipelineError(
            code=X_ORCH_01,
            message="前置产物 requirement_items.json 缺失",
            node_id=NODE_NAME,
        ) from exc
    if not isinstance(items, list):
        raise PipelineError(
            code=X_ORCH_01,
            message="前置产物 requirement_items.json 缺 items 数组",
            node_id=NODE_NAME,
        )
    return [dict(item) for item in items]


def _load_interfaces(store: ArtifactStore, job_id: str) -> list[dict[str, Any]]:
    """读 interfaces.json 的 interfaces（同上）。"""
    try:
        payload = store.read_json(job_id, "interfaces.json")
        entries = payload.get("interfaces")
    except FileNotFoundError as exc:
        raise PipelineError(
            code=X_ORCH_01,
            message="前置产物 interfaces.json 缺失",
            node_id=NODE_NAME,
        ) from exc
    if not isinstance(entries, list):
        raise PipelineError(
            code=X_ORCH_01,
            message="前置产物 interfaces.json 缺 interfaces 数组",
            node_id=NODE_NAME,
        )
    return [dict(entry) for entry in entries]


def _load_code_inventory(store: ArtifactStore, job_id: str) -> dict[str, Any]:
    """读 code_inventory.json（C-09 变更范围派生源；无则空对象 → 两视图为空）。"""
    try:
        return dict(store.read_json(job_id, "code_inventory.json"))
    except FileNotFoundError:
        return {}


def _attach_scan_source_paths(
    inventory: dict[str, Any], store: ArtifactStore, job_id: str
) -> None:
    """把接口还原阶段实际解析的 target 快照路径回填到评审内存清单。

    Java 候选是在 target_sha worktree 上解析的；评审若重新从可变工作区读源码，
    行号和片段可能再次错位。快照路径只在当前评审内使用，不改写 code_inventory 原件。
    """
    try:
        change_set = store.read_json(job_id, "change_set.json")
    except FileNotFoundError:
        return
    if not isinstance(change_set, dict):
        return
    source_by_repo = {
        str(item.get("name", "")): item.get("source_path")
        for item in change_set.get("repositories") or []
        if isinstance(item, dict) and item.get("source_path")
    }
    for repo in inventory.get("repositories") or []:
        if not isinstance(repo, dict):
            continue
        source_path = source_by_repo.get(str(repo.get("name", "")))
        if source_path:
            repo["source_path"] = source_path


def _compact_interfaces(interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """审查提示只保留需求映射需要的字段，代码溯源仍由程序回填。"""
    return [
        {
            "id": item.get("id"),
            "protocol": item.get("protocol"),
            "signature": item.get("signature"),
            "params": item.get("params", []),
            "returns": item.get("returns"),
            "code_location": item.get("code_location"),
        }
        for item in interfaces
    ]


def _select_interfaces_for_claims(
    claims: list[dict[str, Any]], interfaces: list[dict[str, Any]], limit: int = 40
) -> list[dict[str, Any]]:
    """按声明语义收窄接口上下文。

    接口总数不超过 limit 时全量返回，交由 LLM 做语义判断——字面 n-gram 召回会
    漏召「语义相关但字面不重叠」的接口（如“自动审批”与“风险审批试算”），而接口
    少时全量送入 LLM 的成本可忽略，语义召回由 LLM 的理解能力天然完成。仅当接口
    超出 limit 时，才用字面重叠预筛兜底，避免上下文膨胀。
    """
    if len(interfaces) <= limit:
        return list(interfaces)
    claim_tokens = _content_tokens(" ".join(
        " ".join(str(claim.get(key, "")) for key in ("requirement_title", "statement"))
        + " " + " ".join(str(value) for value in claim.get("acceptance_criteria", []))
        for claim in claims
    ))
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for index, interface in enumerate(interfaces):
        signature = interface.get("signature")
        signature_text = json.dumps(signature, ensure_ascii=False) if isinstance(signature, dict) else str(signature or "")
        location = interface.get("code_location") or {}
        interface_text = " ".join((
            str(interface.get("protocol", "")),
            signature_text,
            str(location.get("repo", "")),
            str(location.get("file", "")),
            json.dumps(interface.get("params", []), ensure_ascii=False),
        ))
        overlap = len(claim_tokens & _content_tokens(interface_text))
        if overlap:
            ranked.append((overlap, -index, interface))
    ranked.sort(key=lambda value: (value[0], value[1]), reverse=True)
    if ranked:
        return [entry[2] for entry in ranked[:limit]]
    return interfaces[: min(8, limit)]


def _behavior_evidence(inventory: dict[str, Any]) -> list[dict[str, str]]:
    """从扫描清单选出服务端行为相关文件，避免 Review 只盯 Controller/API。"""
    keywords = (
        "auth",
        "session",
        "jwt",
        "token",
        "setting",
        "config",
        "middleware",
        "security",
        "cache",
        "serializ",
        "model",
        "service",
        "message",
    )
    evidence: list[dict[str, str]] = []
    for repo in inventory.get("repositories") or []:
        if not isinstance(repo, dict):
            continue
        for file in repo.get("files") or []:
            if not isinstance(file, dict):
                continue
            path = str(file.get("path", ""))
            if any(word in path.lower() for word in keywords):
                evidence.append({"repo": str(repo.get("name", "")), "file": path})
    return evidence[:120]


def _collect_code_evidence(
    inventory: dict[str, Any], interfaces: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """抽取可引用的服务端行为证据片段（确定性、限量、脱敏）。

    code_inventory 只保存文件清单，因此此处仅从本次扫描所记录的仓库路径读取
    明确允许的源码/配置后缀。找不到文件、越出仓库根目录或无法解码均跳过；
    评审随后会如实输出“待核查”，而不是猜测实现存在。
    """
    evidence: list[dict[str, Any]] = []
    roots = {
        str(repo.get("name", "")): Path(
            str(repo.get("source_path") or repo.get("path", ""))
        )
        for repo in inventory.get("repositories") or []
        if isinstance(repo, dict)
    }
    # 变更文件优先：本节点评审的是「本次改动」，先把变更文件里的接口证据收进来再截断。
    # 原先是按输入顺序硬截前 36 个接口——113 个接口时 77 个连一条接口证据都没有。
    changed: dict[str, set[str]] = {}
    for repo in inventory.get("repositories") or []:
        if not isinstance(repo, dict):
            continue
        changed_files = repo.get("changed_files")
        if not isinstance(changed_files, dict):
            continue
        # 归一为存储口径（正斜杠）：历史任务可能存反斜杠，而接口 code_location.file
        # 来自 java-parser（正斜杠），不归一则集合成员判定恒为 False（X-REV-04 同类）。
        changed[str(repo.get("name", ""))] = {
            normalize_path(value)
            for key in ("added", "modified")
            for value in (changed_files.get(key) or [])
            if isinstance(value, str)
        }

    def _is_changed(interface: dict[str, Any]) -> bool:
        location = interface.get("code_location") or {}
        return str(location.get("file", "")) in changed.get(str(location.get("repo", "")), set())

    ordered_interfaces = sorted(interfaces or [], key=lambda item: not _is_changed(item))
    # 普通业务声明需要的不只是 Controller 定义位置。接口还原阶段已经产出递归
    # code_spans，这里把 body + callee 的每个源码节点都加入证据池，并合并同一位置
    # 对应的多个接口 ID，避免 Service/Helper/DAO 改动没有证据可供需求映射引用。
    source_entries: dict[tuple[str, str, int, int, str], dict[str, Any]] = {}
    for interface in ordered_interfaces:
        interface_id = str(interface.get("id", ""))
        location = interface.get("code_location") or {}
        locations = [
            {
                "repo": location.get("repo"),
                "file": location.get("file"),
                "start_line": location.get("start_line"),
                "end_line": location.get("end_line"),
                "category": "interface_implementation",
                "role": "controller",
            }
        ]
        for span in interface.get("code_spans") or []:
            if not isinstance(span, dict):
                continue
            locations.append(
                {
                    "repo": location.get("repo"),
                    "file": span.get("file"),
                    "start_line": span.get("start_line"),
                    "end_line": span.get("end_line"),
                    "category": "call_chain_implementation",
                    "role": span.get("role", "other"),
                }
            )
        for item in locations:
            repo_name = str(item.get("repo") or "")
            relative_path = str(item.get("file") or "")
            try:
                start_line = int(item.get("start_line") or 0)
                end_line = int(item.get("end_line") or start_line)
            except (TypeError, ValueError):
                continue
            category = str(item.get("category") or "interface_implementation")
            key = (repo_name, relative_path, start_line, end_line, category)
            if not repo_name or not relative_path or start_line < 1:
                continue
            source = source_entries.setdefault(
                key,
                {
                    "repo": repo_name,
                    "file": relative_path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "category": category,
                    "role": str(item.get("role") or "other"),
                    "interface_ids": [],
                },
            )
            if interface_id and interface_id not in source["interface_ids"]:
                source["interface_ids"].append(interface_id)

    ordered_sources = sorted(
        source_entries.values(),
        key=lambda item: (
            item["file"] not in changed.get(item["repo"], set()),
            item["category"] != "interface_implementation",
        ),
    )
    for item in ordered_sources[:_MAX_INTERFACE_EVIDENCE]:
        repo_name = item["repo"]
        relative_path = item["file"]
        root_value = roots.get(repo_name)
        if root_value is None or not root_value.is_dir():
            continue
        root = root_value.resolve()
        path = (root / relative_path).resolve()
        if root not in path.parents or path.suffix.lower() not in _REVIEWABLE_SUFFIXES:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        start_line = int(item["start_line"])
        if start_line > len(lines):
            continue
        declared_end = max(start_line, int(item["end_line"]))
        snippet_start = max(1, start_line - 2)
        snippet_end = min(len(lines), max(start_line + 8, min(declared_end, start_line + 24)) + 2)
        excerpt = "\n".join(lines[snippet_start - 1 : snippet_end])
        excerpt = _SECRET_VALUE.sub(r"\1\2<redacted>", excerpt)[:_MAX_EVIDENCE_CHARS]
        if not excerpt.strip():
            continue
        evidence.append(
            {
                "category": item["category"],
                "repo": repo_name,
                "file": relative_path,
                "start_line": snippet_start,
                "end_line": snippet_end,
                "excerpt": excerpt,
                "interface_ids": item["interface_ids"],
                "role": item["role"],
            }
        )
    for repo in inventory.get("repositories") or []:
        if not isinstance(repo, dict) or len(evidence) >= _MAX_EVIDENCE_ITEMS:
            continue
        root = Path(str(repo.get("source_path") or repo.get("path", "")))
        if not root.is_dir():
            continue
        root = root.resolve()
        files = repo.get("files") or []
        candidates: list[str] = []
        for item in files:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                candidates.append(item["path"])
        # 扫描清单对 Python 仓库只枚举 .py；补充有限的配置文件，才能审查
        # Session/JWT/缓存等通常落在 settings/application 配置中的服务端行为。
        if len(candidates) < _MAX_EVIDENCE_FILES:
            for path in root.rglob("*"):
                if len(candidates) >= _MAX_EVIDENCE_FILES:
                    break
                if not path.is_file() or {".git", "node_modules", ".venv"} & set(path.parts):
                    continue
                relative = path.relative_to(root).as_posix()
                is_config = path.suffix.lower() in {".yml", ".yaml", ".properties", ".xml"}
                if is_config and relative not in candidates:
                    candidates.append(relative)
        changed_paths = changed.get(str(repo.get("name", "")), set())
        behavior_path_keywords = (
            "auth",
            "session",
            "jwt",
            "token",
            "setting",
            "config",
            "middleware",
            "security",
            "cache",
            "serializ",
            "model",
            "service",
            "message",
        )
        candidates = sorted(
            dict.fromkeys(candidates),
            key=lambda path: (
                path not in changed_paths,
                not any(word in path.lower() for word in behavior_path_keywords),
                path,
            ),
        )[:_MAX_EVIDENCE_FILES]
        for relative_path in candidates:
            if len(evidence) >= _MAX_EVIDENCE_ITEMS:
                break
            path = (root / relative_path).resolve()
            if root not in path.parents or path.suffix.lower() not in _REVIEWABLE_SUFFIXES:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            captured_categories: set[str] = set()
            for line_no, line in enumerate(lines, start=1):
                if len(evidence) >= _MAX_EVIDENCE_ITEMS:
                    break
                category = next(
                    (name for name, pattern in _EVIDENCE_RULES.items() if pattern.search(line)),
                    None,
                )
                if category is None or category in captured_categories:
                    continue
                start = max(1, line_no - 2)
                end = min(len(lines), line_no + 2)
                excerpt = "\n".join(lines[start - 1 : end])
                excerpt = _SECRET_VALUE.sub(r"\1\2<redacted>", excerpt)
                evidence.append(
                    {
                        "category": category,
                        "repo": str(repo.get("name", "")),
                        "file": relative_path,
                        "start_line": start,
                        "end_line": end,
                        "excerpt": excerpt[:_MAX_EVIDENCE_CHARS],
                    }
                )
                # 单个文件/类别一个片段足够；避免同一配置重复占满上下文。
                captured_categories.add(category)
    return evidence


def _derive_call_chains(
    inventory: dict[str, Any], interfaces: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """优先使用 Java Parser 已确认的递归调用链，旧产物才退回正则兜底。

    新产物中的 ``call_graph``/``code_spans`` 是调用链事实来源；这里不能再从 Controller
    文本猜一跳变量名，否则 Service/Helper/DAO 改动会在 Code Review 阶段再次丢失。
    """
    roots = {
        str(repo.get("name", "")): Path(
            str(repo.get("source_path") or repo.get("path", ""))
        )
        for repo in inventory.get("repositories") or []
        if isinstance(repo, dict)
    }
    chains: list[dict[str, Any]] = []
    call_pattern = re.compile(
        r"\b([A-Za-z_][A-Za-z0-9_]*(?:service|repository|dao|mapper|client))\s*\.", re.I
    )
    for interface in interfaces:
        interface_id = str(interface.get("id", ""))
        location = interface.get("code_location") or {}
        repo_name = str(location.get("repo", ""))
        relative = str(location.get("file", ""))
        graph = interface.get("call_graph")
        spans = [
            item
            for item in interface.get("code_spans") or []
            if isinstance(item, dict) and item.get("kind") == "callee"
        ]
        if isinstance(graph, dict):
            chains.append(
                {
                    "interface_id": interface_id,
                    "entry": {"repo": repo_name, "file": relative},
                    "callees": [
                        {
                            "file": str(item.get("file", "")),
                            "role": str(item.get("role", "other")),
                            "start_line": item.get("start_line"),
                            "end_line": item.get("end_line"),
                            "depth": item.get("depth", 1),
                            "call_line": item.get("call_line"),
                        }
                        for item in spans
                    ],
                    "status": str(graph.get("status", "partial")),
                    "resolved_calls": int(graph.get("resolved_calls", 0) or 0),
                    "resolved_nodes": int(graph.get("resolved_nodes", len(spans)) or 0),
                    "unresolved_calls": int(
                        graph.get("unresolved_calls", len(interface.get("unresolved") or [])) or 0
                    ),
                    "unresolved": list(interface.get("unresolved") or []),
                    "max_depth": int(graph.get("max_depth", 0) or 0),
                    "analysis_method": "static_recursive",
                }
            )
            continue
        root = roots.get(repo_name)
        if root is None or not root.is_dir() or not relative:
            continue
        root = root.resolve()
        source = (root / relative).resolve()
        if root not in source.parents:
            continue
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        start = max(0, int(location.get("start_line", 1)) - 1)
        fragment = "\n".join(lines[start : start + 160])
        callees = sorted({match.group(1) for match in call_pattern.finditer(fragment)})[:6]
        if not callees:
            continue
        chains.append(
            {
                "interface_id": interface_id,
                "entry": {"repo": repo_name, "file": relative},
                "callees": callees,
                "status": "partial",
                "resolved_calls": len(callees),
                "resolved_nodes": len(callees),
                "unresolved_calls": 0,
                "max_depth": 1,
                "analysis_method": "legacy_static_one_hop",
            }
        )
    return chains


def _collect_incremental_focus(
    inventory: dict[str, Any], interfaces: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """在 LLM 审查前提供变更接口焦点；只陈述 diff 已知事实。"""
    changed: set[tuple[str, str]] = set()
    for repo in inventory.get("repositories") or []:
        if not isinstance(repo, dict):
            continue
        repo_name = str(repo.get("name", ""))
        files = repo.get("changed_files") or {}
        if not isinstance(files, dict):
            continue
        for file in (files.get("added") or []) + (files.get("modified") or []):
            changed.add((repo_name, normalize_path(file)))
    result: list[dict[str, str]] = []
    for interface in interfaces:
        location = interface.get("code_location") or {}
        repo_name = str(location.get("repo", ""))
        file = str(location.get("file", ""))
        if (repo_name, file) in changed:
            result.append(
                {"interface_id": str(interface.get("id", "")), "repo": repo_name, "file": file}
            )
    return result


def _required_evidence_categories(requirement: dict[str, Any]) -> set[str]:
    """由需求文字触发确定性证据门槛；未命中时不额外施加门槛。"""
    text = " ".join(
        str(requirement.get(key, "")) for key in ("title", "description", "acceptance_criteria")
    ).lower()
    rules = {
        "session": ("session", "会话", "有效期", "过期", "token", "jwt"),
        "authorization": ("权限", "鉴权", "授权", "角色", "permission", "authorize"),
        "password": ("密码", "password", "口令", "哈希"),
        "validation": ("校验", "必填", "长度", "格式", "validate"),
        "transaction": ("事务", "一致性", "回滚", "transaction"),
        "idempotency": ("幂等", "重复提交", "去重", "idempot"),
        "state_transition": ("状态流转", "状态转换", "终态", "确认", "撤销", "状态变更", "state transition"),
        "data_consistency": ("数据一致性", "余额一致", "份额一致", "唯一约束", "并发更新", "数据完整性"),
        "cache": ("缓存", "cache", "redis"),
        "async": ("异步", "消息", "队列", "定时", "kafka", "rabbit"),
    }
    return {
        category
        for category, keywords in rules.items()
        if any(word in text for word in keywords)
    }


def _flatten_requirement_claims(requirements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将新旧需求产物统一为有稳定审查 ID 的 claim 输入。"""
    flattened: list[dict[str, Any]] = []
    next_id = 1
    used_ids: set[str] = set()
    for requirement in requirements:
        req_id = str(requirement.get("id", ""))
        claims = requirement.get("claims")
        if not isinstance(claims, list) or not claims:
            claims = [{"kind": "normal", "statement": requirement.get("description") or requirement.get("title", "")}]
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            claim_id = claim.get("claim_id")
            if not isinstance(claim_id, str) or not re.fullmatch(r"CLM-\d{3,}", claim_id) or claim_id in used_ids:
                while f"CLM-{next_id:04d}" in used_ids:
                    next_id += 1
                claim_id = f"CLM-{next_id:04d}"
            used_ids.add(claim_id)
            next_id = max(next_id + 1, int(claim_id.removeprefix("CLM-")) + 1)
            statement = str(claim.get("statement", "")).strip()
            if not statement:
                statement = str(requirement.get("description", requirement.get("title", ""))).strip()
            criteria = claim.get("acceptance_criteria") or []
            if not isinstance(criteria, list):
                criteria = [str(criteria)]
            flattened.append(
                {
                    "claim_id": claim_id,
                    "requirement_id": req_id,
                    "requirement_title": str(requirement.get("title", "")),
                    "kind": str(claim.get("kind", "normal")),
                    "statement": statement,
                    "acceptance_criteria": [str(value) for value in criteria],
                    "source_refs": list(claim.get("source_refs") or requirement.get("source_refs") or []),
                }
            )
    return flattened


def _content_tokens(value: str) -> set[str]:
    aliases = {
        "产品": ("product", "fund"),
        "基金": ("fund", "product"),
        "订单": ("order", "application"),
        "交易": ("trade", "transaction", "business"),
        "账户": ("account", "user"),
        "用户": ("user", "account"),
        "登录": ("login", "auth", "token"),
        "会话": ("session", "token"),
        "有效期": ("expires", "expiry", "ttl"),
        "过期": ("expires", "expiry", "ttl"),
        "鉴权": ("auth", "authorize", "permission"),
        "权限": ("permission", "authorize", "role"),
        "授权": ("authorize", "permission", "role", "authorization"),
        "事务": ("transaction", "atomic"),
        "回滚": ("rollback", "transaction"),
        "幂等": ("idempotency", "duplicate"),
        "状态流转": ("state", "status", "transition"),
        "一致性": ("consistency", "transaction", "lock"),
        "异步": ("async", "message", "queue"),
        "缓存": ("cache", "redis", "ttl"),
        "定时": ("scheduled", "cron", "task"),
        "开户": ("open", "account", "investor"),
        "清算": ("clearing", "settlement"),
        "对账": ("recon", "reconcile"),
        "查询": ("get", "list", "query", "search"),
        "新增": ("create", "add"),
        "更新": ("update", "edit"),
        "修改": ("update", "edit"),
        "删除": ("delete", "remove"),
        "撤单": ("cancel", "revoke"),
        "状态": ("status", "state"),
        "校验": ("valid", "validate", "validator"),
        "分页": ("page", "pagination"),
        "金额": ("amount", "money"),
        "份额": ("share", "volume"),
        "密码": ("password", "credential"),
        # 风控/审批域补充：真机上「审批/豁免/备注/权重/分级授权」类 claim 一条证据都
        # 召回不到，正因为这些词原先不在表里（`overlap == 0` 会被直接丢弃）。
        "审批": ("approval", "approve", "audit", "review"),
        "审核": ("audit", "review", "approval"),
        "豁免": ("exempt", "exemption", "waive"),
        "备注": ("remark", "comment", "note"),
        "权重": ("weight", "priority", "score"),
        "时点": ("timing", "timepoint", "stage"),
        "投标": ("bid", "bidding", "tender"),
        "中标": ("award", "winbid"),
        "分级": ("grade", "level", "tier", "hierarchy"),
        "债项": ("bond", "debt", "issue"),
        "债券": ("bond", "debt"),
        "内评": ("rating", "internal"),
        "规则": ("rule", "config", "setting"),
        "设置": ("setting", "config", "configure"),
        "配置": ("config", "configuration", "property"),
        "节点": ("node", "stage", "step"),
        "场景": ("scene", "scenario", "case"),
        "开关": ("switch", "toggle", "flag", "enable"),
        "风控": ("risk", "control"),
        "编号": ("serial", "code", "no"),
        "序号": ("index", "serial", "no"),
        "列表": ("list", "page"),
        "搜索": ("search", "query", "filter"),
        "保存": ("save", "store", "persist"),
        "提交": ("submit", "commit"),
        "字段": ("field", "column", "property"),
        "结果": ("result", "outcome"),
        "异常": ("exception", "error", "fail"),
        "合规": ("compliance", "regulatory"),
        "中心": ("center", "centre"),
        "投管": ("investment", "manage"),
        "固收": ("fixedincome", "bond"),
        "触发": ("trigger", "hit"),
        "生效": ("effective", "apply"),
        "优先级": ("priority", "level"),
        "明细": ("detail", "detailitem"),
    }
    tokens: set[str] = set()
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", value):
        raw = match.group(0)
        token = raw.lower()
        tokens.add(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            tokens.update(token[index : index + 2] for index in range(len(token) - 1))
            tokens.update(token[index : index + 3] for index in range(len(token) - 2))
        else:
            # 标识符按 camelCase/下划线拆开：不拆的话 `RiskApprovalRuleServiceImpl`
            # 只产出整串，与「审批/规则」永远零重合，该 claim 的证据会被直接丢掉。
            tokens.update(_split_identifier(raw))
    for token in tuple(tokens):
        tokens.update(part.lower() for part in token.split("_") if len(part) >= 3)
    lowered = value.lower()
    for term, translated in aliases.items():
        if term in lowered:
            tokens.update(translated)
    return tokens


_CAMEL_PIECE_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def _split_identifier(raw: str) -> set[str]:
    """拆 ASCII 标识符（camelCase 与下划线），只保留长度 ≥3 的片段以压噪声。"""
    pieces: set[str] = set()
    for chunk in raw.split("_"):
        if not chunk:
            continue
        pieces.update(
            piece.lower() for piece in _CAMEL_PIECE_RE.findall(chunk) if len(piece) >= 3
        )
    return pieces


async def _select_evidence_with_llm(
    llm: SupportsComplete,
    prompts_dir: Path,
    claims: list[dict[str, Any]],
    code_evidence: list[dict[str, Any]],
    preferred_interface_ids: set[str] | None = None,
) -> list[dict[str, Any]] | None:
    """用一次轻量模型调用按语义挑证据；失败/无效一律返回 None 交由词面召回兜底。

    中转站没有 embedding 接口（`/v1/embeddings` → 401 key_model_access_denied），
    语义召回只能交给模型：程序提供证据目录（编号 + 文件 + 片段摘要）并按编号取回
    完整片段，判断权在模型。这正是纯词面召回做不到的地方——中文 claim 里的「审批
    规则」与 `RiskApprovalRuleServiceImpl` 字面零重合，会被 `overlap == 0` 丢掉。
    """
    if not code_evidence:
        return []
    try:
        prompt = load_prompt(prompts_dir, _EVIDENCE_SELECT_PROMPT)
    except PipelineError as exc:
        logger.warning("证据语义召回 Prompt 加载失败，退回词面召回：%s", exc.message)
        return None
    catalog = [
        {
            "evidence_id": str(item.get("evidence_id", "")),
            "category": str(item.get("category", "")),
            "repo": str(item.get("repo", "")),
            "file": str(item.get("file", "")),
            "start_line": item.get("start_line"),
            "interface_ids": list(item.get("interface_ids") or []),
            "preview": str(item.get("excerpt", ""))[:160],
        }
        for item in code_evidence
        if item.get("evidence_id")
    ]
    user_content = json.dumps(
        {
            "claims": [
                {
                    "claim_id": claim.get("claim_id"),
                    "requirement_title": claim.get("requirement_title"),
                    "statement": claim.get("statement"),
                    "acceptance_criteria": claim.get("acceptance_criteria") or [],
                }
                for claim in claims
            ],
            "preferred_interface_ids": sorted(preferred_interface_ids or []),
            "catalog": catalog,
        },
        ensure_ascii=False,
    )
    try:
        result = await llm.complete(_EVIDENCE_SELECT_PROMPT, prompt, user_content)
    except PipelineError as exc:
        logger.warning("证据语义召回调用失败，退回词面召回：%s", exc.message)
        return None
    try:
        data = json.loads(strip_code_fence(result.text))
    except json.JSONDecodeError:
        logger.warning("证据语义召回输出不是合法 JSON，退回词面召回")
        return None
    raw_ids = data.get("evidence_ids") if isinstance(data, dict) else None
    if not isinstance(raw_ids, list):
        logger.warning("证据语义召回输出缺 evidence_ids 数组，退回词面召回")
        return None
    by_id = {str(item.get("evidence_id", "")): item for item in code_evidence}
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in raw_ids:
        evidence_id = str(value)
        item = by_id.get(evidence_id)
        # 只认目录里存在的编号，虚构/重复一律忽略（与其它节点的溯源硬校验同口径）
        if item is None or evidence_id in seen:
            continue
        seen.add(evidence_id)
        chosen.append(item)
        if len(chosen) >= _MAX_LLM_SELECTED_EVIDENCE:
            break
    if not chosen:
        # 模型说「都不相关」或编号全虚构：退回词面召回，避免整批无证据可判。
        logger.warning("证据语义召回未命中任何证据，退回词面召回")
        return None
    return chosen


def _select_evidence_for_claims(
    claims: list[dict[str, Any]],
    code_evidence: list[dict[str, Any]],
    limit: int = 24,
    preferred_interface_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """按 claim 关键词和高风险类别挑选小批证据，控制上下文并减少无关片段。"""
    claim_text = " ".join(
        " ".join(str(claim.get(key, "")) for key in ("requirement_title", "statement", "kind"))
        + " " + " ".join(str(value) for value in claim.get("acceptance_criteria", []))
        for claim in claims
    )
    claim_tokens = _content_tokens(claim_text)
    categories = set().union(*(_required_evidence_categories({
        "title": claim.get("requirement_title", ""),
        "description": claim.get("statement", ""),
        "acceptance_criteria": claim.get("acceptance_criteria", []),
    }) for claim in claims)) if claims else set()
    ranked: list[tuple[int, dict[str, Any]]] = []
    for evidence in code_evidence:
        evidence_text = " ".join(
            str(evidence.get(key, "")) for key in ("file", "excerpt", "category", "interface_ids")
        )
        overlap = len(claim_tokens & _content_tokens(evidence_text))
        if overlap == 0:
            continue
        linked_interfaces = set(str(value) for value in evidence.get("interface_ids", []))
        if preferred_interface_ids and linked_interfaces.intersection(preferred_interface_ids):
            overlap += 12
        if evidence.get("category") in categories:
            overlap += 24
        ranked.append((overlap, evidence))
    ranked.sort(key=lambda entry: entry[0], reverse=True)
    chosen = [entry[1] for entry in ranked[:limit]]
    return chosen


def _scan_coverage(
    inventory: dict[str, Any],
    code_evidence: list[dict[str, Any]] | None = None,
    interfaces: list[dict[str, Any]] | None = None,
    scan_integrity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """扫描覆盖率与"模型到底看全了没有"（P0①/②）。

    `complete=true` 的语义是"仓库扫描完整，你已看到全部接口"——prompt 直接依赖它
    （`skills/code-review-evidence.md`：complete=false 时"未发现"不能解释成"不存在"）。
    所以任何**截断**（证据池、接口证据、调用链）与任何**完整性校验缺失**都必须让它是 false，
    不能只报我们碰巧检测到的那一项。
    """
    repositories = [item for item in inventory.get("repositories", []) if isinstance(item, dict)]
    repository_file_limit = any(
        isinstance(repo.get("files"), list) and len(repo["files"]) > _MAX_EVIDENCE_FILES
        for repo in repositories
    )
    evidence_limit_reached = repository_file_limit or (
        len(code_evidence or []) >= _MAX_EVIDENCE_ITEMS
    )
    interface_items = [item for item in interfaces or [] if isinstance(item, dict)]
    interface_total = len(interface_items)
    expected_interface_ids = {
        str(item.get("id")) for item in interface_items if str(item.get("id", ""))
    }
    covered_interface_ids = {
        str(interface_id)
        for evidence in code_evidence or []
        if isinstance(evidence, dict)
        and evidence.get("category") == "interface_implementation"
        for interface_id in evidence.get("interface_ids") or []
        if str(interface_id)
    }
    missing_interface_ids = sorted(expected_interface_ids - covered_interface_ids)
    # 接口证据与调用链的硬上限原先不在 scan_coverage 里：超过 140 个接口时证据被悄悄截断，
    # 而 complete 仍是 true —— 下游据此把"模型没看到"读成"不存在"。
    interface_evidence_truncated = (
        interface_total > _MAX_INTERFACE_EVIDENCE or bool(missing_interface_ids)
    )
    call_chain_truncated = interface_total > _MAX_CALL_CHAINS
    integrity_complete = True
    integrity_reasons: list[str] = []
    if scan_integrity is None:
        integrity_complete = False
        integrity_reasons = [
            "未找到接口扫描完整性记录（接口还原产物缺该字段，无法校验扫描是否看全）"
        ]
    else:
        integrity_complete = bool(scan_integrity.get("complete"))
        integrity_reasons = [str(item) for item in (scan_integrity.get("reasons") or [])]
    incomplete = [
        {
            "repo": str(repo.get("name", "")),
            "status": str(repo.get("status", "unknown")),
            "truncated_scope": list(repo.get("truncated_scope") or []),
            "encoding_skipped": list(repo.get("encoding_skipped") or []),
        }
        for repo in repositories
        if repo.get("status") != "ok" or repo.get("truncated_scope") or repo.get("encoding_skipped")
    ]
    complete = (
        bool(repositories)
        and not incomplete
        and not evidence_limit_reached
        and not interface_evidence_truncated
        and not call_chain_truncated
        and integrity_complete
    )
    return {
        "complete": complete,
        "repositories": incomplete,
        "review_evidence_limited": evidence_limit_reached,
        "interface_evidence_truncated": interface_evidence_truncated,
        "interface_evidence_limit": _MAX_INTERFACE_EVIDENCE,
        "interface_evidence_count": len(covered_interface_ids),
        "interface_evidence_missing": missing_interface_ids,
        "call_chain_truncated": call_chain_truncated,
        "call_chain_limit": _MAX_CALL_CHAINS,
        "interfaces_total": interface_total,
        "scan_integrity_complete": integrity_complete,
        "scan_integrity_reasons": integrity_reasons,
    }


def _load_scan_integrity(store: ArtifactStore, job_id: str) -> dict[str, Any] | None:
    """读接口还原阶段落盘的扫描完整性门禁结果（P0①）；缺失返回 None。

    主源是 change_set.json（必带 scan_integrity），退回 interface_candidates.json。
    读不到**不假装完整**：调用方把 None 当"无法校验"（complete=false）。
    """
    for filename in ("change_set.json", "interface_candidates.json"):
        try:
            payload = store.read_json(job_id, filename)
        except FileNotFoundError:
            continue
        if not isinstance(payload, dict):
            continue
        integrity = payload.get("scan_integrity")
        if isinstance(integrity, dict):
            return dict(integrity)
        if isinstance(payload.get("stats"), dict):
            # 有统计但没门禁结论：完整性未知，如实标不完整
            return {"complete": False, "reasons": ["接口还原产物未记录扫描完整性结论"]}
    return None


def _aggregate_claim_assessments(
    requirements: list[dict[str, Any]], assessments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """将 claim 状态汇总为旧页面仍可读取的 REQ 级映射，同时保留逐条依据。"""
    assessment_by_id = {str(item["claim_id"]): item for item in assessments}
    claims_by_requirement: dict[str, list[dict[str, Any]]] = {}
    for claim in _flatten_requirement_claims(requirements):
        claims_by_requirement.setdefault(str(claim["requirement_id"]), []).append(claim)
    mappings: list[dict[str, Any]] = []
    for requirement in requirements:
        parent_id = str(requirement.get("id", ""))
        parent_claims = claims_by_requirement.get(parent_id, [])
        claim_results = [
            assessment_by_id[item["claim_id"]]
            for item in parent_claims
            if item["claim_id"] in assessment_by_id
        ]
        statuses = [item["status"] for item in claim_results]
        if statuses and all(status == "implemented" for status in statuses):
            status = "implemented"
        elif statuses and all(value == "missing" for value in statuses):
            status = "missing"
        elif any(value in {"partial", "missing"} for value in statuses):
            status = "partial"
        elif "implemented" in statuses and "undetermined" in statuses:
            # 同一需求下已有部分声明被源码证明、部分声明仍缺精确证据时，需求整体是
            # “部分实现”，不是把已确认的实现一起抹成“无法判断”。只有全部 claim 都
            # 无法判断时，才汇总为 undetermined。
            status = "partial"
        else:
            status = "undetermined"
        interface_ids = list(dict.fromkeys(
            value for result in claim_results for value in result["interface_ids"]
        ))
        evidence_refs = list(dict.fromkeys(
            value for result in claim_results for value in result["evidence_refs"]
        ))
        claim_ids = [str(item["claim_id"]) for item in claim_results]
        claim_counts = {
            "total": len(parent_claims),
            "implemented": sum(item.get("status") == "implemented" for item in claim_results),
            "partial": sum(item.get("status") == "partial" for item in claim_results),
            "missing": sum(item.get("status") == "missing" for item in claim_results),
            "undetermined": sum(item.get("status") == "undetermined" for item in claim_results),
        }
        note_parts = []
        note_parts.extend(value for result in claim_results for value in result["unmet_conditions"])
        note_parts.extend(value for result in claim_results for value in result["verification_needed"])
        mapping: dict[str, Any] = {
            "requirement_id": parent_id,
            "status": status,
            # 需求级置信度取 claim 最小值（最薄弱一环决定整体），不用平均——
            # 平均会让多条高分 claim 稀释掉一条确定缺失的低可信结论。
            "confidence": round(min(float(item["confidence"]) for item in claim_results), 2) if claim_results else 0.0,
            "interface_ids": interface_ids,
            "claim_ids": claim_ids,
            "claim_counts": claim_counts,
            "claim_assessments": claim_results,
            "evidence": {"refs": evidence_refs, "note": "；".join(dict.fromkeys(note_parts))},
        }
        if interface_ids:
            mapping["interface_id"] = interface_ids[0]
        mappings.append(mapping)
    return mappings


def _derive_deterministic_checks(
    requirements: list[dict[str, Any]], code_evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """声明级证据门槛：候选证据按声明相关性筛选，禁止全仓类别命中冒充相关证据。"""
    checks: list[dict[str, Any]] = []
    for claim in _flatten_requirement_claims(requirements):
        categories = _required_evidence_categories({
            "title": claim["requirement_title"],
            "description": claim["statement"],
            "acceptance_criteria": claim["acceptance_criteria"],
        })
        relevant = _select_evidence_for_claims([claim], code_evidence, limit=12)
        for category in sorted(categories):
            evidence_ids = [
                str(item["evidence_id"])
                for item in relevant
                if item.get("category") == category and item.get("evidence_id")
            ]
            found = bool(evidence_ids)
            checks.append(
                {
                    "requirement_id": claim["requirement_id"],
                    "claim_id": claim["claim_id"],
                    "rule_id": f"EVID-{category.upper()}",
                    "category": category,
                    "status": "evidence_found" if found else "needs_verification",
                    "evidence_ids": evidence_ids,
                    "description": (
                        f"已提取到 {category} 相关代码/配置片段，仍需结合需求语义核对"
                        if found
                        else f"需求涉及 {category} 行为，但扫描范围未提取到直接代码/配置证据"
                    ),
                }
            )
    return checks


def _cap_confidence(record: dict[str, Any], cap: float = 0.45) -> None:
    """确定性门槛封顶置信度，并保留封顶前原值（raw_confidence / confidence_capped）。

    仅在实际压低数值时留痕；setdefault 保证多个门槛连续触发时原值不被覆盖。
    """
    current = float(record.get("confidence", 0.0))
    if current > cap:
        record.setdefault("raw_confidence", current)
        record["confidence_capped"] = True
        record["confidence"] = cap


def _apply_claim_deterministic_gates(
    assessments: list[dict[str, Any]], checks: list[dict[str, Any]], scan_complete: bool
) -> list[dict[str, Any]]:
    """降级证据不充分的确定结论，并记录具体待核查原因。"""
    checks_by_claim: dict[str, list[dict[str, Any]]] = {}
    for check in checks:
        claim_id = check.get("claim_id")
        if claim_id:
            checks_by_claim.setdefault(str(claim_id), []).append(check)
    for assessment in assessments:
        if assessment.get("status") == "missing" and not scan_complete:
            assessment["status"] = "undetermined"
            _cap_confidence(assessment)
            needed = assessment.setdefault("verification_needed", [])
            note = "仓库扫描覆盖不完整，不能仅凭当前未发现实现判定功能缺失。"
            if note not in needed:
                needed.append(note)
        if assessment.get("status") not in {"implemented", "partial", "missing"}:
            continue
        cited = set(assessment.get("evidence_refs") or [])
        uncovered = [
            check for check in checks_by_claim.get(str(assessment.get("claim_id")), [])
            if check.get("status") != "evidence_found"
            or not cited.intersection(check.get("evidence_ids") or [])
        ]
        if not uncovered:
            continue
        assessment["status"] = "undetermined"
        _cap_confidence(assessment)
        needed = assessment.setdefault("verification_needed", [])
        categories = "、".join(str(check["category"]) for check in uncovered)
        note = f"确定性证据门槛：当前声明的结论缺少相关 {categories} 代码/配置证据，须人工核查。"
        if note not in needed:
            needed.append(note)
    return assessments


def _checks_for_batch(
    checks: list[dict[str, Any]], requirements: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    req_ids = {str(item.get("id", "")) for item in requirements}
    return [check for check in checks if check["requirement_id"] in req_ids]


def _annotate_evidence_quality(
    mappings: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    call_chains: list[dict[str, Any]],
) -> None:
    """证据质量分级：不把接口签名或文件名当作完整实现证据。"""
    missing = {
        str(check["requirement_id"])
        for check in checks
        if check["status"] == "needs_verification"
    }
    chained_interfaces = {str(chain["interface_id"]) for chain in call_chains}
    for mapping in mappings:
        req_id = str(mapping["requirement_id"])
        interface_id = str(mapping.get("interface_id", ""))
        evidence_refs = set((mapping.get("evidence") or {}).get("refs", []))
        if mapping.get("status") == "undetermined" or req_id in missing:
            mapping["evidence_quality"] = "insufficient"
        elif evidence_refs:
            mapping["evidence_quality"] = "behavior_path"
        elif interface_id and interface_id in chained_interfaces:
            mapping["evidence_quality"] = "behavior_path"
        elif interface_id:
            mapping["evidence_quality"] = "interface_only"
        else:
            mapping["evidence_quality"] = "none"


def _append_review_progress(
    store: ArtifactStore, job_id: str, index: int, total: int, count: int
) -> None:
    """长批次调用前落一条无敏感信息的进度事件，供总览实时展示。"""
    entry = {
        "call_id": f"REVIEW-{index:03d}",
        "timestamp": datetime.now(UTC).isoformat(),
        "node": NODE_NAME,
        "event": "started",
        "batch": {"index": index, "total": total, "claims": count},
        "ok": True,
        "duration_ms": 0,
        "tokens": None,
    }
    path = store.job_dir(job_id) / "review_log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _derive_change_scope(
    inventory: dict[str, Any],
    interfaces: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """视图二（C-09）：变更接口 × 需求覆盖（程序化派生，不经 LLM）。

    关联口径：interface.code_location.{repo, file} 命中仓库 changed_files
    （added/modified，文件精确匹配仓内相对路径）；covered_by 从映射表回填，
    空 = 变更接口未被任何需求覆盖（变更风险信号）。removed 文件被测侧
    无源码可关联接口 → 单列 removed_files；未命中任何接口定义文件的
    变更文件（业务逻辑层变更等）→ 单列 unmatched_changed_files。
    """
    changed_by_repo: dict[str, dict[str, str]] = {}
    removed: list[dict[str, Any]] = []
    repositories = inventory.get("repositories") or []
    for repo in repositories:
        if not isinstance(repo, dict):
            continue
        name = str(repo.get("name", ""))
        files = repo.get("changed_files") or {}
        if not isinstance(files, dict):
            continue
        file_map: dict[str, str] = {}
        for file in files.get("added") or []:
            file_map[normalize_path(file)] = "added"
        for file in files.get("modified") or []:
            file_map[normalize_path(file)] = "modified"
        changed_by_repo[name] = file_map
        for file in files.get("removed") or []:
            removed.append({"repo": name, "file": normalize_path(file)})

    if not changed_by_repo and not removed:
        return [], [], []

    # 映射表反向：interface_id → 覆盖它的 REQ 清单与 Claim 明细。
    reqs_by_interface: dict[str, list[str]] = {}
    claims_by_interface: dict[str, list[dict[str, Any]]] = {}
    for entry in mappings:
        interface_ids = entry.get("interface_ids") or ([entry["interface_id"]] if entry.get("interface_id") else [])
        for if_id in interface_ids:
            interface_id = str(if_id)
            requirement_id = str(entry["requirement_id"])
            reqs_by_interface.setdefault(interface_id, []).append(requirement_id)
            claim_assessments = [
                item for item in entry.get("claim_assessments") or []
                if isinstance(item, dict)
                and (
                    interface_id in {str(value) for value in item.get("interface_ids") or []}
                    or not item.get("interface_ids")
                )
            ]
            # 老版本映射没有 Claim 明细时，保持视图二的旧输出形状；只有
            # 真正存在原子声明评审结果时，才展示 covered_claims。
            if claim_assessments:
                claims_by_interface.setdefault(interface_id, []).append(
                    {
                        "requirement_id": requirement_id,
                        "claim_ids": [str(item.get("claim_id")) for item in claim_assessments],
                        "status": str(entry.get("status", "undetermined")),
                    }
                )

    change_scope: list[dict[str, Any]] = []
    matched: set[tuple[str, str]] = set()  # 已被某个接口命中的 (repo, file)
    for interface in interfaces:
        location = interface.get("code_location") or {}
        repo_name = str(location.get("repo", ""))
        file = str(location.get("file", ""))
        repo_changes = changed_by_repo.get(repo_name)
        if repo_changes is None:
            continue
        related_hits = _changed_related_files(interface, repo_changes)
        defined_changed = file in repo_changes
        if not defined_changed and not related_hits:
            continue
        if_id = str(interface.get("id", ""))
        entry: dict[str, Any] = {
            "interface_id": if_id,
            "repo": repo_name,
            "file": file,
            # change 的取值域只有 added/modified：接口定义文件没改时取关联文件的变更类型，
            # 具体来源由 matched_via / related_changed_files 说清。
            "change": repo_changes.get(file) or related_hits[0]["change"],
            "matched_via": "definition" if defined_changed else "related_file",
            "covered_by": list(dict.fromkeys(reqs_by_interface.get(if_id, []))),
        }
        covered_claims = claims_by_interface.get(if_id, [])
        if covered_claims:
            entry["covered_claims"] = covered_claims
        if related_hits:
            entry["related_changed_files"] = related_hits
        evidence = interface.get("change_evidence")
        if isinstance(evidence, dict) and evidence.get("level"):
            entry["evidence_level"] = str(evidence["level"])
        change_scope.append(entry)
        if defined_changed:
            matched.add((repo_name, file))
        for item in related_hits:
            matched.add((repo_name, item["file"]))

    # 未命中任何接口（含其关联文件）的变更文件（业务逻辑层变更等——行为风险信号）
    unmatched: list[dict[str, Any]] = []
    for repo_name, file_map in changed_by_repo.items():
        for file, change in file_map.items():
            if (repo_name, file) not in matched:
                unmatched.append({"repo": repo_name, "file": file, "change": change})
    return change_scope, removed, unmatched


def _changed_related_files(
    interface: dict[str, Any], repo_changes: dict[str, str]
) -> list[dict[str, Any]]:
    """接口的关联文件（Service/DTO/Entity/Enum…）中落在变更清单里的那些。

    评审 §5：只看 `code_location.file` 会把「依赖改了、Controller 文件没改」的接口漏出
    change_scope，而那些 Service/DTO 变更又全被记成 `unmatched_changed_files`——两头都不对。
    `related_files` 由 java-parser 静态解析产出（Controller 自身 + 注入字段/构造器参数
    类型 + 端点方法参数与返回类型，含 XxxService → XxxServiceImpl 一跳）。
    """
    hits: list[dict[str, Any]] = []
    related = interface.get("related_files")
    if not isinstance(related, list):
        return hits
    for item in related:
        if not isinstance(item, dict):
            continue
        file = str(item.get("file", ""))
        if file and file in repo_changes:
            hits.append(
                {
                    "file": file,
                    "role": str(item.get("role", "other")),
                    "change": repo_changes[file],
                }
            )
    return hits


def _derive_incremental_findings(
    change_scope: list[dict[str, Any]],
    removed_files: list[dict[str, Any]],
    unmatched_files: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """增量评审结论完全由 diff 与映射派生，优先暴露无需求覆盖的改动。"""
    findings: list[dict[str, Any]] = []
    status_by_interface: dict[str, str] = {}
    severity = {"missing": 3, "partial": 2, "undetermined": 1, "implemented": 0}
    for item in mappings:
        interface_ids = item.get("interface_ids") or ([item["interface_id"]] if item.get("interface_id") else [])
        for interface_id in interface_ids:
            key = str(interface_id)
            current = status_by_interface.get(key)
            if current is None or severity.get(str(item.get("status")), 0) > severity.get(current, 0):
                status_by_interface[key] = str(item.get("status"))
    for entry in change_scope:
        interface_id = str(entry["interface_id"])
        status = status_by_interface.get(interface_id)
        if not entry["covered_by"]:
            findings.append(
                {
                    "severity": "P1",
                    "kind": "changed_interface_uncovered",
                    "description": f"变更接口 {interface_id} 未被任何需求条目覆盖",
                    "repo": entry["repo"],
                    "file": entry["file"],
                }
            )
        elif status in {"partial", "missing", "undetermined"}:
            findings.append(
                {
                    "severity": "P1" if status != "undetermined" else "P2",
                    "kind": "changed_interface_risk",
                    "description": f"变更接口 {interface_id} 的需求评审状态为 {status}",
                    "repo": entry["repo"],
                    "file": entry["file"],
                }
            )
    for entry in unmatched_files:
        findings.append(
            {
                "severity": "P2",
                "kind": "changed_behavior_file",
                "description": "业务逻辑文件发生变更，但未命中接口定义，需人工核对影响范围",
                "repo": entry["repo"],
                "file": entry["file"],
            }
        )
    for entry in removed_files:
        findings.append(
            {
                "severity": "P1",
                "kind": "removed_file",
                "description": "被测分支删除文件，需确认能力回退与调用方影响",
                "repo": entry["repo"],
                "file": entry["file"],
            }
        )
    return findings


def _merge_llm_mappings(
    text: str,
    claims: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
    code_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """解析 claim 级审查并验证输入 ID 覆盖及证据引用。

    Raises:
        PipelineError: X-LLM-02——非法 JSON、漏审 claim、虚构引用或结构错误。
    """
    try:
        data = _parse_review_json(text)
    except json.JSONDecodeError as exc:
        raise PipelineError(
            code=X_LLM_02,
            message=f"LLM 输出不是合法 JSON：{exc}",
            node_id=NODE_NAME,
        ) from exc
    raw_assessments = data.get("assessments") if isinstance(data, dict) else None
    if not isinstance(raw_assessments, list):
        raise PipelineError(
            code=X_LLM_02,
            message='LLM 输出缺少 assessments 数组（根对象须为 {"assessments": [...]}）',
            node_id=NODE_NAME,
        )

    expected = {str(item["claim_id"]) for item in claims}
    interface_ids = {str(item["id"]) for item in interfaces if item.get("id")}
    evidence_ids = {str(item["evidence_id"]) for item in code_evidence if item.get("evidence_id")}
    covered: set[str] = set()
    assessments: list[dict[str, Any]] = []
    for raw in raw_assessments:
        if not isinstance(raw, dict):
            raise PipelineError(
                code=X_LLM_02,
                message="LLM 输出的 assessments 含非对象条目",
                node_id=NODE_NAME,
            )
        claim_id = str(raw.get("claim_id", ""))
        if claim_id not in expected:
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 引用了不存在或不属于本批次的 claim：{claim_id}（禁止虚构）",
                node_id=NODE_NAME,
            )
        if claim_id in covered:
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 对同一 claim 输出多个评审条目：{claim_id}",
                node_id=NODE_NAME,
            )
        covered.add(claim_id)

        status = str(raw.get("status", ""))
        if status not in _VALID_STATUSES:
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 输出了非法 status：{status}（四态之外）",
                node_id=NODE_NAME,
            )

        raw_interface_ids = raw.get("interface_ids", [])
        raw_evidence_refs = raw.get("evidence_refs", [])
        if not isinstance(raw_interface_ids, list) or not isinstance(raw_evidence_refs, list):
            raise PipelineError(code=X_LLM_02, message="评审引用必须为数组", node_id=NODE_NAME)
        used_interfaces = list(dict.fromkeys(str(value) for value in raw_interface_ids))
        used_evidence = list(dict.fromkeys(str(value) for value in raw_evidence_refs))
        if any(value not in interface_ids for value in used_interfaces):
            invalid = sorted(set(used_interfaces) - interface_ids)
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 引用了不存在的接口：{invalid}（禁止虚构）",
                node_id=NODE_NAME,
            )
        if any(value not in evidence_ids for value in used_evidence):
            invalid = sorted(set(used_evidence) - evidence_ids)
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 引用了不存在的代码证据：{invalid}（禁止虚构）",
                node_id=NODE_NAME,
            )
        confidence = raw.get("confidence")
        if not isinstance(confidence, (float, int)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            raise PipelineError(code=X_LLM_02, message="LLM 输出的 confidence 不在 0 到 1 范围", node_id=NODE_NAME)
        # 封顶留痕：记录首次被确定性门槛压低前的原值（仅实际压低时写入产物）
        raw_confidence: float | None = None
        verification = raw.get("verification_needed", [])
        if not isinstance(verification, list) or any(not isinstance(entry, str) for entry in verification):
            raise PipelineError(code=X_LLM_02, message="LLM 输出的 verification_needed 必须为字符串数组", node_id=NODE_NAME)
        if status in {"implemented", "partial", "missing"} and not used_evidence:
            # 接口 ID、路径线索或搜索未命中都不是行为证据。
            status = "undetermined"
            if float(confidence) > 0.45:
                raw_confidence = float(confidence)
                confidence = 0.45
            verification = list(verification)
            verification.append("需要提供可验证的实现代码/配置证据；当前判断没有引用证据片段。")
        arrays: dict[str, list[str]] = {}
        for key in ("met_conditions", "unmet_conditions"):
            value = raw.get(key, [])
            if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
                raise PipelineError(
                    code=X_LLM_02,
                    message=f"LLM 输出的 {key} 必须为字符串数组",
                    node_id=NODE_NAME,
                )
            arrays[key] = value
        missing_explanation = (
            status == "implemented" and not arrays["met_conditions"]
        ) or (
            status == "partial" and (not arrays["met_conditions"] or not arrays["unmet_conditions"])
        ) or (
            status == "missing" and not arrays["unmet_conditions"]
        )
        if missing_explanation:
            status = "undetermined"
            if float(confidence) > 0.45:
                if raw_confidence is None:
                    raw_confidence = float(confidence)
                confidence = 0.45
            verification = list(verification)
            verification.append("结论缺少与状态相匹配的已满足/未满足条件说明，需结合源码证据复核。")
        if status == "undetermined" and not verification:
            verification = ["当前证据不足以判定该声明，请核对相关实现、配置或运行行为。"]
        assessment: dict[str, Any] = {
            "claim_id": claim_id,
            "status": status,
            "confidence": float(confidence),
            "interface_ids": used_interfaces,
            "evidence_refs": used_evidence,
            "met_conditions": arrays["met_conditions"],
            "unmet_conditions": arrays["unmet_conditions"],
            "verification_needed": verification,
        }
        if raw_confidence is not None:
            assessment["raw_confidence"] = raw_confidence
            assessment["confidence_capped"] = True
        assessments.append(assessment)

    missing_coverage = sorted(expected - covered)
    if missing_coverage:
        raise PipelineError(
            code=X_LLM_02,
            message=f"Code Review claim 覆盖率不足 100%，缺：{missing_coverage}",
            node_id=NODE_NAME,
        )
    if not assessments:
        raise PipelineError(
            code=X_LLM_02,
            message="LLM 输出的 assessments 为空（输入 claims 为零时不应到达本节点）",
            node_id=NODE_NAME,
        )
    return assessments


def _merge_claim_assessments_compat(
    text: str,
    claims: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
    code_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """优先解析 claim 契约，必要时把旧 REQ 映射无损展开到 claim。"""
    try:
        return _merge_llm_mappings(text, claims, interfaces, code_evidence)
    except PipelineError as exc:
        if exc.code != X_LLM_02 or "缺少 assessments" not in exc.message:
            raise
    try:
        data = _parse_review_json(text)
    except json.JSONDecodeError as exc:
        raise PipelineError(code=X_LLM_02, message=f"LLM 输出不是合法 JSON：{exc}", node_id=NODE_NAME) from exc
    raw_mappings = _extract_mapping_items(data)
    if raw_mappings is None:
        raise PipelineError(
            code=X_LLM_02,
            message='LLM 输出缺少 assessments 数组（兼容映射也未找到 mappings）',
            node_id=NODE_NAME,
        )
    by_requirement = {
        str(item.get("requirement_id", "")): item
        for item in raw_mappings
        if isinstance(item, dict)
    }
    valid_interfaces = {str(item.get("id")) for item in interfaces if item.get("id")}
    assessments: list[dict[str, Any]] = []
    for claim in claims:
        mapping = by_requirement.get(str(claim["requirement_id"]))
        if mapping is None:
            raise PipelineError(
                code=X_LLM_02,
                message=f"Code Review claim 覆盖率不足 100%，缺：{claim['claim_id']}",
                node_id=NODE_NAME,
            )
        status = str(mapping.get("status", "undetermined"))
        if status not in _VALID_STATUSES:
            raise PipelineError(code=X_LLM_02, message=f"LLM 输出了非法 status：{status}", node_id=NODE_NAME)
        ids = mapping.get("interface_ids")
        if ids is None:
            ids = [mapping.get("interface_id")] if mapping.get("interface_id") else []
        if not isinstance(ids, list) or any(str(value) not in valid_interfaces for value in ids):
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 引用了不存在的接口（claim {claim['claim_id']}）",
                node_id=NODE_NAME,
            )
        try:
            confidence = float(mapping.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        assessments.append(
            {
                "claim_id": str(claim["claim_id"]),
                "status": status,
                "confidence": max(0.0, min(1.0, confidence)),
                "interface_ids": list(dict.fromkeys(str(value) for value in ids)),
                "evidence_refs": [],
                "met_conditions": [],
                "unmet_conditions": [],
                "verification_needed": [
                    "历史映射未提供 claim 级代码证据，建议结合当前源码证据复核。"
                ],
            }
        )
    return assessments


def _merge_legacy_mappings(
    text: str,
    requirements: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """读取 schema 1.0 任务的旧 mappings，作为一次性兼容入口。

    该路径仅对没有原子 claims 的历史需求产物开放。它不参与新任务的 claim
    证据门槛，因此不会让新模型绕过声明级审查；输出仍会经过需求覆盖、接口
    存在性和状态枚举校验，并由程序回填接口代码位置。
    """
    try:
        data = _parse_review_json(text)
    except json.JSONDecodeError as exc:
        raise PipelineError(
            code=X_LLM_02,
            message=f"LLM 输出不是合法 JSON：{exc}",
            node_id=NODE_NAME,
        ) from exc
    raw_mappings = _extract_mapping_items(data)
    if raw_mappings is None:
        raise PipelineError(
            code=X_LLM_02,
            message='LLM 输出缺少 mappings 数组（历史任务兼容路径）',
            node_id=NODE_NAME,
        )
    req_ids = {str(item.get("id", "")) for item in requirements}
    if_by_id = {str(item["id"]): item for item in interfaces if item.get("id")}
    covered: set[str] = set()
    mappings: list[dict[str, Any]] = []
    for raw in raw_mappings:
        if not isinstance(raw, dict):
            raise PipelineError(code=X_LLM_02, message="旧 mappings 含非对象条目", node_id=NODE_NAME)
        req_id = str(raw.get("requirement_id", ""))
        if req_id not in req_ids:
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 引用了不存在的需求条目：{req_id}（禁止虚构）",
                node_id=NODE_NAME,
            )
        if req_id in covered:
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 对同一需求输出多个映射条目：{req_id}",
                node_id=NODE_NAME,
            )
        covered.add(req_id)
        status = str(raw.get("status", ""))
        if status not in _VALID_STATUSES:
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 输出了非法 status：{status}（四态之外）",
                node_id=NODE_NAME,
            )
        interface_id = raw.get("interface_id")
        interface = None
        if interface_id is not None:
            interface_id = str(interface_id)
            interface = if_by_id.get(interface_id)
            if interface is None:
                raise PipelineError(
                    code=X_LLM_02,
                    message=f"LLM 引用了不存在的接口：{interface_id}（禁止虚构）",
                    node_id=NODE_NAME,
                )
        entry: dict[str, Any] = {
            "requirement_id": req_id,
            "status": status,
            "confidence": float(raw.get("confidence", 0.0)),
            # 旧版产物没有 claim 级字段；仍补齐 2.0 Schema 的稳定形状，
            # 让历史任务可以继续查看，而不会把旧模型输出当成新契约。
            "interface_ids": [str(interface_id)] if interface_id is not None else [],
            "claim_assessments": [],
            "evidence": {},
        }
        if interface is not None:
            entry["interface_id"] = interface_id
            evidence: dict[str, Any] = {}
            if isinstance(interface.get("code_location"), dict):
                evidence["code_location"] = interface["code_location"]
            note = raw.get("evidence")
            if isinstance(note, dict) and note.get("note"):
                evidence["note"] = str(note["note"])
            entry["evidence"] = evidence
        elif isinstance(raw.get("evidence"), dict) and raw["evidence"].get("note"):
            entry["evidence"] = {"note": str(raw["evidence"]["note"])}
        mappings.append(entry)
    missing = sorted(req_ids - covered)
    if missing:
        raise PipelineError(
            code=X_LLM_02,
            message=f"映射覆盖率不足 100%（R-REVW-01），缺：{missing}",
            node_id=NODE_NAME,
        )
    if not mappings:
        raise PipelineError(code=X_LLM_02, message="旧 mappings 为空", node_id=NODE_NAME)
    return mappings


def _parse_review_json(text: str) -> Any:
    """从模型输出中读取首个完整 JSON 值（对象或数组）。

    通用 ``strip_code_fence`` 只提取 JSON 对象，遇到模型错误地返回裸数组时会
    丢失数组外层；Code Review 在此单独保留数组，以便无损归一后继续执行严格校验。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :].strip()
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3].strip()

    decoder = json.JSONDecoder()
    for start, char in enumerate(stripped):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError:
            continue
        return value
    # 保留原始 json 异常，供上层给用户精确提示。
    return json.loads(stripped)


def _extract_mapping_items(data: Any) -> list[Any] | None:
    """仅解开约定的 Review 外层，不猜测条目字段或放宽业务质量闸门。"""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    for key in _MAPPING_CONTAINER_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            return value
    for wrapper in _MAPPING_WRAPPER_KEYS:
        nested = data.get(wrapper)
        if isinstance(nested, (dict, list)):
            extracted = _extract_mapping_items(nested)
            if extracted is not None:
                return extracted
    return None


def _attach_fallback_risk_levels(
    change_scope: list[dict[str, Any]], incremental_findings: list[dict[str, Any]]
) -> None:
    """把 incremental_findings 的确定性 P1/P2 判定映射为条目级 risk_level 兜底（C-108）。

    finding 不带 interface_id，按 (repo, file) 关联接口类 finding；同一文件命中多条时
    取更高优先级（P1 < P2 按字典序恰好成立）。LLM 影响分析失败时视图二仍有程序派生
    的回归优先级可展示。
    """
    interface_kinds = {"changed_interface_uncovered", "changed_interface_risk"}
    level_by_file: dict[tuple[str, str], str] = {}
    for finding in incremental_findings:
        if str(finding.get("kind", "")) not in interface_kinds:
            continue
        severity = str(finding.get("severity", ""))
        if severity not in _RISK_LEVELS:
            continue
        key = (str(finding.get("repo", "")), str(finding.get("file", "")))
        current = level_by_file.get(key)
        if current is None or severity < current:
            level_by_file[key] = severity
    for entry in change_scope:
        level = level_by_file.get((str(entry.get("repo", "")), str(entry.get("file", ""))))
        if level:
            entry["risk_level"] = level


async def _analyze_change_impact_with_llm(
    llm: SupportsComplete,
    prompts_dir: Path,
    change_scope: list[dict[str, Any]],
    unmatched: list[dict[str, Any]],
    removed_files: list[dict[str, Any]],
    call_chains: list[dict[str, Any]],
    incremental_findings: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """视图二 LLM 影响分析（C-108）：变更总评 + 接口级影响 + 盲区归因。

    与证据语义召回同口径：任何失败（prompt 缺失/调用失败/非 JSON/结构错/全量虚构）
    一律返回 None，回退纯程序视图，节点不因此失败。编号引用经程序校验，虚构条目丢弃。
    """
    try:
        prompt = load_prompt(prompts_dir, _CHANGE_IMPACT_PROMPT)
    except PipelineError as exc:
        logger.warning("变更影响分析 Prompt 加载失败，回退纯程序视图：%s", exc.message)
        return None

    scope_input = change_scope
    if len(scope_input) > _MAX_IMPACT_SCOPE_ENTRIES:
        logger.warning(
            "变更接口 %d 条超过影响分析上限 %d，超限条目仅保留程序派生 risk_level",
            len(scope_input), _MAX_IMPACT_SCOPE_ENTRIES,
        )
        scope_input = scope_input[:_MAX_IMPACT_SCOPE_ENTRIES]
    unmatched_input = unmatched
    if len(unmatched_input) > _MAX_IMPACT_UNMATCHED:
        logger.warning(
            "未匹配变更文件 %d 条超过归因上限 %d，超限条目不做归因",
            len(unmatched_input), _MAX_IMPACT_UNMATCHED,
        )
        unmatched_input = unmatched_input[:_MAX_IMPACT_UNMATCHED]

    scope_interface_ids = {str(item.get("interface_id", "")) for item in scope_input}
    status_by_requirement = {
        str(item.get("requirement_id", "")): str(item.get("status", ""))
        for item in mappings
    }
    chain_summaries = [
        {
            "interface_id": str(chain.get("interface_id", "")),
            "status": chain.get("status"),
            "callees": [
                {"file": item.get("file"), "role": item.get("role")}
                if isinstance(item, dict)
                else str(item)
                for item in (chain.get("callees") or [])[:8]
            ],
        }
        for chain in call_chains
        if str(chain.get("interface_id", "")) in scope_interface_ids
    ]
    user_content = json.dumps(
        {
            "change_scope": scope_input,
            "removed_files": removed_files,
            "unmatched_changed_files": unmatched_input,
            "call_chains": chain_summaries,
            "requirements": [
                {
                    "id": str(item.get("id", "")),
                    "title": str(item.get("title", "")),
                    "review_status": status_by_requirement.get(str(item.get("id", "")), ""),
                }
                for item in requirements
            ],
            "interfaces": [
                {"id": str(item.get("id", "")), "summary": str(item.get("summary", ""))[:120]}
                for item in interfaces
                if item.get("id")
            ],
            "incremental_findings": incremental_findings,
        },
        ensure_ascii=False,
    )
    try:
        result = await llm.complete(_CHANGE_IMPACT_PROMPT, prompt, user_content)
    except PipelineError as exc:
        logger.warning("变更影响分析调用失败，回退纯程序视图：%s", exc.message)
        return None
    try:
        data = json.loads(strip_code_fence(result.text))
    except json.JSONDecodeError:
        logger.warning("变更影响分析输出不是合法 JSON，回退纯程序视图")
        return None
    if not isinstance(data, dict):
        logger.warning("变更影响分析输出不是 JSON 对象，回退纯程序视图")
        return None
    return _validate_change_impact(
        data,
        scope_interface_ids,
        {(str(item.get("repo", "")), str(item.get("file", ""))) for item in unmatched_input},
        {str(item.get("id", "")) for item in interfaces if item.get("id")},
        {str(item.get("id", "")) for item in requirements if item.get("id")},
    )


def _validate_change_impact(
    data: dict[str, Any],
    scope_interface_ids: set[str],
    unmatched_keys: set[tuple[str, str]],
    interface_ids: set[str],
    requirement_ids: set[str],
) -> dict[str, Any] | None:
    """校验 LLM 影响分析输出：编号引用必须真实存在，虚构条目丢弃（与溯源硬校验同口径）。"""
    summary = data.get("change_summary")
    if not isinstance(summary, dict) or str(summary.get("overall_risk", "")) not in _RISK_LEVELS:
        logger.warning("变更影响分析缺少合法 change_summary，回退纯程序视图")
        return None
    change_summary = {
        "nature": str(summary.get("nature", "")),
        "overall_risk": str(summary["overall_risk"]),
        "regression_advice": str(summary.get("regression_advice", "")),
    }

    raw_impacts = data.get("impacts")
    impacts: list[dict[str, Any]] = []
    seen_interfaces: set[str] = set()
    if isinstance(raw_impacts, list):
        for item in raw_impacts:
            if not isinstance(item, dict):
                continue
            interface_id = str(item.get("interface_id", ""))
            risk_level = str(item.get("risk_level", ""))
            impact_summary = str(item.get("summary", "")).strip()
            if (
                interface_id not in scope_interface_ids
                or interface_id in seen_interfaces
                or risk_level not in _RISK_LEVELS
                or not impact_summary
            ):
                logger.warning("影响分析条目被丢弃（虚构/重复/缺字段）：%s", interface_id or "<空>")
                continue
            seen_interfaces.add(interface_id)
            impacts.append(
                {
                    "interface_id": interface_id,
                    "summary": impact_summary,
                    "impact_surface": [
                        str(value) for value in item.get("impact_surface") or [] if str(value)
                    ],
                    "test_suggestion": str(item.get("test_suggestion", "")),
                    "risk_level": risk_level,
                }
            )
    if isinstance(raw_impacts, list) and raw_impacts and not impacts:
        logger.warning("影响分析条目全部无效（疑似整体虚构），回退纯程序视图")
        return None

    raw_attrs = data.get("unmatched_attributions")
    attributions: list[dict[str, Any]] = []
    if isinstance(raw_attrs, list):
        for item in raw_attrs:
            if not isinstance(item, dict):
                continue
            key = (str(item.get("repo", "")), str(item.get("file", "")))
            if key not in unmatched_keys:
                logger.warning("盲区归因引用了不存在的变更文件，丢弃：%s", "/".join(key))
                continue
            attributions.append(
                {
                    "repo": key[0],
                    "file": key[1],
                    "likely_interfaces": [
                        str(value)
                        for value in item.get("likely_interfaces") or []
                        if str(value) in interface_ids
                    ],
                    "likely_requirements": [
                        str(value)
                        for value in item.get("likely_requirements") or []
                        if str(value) in requirement_ids
                    ],
                    "rationale": str(item.get("rationale", "")),
                }
            )
    return {
        "change_summary": change_summary,
        "impacts": impacts,
        "unmatched_attributions": attributions,
    }


def _attach_impact_analysis(
    change_scope: list[dict[str, Any]], impacts: list[dict[str, Any]]
) -> None:
    """把已校验的接口级影响分析回挂到 change_scope 条目。"""
    by_interface = {str(item["interface_id"]): item for item in impacts}
    for entry in change_scope:
        impact = by_interface.get(str(entry.get("interface_id", "")))
        if impact is None:
            continue
        entry["impact_analysis"] = {
            "summary": impact["summary"],
            "impact_surface": impact["impact_surface"],
            "test_suggestion": impact["test_suggestion"],
            "risk_level": impact["risk_level"],
        }


def _attach_unmatched_attributions(
    unmatched: list[dict[str, Any]], attributions: list[dict[str, Any]]
) -> None:
    """把已校验的盲区归因回挂到 unmatched_changed_files 条目。"""
    by_key = {(item["repo"], item["file"]): item for item in attributions}
    for entry in unmatched:
        attr = by_key.get((str(entry.get("repo", "")), str(entry.get("file", ""))))
        if attr is None:
            continue
        entry["attribution"] = {
            "likely_interfaces": attr["likely_interfaces"],
            "likely_requirements": attr["likely_requirements"],
            "rationale": attr["rationale"],
        }


def _change_scope_available(inventory: dict[str, Any]) -> bool:
    """判断是否实际提供了可用的基线/提交变更清单。"""
    return any(
        isinstance(repo, dict) and isinstance(repo.get("changed_files"), dict)
        for repo in inventory.get("repositories") or []
    )
