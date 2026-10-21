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
import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from common.artifacts import ArtifactStore
from common.async_work import ordered_parallel_map
from common.dto_validation import (
    dto_field_contracts,
    dto_precondition_reachability_issue,
    dto_validation_oracle_issue,
    field_validation_annotation,
    invalid_value_for_validation,
    validation_kind,
    value_violates_validation,
)
from common.engine import Engine, JobState
from common.errors import X_LLM_01, X_LLM_02, X_ORCH_01, PipelineError
from common.field_value_contracts import documented_codes
from common.llm import LlmResult
from common.llm_batch import chunk_json_items
from common.llm_repair import complete_with_json_repair
from common.llm_text import strip_code_fence
from common.java_source_citations import entry_assertion_issue, exception_assignment_issue, factory_assertion_issue
from common.oracle_conditions import CONTROL_KINDS, execution_blockers, normalize_expected_behavior, normalize_state_conditions, required_control_kinds
from common.paths import normalize_path
from common.prompts_loader import PromptSpec, load_prompt
from common.response_assertions import (
    evaluate_response_assertions,
    normalize_response_assertions,
    response_assertion_scope,
)

from orchestrator.oracle_diagnostics import build_oracle_diagnostics
from orchestrator.oracle_normalization import (
    blank_request_guards, framework_body_design, normalize_oracle_bookkeeping,
)
from orchestrator.oracle_paths import controller_exits
from orchestrator.oracle_runtime import (
    _derive_runtime_observed_exception_oracles,
    _observe_safe_runtime_exception_contracts,
)

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
# 语义召回按完整证据条目分批请求模型，不裁剪源码片段或候选数量。
_EVIDENCE_SELECT_PROMPT = "code_review_evidence_select"
# 视图二 LLM 影响分析（C-108）：全量输入；响应由 LLM 客户端按协议续接至正常结束。
_CHANGE_IMPACT_PROMPT = "code_review_change_impact"
_TEST_ORACLE_PROMPT = "code_review_test_oracles"
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
    *,
    rest_base_url: str | None = None,
    rest_insecure_tls: bool = True,
    auth: dict[str, Any] | None = None,
    auth_headers: dict[str, str] | None = None,
    max_concurrency: int = 4,
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
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须大于 0")
        # 独立 Claim 有界并行；每条仍逐接口、逐页核验完整证据。
        interface_index = _compact_interfaces(interfaces)
        all_claims = _flatten_requirement_claims(requirements)
        # 声明逐条审查：单条输入/输出便于精确核验，也避免一条超长声明或大量声明
        # 挤掉同批其它条目的模型上下文。批次只拆分，不裁剪任何声明内容。
        review_batches = [[claim] for claim in all_claims]
        behavior_evidence = _behavior_evidence(inventory)
        # 所有接口都进入确定性证据提取；后续 LLM 仍可按需求做有限召回，但不能改变
        # “接口是否被源码扫描覆盖”的事实。
        code_evidence = _collect_code_evidence(inventory, interfaces)
        # Claim 映射和测试预期共享同一份完整方法源码，避免映射阶段只见到 520 字摘要，
        # 到后续 oracle 阶段才读取完整方法。
        code_evidence = _expand_oracle_evidence(inventory, code_evidence, interfaces)
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
        async def review_claim_content(work: tuple[int, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            index, batch = work
            batch_claim_ids = {str(claim["claim_id"]) for claim in batch}
            batch_interfaces = _select_interfaces_for_claims(batch, interface_index)
            batch_interface_ids = {str(item["id"]) for item in batch_interfaces if item.get("id")}
            # 先按语义召回（LLM 重排），无效再退回词面重合。词面召回对「中文 claim
            # vs Java 标识符」基本失效（overlap == 0 直接丢弃），语义层是本节点的关键。
            selected_evidence = await _select_evidence_with_llm(
                llm,
                prompts_dir,
                batch,
                code_evidence,
                batch_interface_ids,
            )
            if not selected_evidence:
                selected_evidence = _select_evidence_for_claims(
                    batch,
                    code_evidence,
                    preferred_interface_ids=batch_interface_ids,
                )
            batch_checks = [
                check for check in deterministic_checks
                if str(check.get("claim_id", "")) in batch_claim_ids
            ]
            if legacy_mode:
                selected_locations = {
                    (str(item.get("repo", "")), str(item.get("file", "")))
                    for item in selected_evidence
                }
                user_content = json.dumps(
                    {
                        "batch": {"index": index, "total": len(claim_batches)},
                        "claims": batch,
                        "interfaces": batch_interfaces,
                        "interfaces_complete": len(batch_interfaces) == len(interface_index),
                        "behavior_evidence": [
                            item for item in behavior_evidence
                            if (str(item.get("repo", "")), str(item.get("file", ""))) in selected_locations
                        ],
                        "code_evidence": selected_evidence,
                        "call_chains": [
                            chain for chain in call_chains
                            if str(chain.get("interface_id", "")) in batch_interface_ids
                        ],
                        "incremental_focus": [
                            item for item in incremental_focus
                            if str(item.get("interface_id", "")) in batch_interface_ids
                        ],
                        "deterministic_checks": batch_checks,
                        "scan_coverage": scan_coverage,
                    },
                    ensure_ascii=False,
                )
                response = await llm.complete(NODE_NAME, prompt, user_content)
                return [], _merge_legacy_mappings(response.text, requirements, interfaces)
            per_interface_assessments: list[dict[str, Any]] = []
            interface_pages = [[item] for item in batch_interfaces] or [[]]
            for interface_index_in_batch, interface_page in enumerate(interface_pages, start=1):
                page_interface_ids = {
                    str(item.get("id", "")) for item in interface_page if item.get("id")
                }
                interface_evidence = [
                    item for item in selected_evidence
                    if not item.get("interface_ids")
                    or page_interface_ids.intersection(
                        str(value) for value in item.get("interface_ids") or []
                    )
                ]
                evidence_fragments = _split_evidence_for_transport(
                    interface_evidence, max_excerpt_chars=4_000
                )
                evidence_pages = chunk_json_items(evidence_fragments, max_chars=8_000) or [[]]
                for evidence_page_index, page_evidence in enumerate(evidence_pages, start=1):
                    page_evidence_ids = {
                        str(item.get("evidence_id", "")) for item in page_evidence
                    }
                    page_checks = [
                        {
                            **check,
                            "evidence_ids": [
                                evidence_id for evidence_id in check.get("evidence_ids", [])
                                if str(evidence_id) in page_evidence_ids
                            ],
                        }
                        for check in batch_checks
                    ]
                    page_locations = {
                        (str(item.get("repo", "")), str(item.get("file", "")))
                        for item in page_evidence
                    }
                    user_content = json.dumps(
                        {
                            "batch": {
                                "claim_index": index,
                                "claim_total": len(claim_batches),
                                "interface_index": interface_index_in_batch,
                                "interface_total": len(interface_pages),
                                "evidence_page": evidence_page_index,
                                "evidence_pages": len(evidence_pages),
                            },
                            "claims": batch,
                            "interfaces": interface_page,
                            # 每次只传一个接口时，若全仓有多个接口，不能误导模型把局部未命中当成全局缺失。
                            "interfaces_complete": (
                                len(interface_index) <= 1 and len(evidence_pages) == 1
                            ),
                            "behavior_evidence": [
                                item for item in behavior_evidence
                                if (str(item.get("repo", "")), str(item.get("file", ""))) in page_locations
                            ],
                            "code_evidence": page_evidence,
                            "call_chains": [
                                chain for chain in call_chains
                                if str(chain.get("interface_id", "")) in page_interface_ids
                            ],
                            "incremental_focus": [
                                item for item in incremental_focus
                                if str(item.get("interface_id", "")) in page_interface_ids
                            ],
                            "deterministic_checks": page_checks,
                            "scan_coverage": scan_coverage,
                        },
                        ensure_ascii=False,
                    )
                    per_interface_assessments.extend(
                        await complete_with_json_repair(
                            llm,
                            NODE_NAME,
                            prompt,
                            user_content,
                            partial(
                                _merge_claim_assessments_compat,
                                claims=batch,
                                interfaces=interface_page,
                                code_evidence=page_evidence,
                            ),
                        )
                    )
            if len(batch) == 1:
                return [
                    _merge_interface_claim_assessments(
                        batch[0], per_interface_assessments, scan_complete=bool(scan_coverage["complete"])
                    )
                ], []
            return per_interface_assessments, []

        async def review_claim(work: tuple[int, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            index, batch = work
            started = time.monotonic()
            _append_review_progress(
                store, state.job_id, index, len(claim_batches), len(batch),
                concurrency=max_concurrency,
            )
            result = await review_claim_content(work)
            _append_review_progress(
                store, state.job_id, index, len(claim_batches), len(batch),
                event="completed", duration_ms=int((time.monotonic() - started) * 1000),
                concurrency=max_concurrency,
            )
            return result

        # worker 数量有限，失败/取消会回收同批所有 worker；结果按原始 Claim 顺序合并。
        claim_results = await ordered_parallel_map(
            list(enumerate(claim_batches, start=1)), review_claim,
            concurrency=max_concurrency,
        )
        for assessments, legacy_mappings in claim_results:
            claim_assessments.extend(assessments)
            mappings.extend(legacy_mappings)
        if legacy_mode:
            deterministic_checks = []

        if not legacy_mode:
            claim_assessments = _apply_claim_deterministic_gates(
                claim_assessments, deterministic_checks, bool(scan_coverage["complete"])
            )
            mappings = _aggregate_claim_assessments(requirements, claim_assessments)
        _annotate_evidence_quality(mappings, deterministic_checks, call_chains)

        oracle_extra_evidence = _collect_oracle_controller_headers(inventory, interfaces)
        oracle_extra_evidence.extend(_collect_oracle_service_evidence(inventory, interfaces))
        oracle_extra_evidence.extend(_collect_oracle_auxiliary_evidence(inventory, interfaces))
        oracle_extra_evidence.extend(_collect_oracle_enum_evidence(inventory, interfaces))
        oracle_extra_evidence.extend(_collect_oracle_dao_evidence(inventory, interfaces))
        oracle_extra_evidence.extend(_collect_oracle_called_method_evidence(
            inventory, interfaces, code_evidence + oracle_extra_evidence
        ))
        oracle_extra_evidence.extend(_collect_oracle_response_evidence(
            inventory, interfaces, code_evidence + oracle_extra_evidence
        ))
        for evidence_index, evidence in enumerate(
            oracle_extra_evidence, start=len(code_evidence) + 1
        ):
            evidence["evidence_id"] = f"EVD-{evidence_index:04d}"
        code_evidence.extend(oracle_extra_evidence)
        try:
            job_input = store.read_json(state.job_id, "job_input.json")
        except FileNotFoundError:
            # CLI 直接运行没有 API 配置产物；所有主动探测/造数能力默认关闭。
            job_input = {}
        runtime_observations: list[dict[str, Any]] = []
        runtime_observation_warnings: list[str] = []
        if job_input.get("observe_runtime_contracts") and rest_base_url:
            runtime_observations, runtime_observation_warnings = (
                await _observe_safe_runtime_exception_contracts(
                    rest_base_url,
                    interfaces,
                    code_evidence,
                    verify=not rest_insecure_tls,
                    auth=auth,
                    auth_headers=auth_headers,
                    first_evidence_index=len(code_evidence) + 1,
                )
            )
            code_evidence.extend(
                item["evidence"] for item in runtime_observations
                if isinstance(item.get("evidence"), dict)
            )
        test_oracles, test_oracle_warnings = await _derive_test_oracles_with_llm(
            llm, prompts_dir, interfaces, code_evidence,
            auto_record_fixtures=bool(
                job_input.get("auto_record_fixtures", False)
            ),
            runtime_observations=runtime_observations,
            control_fixtures=_eligible_control_fixtures(job_input.get("fixtures")),
            claims=all_claims,
            max_concurrency=max_concurrency,
        )
        test_oracle_warnings.extend(runtime_observation_warnings)

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
            "schema_version": "2.2",
            "job_id": state.job_id,
            "mappings": mappings,
            "field_value_contracts": _derive_field_value_contracts(interfaces),
            "test_oracles": test_oracles,
            "test_oracle_warnings": test_oracle_warnings,
            "test_oracle_diagnostics": build_oracle_diagnostics(test_oracle_warnings, test_oracles),
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
                detail = "；".join(str(item) for item in schema_errors)
                suffix = f"：{detail}" if detail else ""
                raise PipelineError(
                    code=X_ORCH_01,
                    message=f"review_mapping.json 程序派生产物未通过 Schema 校验{suffix}",
                    node_id=NODE_NAME,
                    details=exc.details,
                ) from exc
            raise
        if interfaces and not test_oracles:
            # Keep the review artifact for inspection, but stop at the stage that
            # owns the missing contracts. Otherwise every downstream node reports
            # a different missing-artifact error and hides the actual gap.
            source_gap = next(
                (
                    warning for warning in test_oracle_warnings
                    if "异常响应映射缺口" in warning
                ),
                "",
            )
            runtime_gap = next(
                (
                    warning for warning in test_oracle_warnings
                    if "运行实测" in warning or "实测契约" in warning
                ),
                "",
            )
            detail = "；".join(item for item in (source_gap, runtime_gap) if item)
            detail = detail or "请查看 review_mapping.json 的 test_oracle_warnings"
            raise PipelineError(
                code=X_ORCH_01,
                message=f"Code Review 未形成任何源码可证的执行预期；{detail}",
                node_id=NODE_NAME,
            )
        engine.mark_completed(state, NODE_NAME)
        return path
    except PipelineError as exc:
        engine.mark_failed(state, NODE_NAME, exc)
        raise


def _derive_field_value_contracts(interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把接口字段说明整理成可审查的取值证据；未知规则保持未知。"""
    contracts: list[dict[str, Any]] = []
    for interface in interfaces:
        for param in interface.get("params") or []:
            if not isinstance(param, dict) or param.get("in") != "body":
                continue
            for field in param.get("fields") or []:
                if not isinstance(field, dict) or not field.get("name"):
                    continue
                description = str(field.get("description") or "")
                labels, codes, status = (
                    documented_codes(description)
                    if field.get("description_origin") == "source"
                    else ({}, [], "unspecified")
                )
                entry: dict[str, Any] = {
                    "interface_id": str(interface.get("id", "")),
                    "parameter": str(param.get("name", "")),
                    "field": str(field["name"]),
                    "type": str(field.get("type", "unknown")),
                    "value_rule_status": status,
                    "description": description,
                    "description_origin": str(field.get("description_origin") or "unknown"),
                    "documented_values": [
                        {"code": code, "label": label}
                        for label, code in labels.items()
                        if label and label != code
                    ],
                    "example_codes": codes if status == "examples_only" else [],
                    "source_refs": field.get("source_refs") or [],
                }
                if "required" in field:
                    entry["required"] = bool(field["required"])
                if field.get("required_evidence"):
                    entry["required_evidence"] = str(field["required_evidence"])
                if field.get("constraints"):
                    entry["constraints"] = field["constraints"]
                contracts.append(entry)
    return contracts


def _oracle_repo_root(repo: dict[str, Any]) -> Path:
    """预期证据必须来自扫描锁定的 target 快照。"""
    return Path(str(
        repo.get("snapshot_path") or repo.get("source_path") or repo.get("path", "")
    ))


def _expand_oracle_evidence(
    inventory: dict[str, Any],
    evidence_catalog: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从本次 target 快照重读接口方法，保留相同 EVD 编号和可核对的完整引文。"""
    roots = {
        str(repo.get("name", "")): _oracle_repo_root(repo)
        for repo in inventory.get("repositories") or []
        if isinstance(repo, dict)
    }
    interface_by_id = {
        str(interface.get("id", "")): interface for interface in interfaces
    }
    expanded: list[dict[str, Any]] = []
    for evidence in evidence_catalog:
        entry = dict(evidence)
        source_span = entry.pop("_source_span", None)
        if entry.get("category") not in {"interface_implementation", "call_chain_implementation"}:
            expanded.append(entry)
            continue
        root_value = roots.get(str(entry.get("repo", "")))
        if root_value is None or not root_value.is_dir():
            entry["excerpt_truncated"] = True
            expanded.append(entry)
            continue
        root = root_value.resolve()
        source = (root / str(entry.get("file", ""))).resolve()
        if root not in source.parents or source.suffix.lower() not in _REVIEWABLE_SUFFIXES:
            entry["excerpt_truncated"] = True
            expanded.append(entry)
            continue
        spans: list[tuple[int, int]] = []
        for interface_id in entry.get("interface_ids") or []:
            interface = interface_by_id.get(str(interface_id), {})
            if entry.get("category") == "interface_implementation":
                location = interface.get("code_location") or {}
                if (
                    str(location.get("repo", "")) == str(entry.get("repo", ""))
                    and str(location.get("file", "")) == str(entry.get("file", ""))
                ):
                    try:
                        spans.append((int(location["start_line"]), int(location["end_line"])))
                    except (KeyError, TypeError, ValueError):
                        pass
            else:
                for span in interface.get("code_spans") or []:
                    if not isinstance(span, dict) or str(span.get("file", "")) != str(entry.get("file", "")):
                        continue
                    try:
                        exact_span = (int(span["start_line"]), int(span["end_line"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                    if isinstance(source_span, dict):
                        try:
                            expected_span = (
                                int(source_span["start_line"]), int(source_span["end_line"])
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
                        if exact_span == expected_span:
                            spans.append(exact_span)
        # 必须使用候选/调用链 AST 的精确范围。旧产物只有邻近文本窗口时，
        # 不用行号距离猜方法归属，也不把局部片段伪装成完整证据。
        spans = sorted(set(spans))
        if not spans:
            entry["excerpt_truncated"] = True
            expanded.append(entry)
            continue
        start = min(item[0] for item in spans)
        end = max(item[1] for item in spans)
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            entry["excerpt_truncated"] = True
            expanded.append(entry)
            continue
        if start < 1 or end < start or end > len(lines):
            entry["excerpt_truncated"] = True
            expanded.append(entry)
            continue
        excerpt = "\n".join(lines[start - 1 : end])
        excerpt = _SECRET_VALUE.sub(r"\1\2<redacted>", excerpt)
        entry["start_line"] = start
        entry["end_line"] = end
        entry["excerpt_truncated"] = False
        entry["excerpt"] = excerpt
        expanded.append(entry)
    return expanded


def _controller_service_invocations(
    root: Path, interface: dict[str, Any]
) -> set[tuple[str, int]]:
    """Read the method calls in this exact controller span to resolve its service implementation."""
    location = interface.get("code_location") or {}
    relative = str(location.get("file", ""))
    if not relative.endswith(".java"):
        return set()
    source = (root / relative).resolve()
    resolved_root = root.resolve()
    if resolved_root not in source.parents:
        return set()
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
        start, end = int(location["start_line"]), int(location["end_line"])
    except (OSError, KeyError, TypeError, ValueError):
        return set()
    if start < 1 or end < start or end > len(lines):
        return set()
    code = _java_source_mask("\n".join(lines[start - 1 : end]))
    if code is None:
        return set()
    invocations: set[tuple[str, int]] = set()
    pattern = re.compile(
        r"\b[A-Za-z_$][\w$]*Service\s*\.\s*"
        r"(?P<method>[A-Za-z_$][\w$]*)\s*\("
    )
    for match in pattern.finditer(code):
        opening = code.find("(", match.start("method"), match.end())
        closing = _java_matching_delimiter(code, opening, "(", ")")
        if closing is None:
            continue
        count = _java_parameter_count(code[opening + 1 : closing])
        if count is not None:
            invocations.add((match.group("method"), count))
    return invocations


def _collect_oracle_service_evidence(
    inventory: dict[str, Any],
    interfaces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """补上 Java 接口调用对应的实现方法；只引用本次 target 快照中的真实源码。"""
    repos = {
        str(repo.get("name", "")): repo
        for repo in inventory.get("repositories") or []
        if isinstance(repo, dict)
    }
    found: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    source_cache: dict[tuple[str, str], str | None] = {}
    for interface in interfaces:
        interface_id = str(interface.get("id", ""))
        repo_name = str((interface.get("code_location") or {}).get("repo", ""))
        repo = repos.get(repo_name)
        if not interface_id or not repo:
            continue
        root_value = _oracle_repo_root(repo)
        if not root_value.is_dir():
            continue
        root = root_value.resolve()
        service_types = {
            Path(str(item.get("file", ""))).stem
            for item in interface.get("related_files") or []
            if isinstance(item, dict) and item.get("role") == "service"
            and str(item.get("file", "")).endswith("Service.java")
        }
        service_spans = [
            span for span in interface.get("code_spans") or []
            if isinstance(span, dict) and span.get("role") == "service"
        ]
        method_signatures = _controller_service_invocations(root, interface)
        for span in service_spans:
            service_path = str(span.get("file", ""))
            service_type = Path(service_path).stem
            if not service_path.endswith(".java") or not service_type:
                continue
            service_types.add(service_type)
            declaration = (root / service_path).resolve()
            if root not in declaration.parents:
                continue
            try:
                declaration_text = declaration.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            method_name = str(span.get("method_name") or "")
            if method_name:
                parameter_count = _method_span_parameter_count(
                    declaration_text, span, method_name
                )
                if parameter_count is not None:
                    method_signatures.add((method_name, parameter_count))
        if not service_types or not method_signatures:
            continue
        preferred_impls = [
            str(item.get("file", ""))
            for item in interface.get("related_files") or []
            if isinstance(item, dict) and item.get("role") == "service"
            and str(item.get("file", "")).endswith("Impl.java")
        ]
        candidates = preferred_impls or [
            str(item.get("path", "")) for item in repo.get("files") or []
            if isinstance(item, dict) and str(item.get("path", "")).endswith(".java")
        ]
        for relative in candidates:
            if not relative.endswith(".java"):
                continue
            source = (root / relative).resolve()
            if root not in source.parents:
                continue
            cache_key = (repo_name, relative)
            if cache_key not in source_cache:
                try:
                    source_cache[cache_key] = source.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    source_cache[cache_key] = None
            source_text = source_cache[cache_key]
            if source_text is None:
                continue
            if not any(_java_type_implements(source_text, service_type)
                       for service_type in service_types):
                continue
            for method_name, parameter_count in method_signatures:
                extracted = _java_method_excerpt(source_text, method_name, parameter_count)
                if extracted is None:
                    continue
                start_line, end_line, excerpt = extracted
                key = (repo_name, relative, start_line, end_line)
                entry = found.setdefault(key, {
                    "category": "call_chain_implementation",
                    "repo": repo_name,
                    "file": relative,
                    "start_line": start_line,
                    "end_line": end_line,
                    "excerpt": _SECRET_VALUE.sub(r"\1\2<redacted>", excerpt),
                    "excerpt_truncated": False,
                    "interface_ids": [],
                    "role": "service_impl",
                })
                if interface_id not in entry["interface_ids"]:
                    entry["interface_ids"].append(interface_id)
    return list(found.values())


def _collect_oracle_controller_headers(
    inventory: dict[str, Any], interfaces: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """给状态码审查提供类级 REST 注解；方法局部片段通常看不到它们。"""
    roots = {
        str(repo.get("name", "")): _oracle_repo_root(repo)
        for repo in inventory.get("repositories") or []
        if isinstance(repo, dict)
    }
    by_controller: dict[tuple[str, str, str], dict[str, Any]] = {}
    for interface in interfaces:
        location = interface.get("code_location") or {}
        relative = str(location.get("file", ""))
        controller_name = str(interface.get("controller_name") or Path(relative).stem)
        key = (
            str(location.get("repo", "")),
            relative,
            controller_name,
        )
        interface_id = str(interface.get("id", ""))
        if key[0] and key[1].endswith(".java") and key[2] and interface_id:
            group = by_controller.setdefault(key, {"interface_ids": [], "annotations": {}})
            if interface_id not in group["interface_ids"]:
                group["interface_ids"].append(interface_id)
            for annotation in interface.get("controller_annotations") or []:
                if not isinstance(annotation, dict):
                    continue
                try:
                    start = int(annotation["start_line"])
                    end = int(annotation["end_line"])
                    name = str(annotation["name"])
                except (KeyError, TypeError, ValueError):
                    continue
                if start > 0 and end >= start and name:
                    group["annotations"][(name, start, end)] = annotation
    result: list[dict[str, Any]] = []
    for (repo_name, relative, _controller_name), group in by_controller.items():
        root_value = roots.get(repo_name)
        if root_value is None or not root_value.is_dir():
            continue
        root = root_value.resolve()
        source = (root / relative).resolve()
        if root not in source.parents:
            continue
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        header_ranges = sorted(group["annotations"].values(), key=lambda item: (
            int(item.get("start_line", 0)), int(item.get("end_line", 0))
        ))
        excerpts: list[tuple[int, int, str]] = []
        for annotation in header_ranges:
            try:
                start = int(annotation["start_line"])
                end = int(annotation["end_line"])
            except (KeyError, TypeError, ValueError):
                continue
            if start > 0 and end >= start and end <= len(lines):
                excerpts.append((start, end, "\n".join(lines[start - 1 : end])))
        # Older saved interface snapshots may not contain controller_name or
        # controller_annotations. Recover the class-level REST annotations from
        # the source file itself so restarting Code Review does not depend on a
        # stale interface_reconstruct artifact.
        if not excerpts:
            extracted = _java_type_excerpt("\n".join(lines), _controller_name)
            if extracted is None:
                continue
            start, end, type_excerpt = extracted
            type_lines = type_excerpt.splitlines()
            declaration_index = next((index for index, line in enumerate(type_lines)
                                      if re.search(
                                          r"\b(?:class|interface|enum|record)\s+"
                                          + re.escape(_controller_name) + r"\b", line
                                      )), None)
            if declaration_index is None:
                continue
            header_lines = type_lines[: declaration_index + 1]
            excerpts.append((start, start + len(header_lines) - 1, "\n".join(header_lines)))
        for start, end, excerpt in excerpts:
            result.append({
                "category": "interface_implementation",
                "repo": repo_name,
                "file": relative,
                "start_line": start,
                "end_line": end,
                "excerpt": _SECRET_VALUE.sub(r"\1\2<redacted>", excerpt),
                "excerpt_truncated": False,
                "interface_ids": list(group["interface_ids"]),
                "role": "controller_header",
            })
    return result


def _collect_oracle_auxiliary_evidence(
    inventory: dict[str, Any], interfaces: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """补齐请求校验与异常处理；二者会先于控制器业务分支改变响应。"""
    repos = {
        str(repo.get("name", "")): repo
        for repo in inventory.get("repositories") or []
        if isinstance(repo, dict)
    }
    found: dict[tuple[str, str], dict[str, Any]] = {}
    advice_paths_by_repo: dict[str, list[str]] = {}
    for interface in interfaces:
        interface_id = str(interface.get("id", ""))
        location = interface.get("code_location") or {}
        repo_name = str(location.get("repo", ""))
        repo = repos.get(repo_name)
        if not interface_id or not repo or interface.get("protocol") != "rest":
            continue
        root_value = _oracle_repo_root(repo)
        if not root_value.is_dir():
            continue
        root = root_value.resolve()
        paths = [
            (str(item.get("file", "")), "dto_validation")
            for item in interface.get("related_files") or []
            if isinstance(item, dict)
            and item.get("role") == "dto"
            and item.get("repo") == repo_name
        ]
        if repo_name not in advice_paths_by_repo:
            advice_paths: list[str] = []
            for file_entry in repo.get("files") or []:
                if not isinstance(file_entry, dict):
                    continue
                candidate = str(file_entry.get("path", ""))
                source = (root / candidate).resolve()
                if not candidate.endswith(".java") or root not in source.parents:
                    continue
                try:
                    candidate_text = source.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                masked = _java_source_mask(candidate_text)
                if masked and re.search(r"@(?:Rest)?ControllerAdvice\b", masked):
                    advice_paths.append(candidate)
            advice_paths_by_repo[repo_name] = advice_paths
        paths.extend(
            (candidate, "exception_advice")
            for candidate in advice_paths_by_repo[repo_name]
        )
        for relative, role in paths:
            source = (root / relative).resolve()
            if not relative.endswith(".java") or root not in source.parents:
                continue
            try:
                source_text = source.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if role == "exception_advice" and not re.search(
                r"@(?:Rest)?ControllerAdvice\b", _java_source_mask(source_text) or ""
            ):
                continue
            extracted_type = _java_type_excerpt(source_text, Path(relative).stem)
            if extracted_type is None:
                continue
            start, end, excerpt = extracted_type
            key = (repo_name, relative)
            entry = found.setdefault(key, {
                "category": "call_chain_implementation",
                "repo": repo_name,
                "file": relative,
                "start_line": start,
                "end_line": end,
                "excerpt": _SECRET_VALUE.sub(r"\1\2<redacted>", excerpt),
                "excerpt_truncated": False,
                "interface_ids": [],
                "role": role,
            })
            if interface_id not in entry["interface_ids"]:
                entry["interface_ids"].append(interface_id)
    return list(found.values())


def _collect_oracle_response_evidence(
    inventory: dict[str, Any], interfaces: list[dict[str, Any]],
    seed_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve response DTOs, inherited fields and serializer annotations losslessly."""
    repos = {str(repo.get("name", "")): repo for repo in inventory.get("repositories", []) if isinstance(repo, dict)}
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for interface in interfaces:
        location = interface.get("code_location") or {}
        repo_name = str(location.get("repo", ""))
        repo = repos.get(repo_name)
        if repo is None or interface.get("protocol") != "rest":
            continue
        root = _oracle_repo_root(repo).resolve()
        files = [str(f.get("path", "")) for f in repo.get("files", []) if isinstance(f, dict)]
        interface_id = str(interface["id"])
        controller = (root / str(location.get("file", ""))).resolve()
        if root not in controller.parents or not controller.is_file():
            continue
        try:
            controller_source = controller.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        returns = interface.get("returns") or {}
        queue: list[tuple[str, str]] = [(name, controller_source) for name in re.findall(r"[A-Z][A-Za-z0-9_$]*", str(returns.get("type", "")))]
        for evidence in seed_evidence:
            if interface_id not in evidence.get("interface_ids", []):
                continue
            owner = (root / str(evidence.get("file", ""))).resolve()
            if root not in owner.parents or not owner.is_file():
                continue
            try:
                owner_source = owner.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            # Local objects constructed on the response processing path often
            # supply nested DTOs even when the controller returns Object.
            for name in re.findall(r"\bnew\s+([A-Z][A-Za-z0-9_$]*)\s*[(<]", str(evidence.get("excerpt", ""))):
                queue.append((name, owner_source))
        seen: set[str] = set()
        while queue:
            name, owner_source = queue.pop(0)
            imports = dict((qualified.rsplit(".", 1)[-1], qualified) for qualified in re.findall(r"^\s*import\s+([\w.]+)\s*;", owner_source, re.M))
            package = re.search(r"^\s*package\s+([\w.]+)\s*;", owner_source, re.M)
            qualified = imports.get(name) or (f"{package.group(1)}.{name}" if package else "")
            if not qualified or qualified in seen:
                continue
            seen.add(qualified)
            suffix = "/" + qualified.replace(".", "/") + ".java"
            candidates = [file for file in files if ("/" + file).endswith(suffix)]
            if len(candidates) != 1:
                continue
            relative = candidates[0]
            path = (root / relative).resolve()
            if root not in path.parents:
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            span = _java_type_excerpt(source, name)
            if span is None:
                continue
            start, end, excerpt = span
            entry = found.setdefault((repo_name, relative), {
                "category": "call_chain_implementation", "repo": repo_name,
                "file": relative, "start_line": start, "end_line": end,
                "excerpt": _SECRET_VALUE.sub(r"\1\2<redacted>", excerpt),
                "excerpt_truncated": False, "interface_ids": [], "role": "response_dto",
            })
            if interface_id not in entry["interface_ids"]:
                entry["interface_ids"].append(interface_id)
            # Resolve only types actually declared in the DTO source. Complete
            # classes preserve defaults, JSON names and getter/setter mappings.
            masked = _java_source_mask(source) or ""
            dependencies = re.findall(r"\b(?:private|protected|public)\s+(?:(?:static|final|transient)\s+)*([A-Z][\w$]*(?:\s*<[^;=]+>)?)\s+\w+\s*[;=]", masked)
            dependencies.extend(re.findall(r"\bextends\s+([A-Z][\w$]*)", masked))
            for declaration in dependencies:
                queue.extend((nested, source) for nested in re.findall(r"[A-Z][A-Za-z0-9_$]*", declaration))
    return list(found.values())


def _collect_oracle_called_method_evidence(
    inventory: dict[str, Any], interfaces: list[dict[str, Any]],
    seed_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Follow mapped controller and service calls within the locked source snapshot."""
    repos = {
        str(repo.get("name", "")): repo
        for repo in inventory.get("repositories") or [] if isinstance(repo, dict)
    }
    found: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    for interface in interfaces:
        interface_id = str(interface.get("id", ""))
        location = interface.get("code_location") or {}
        repo_name = str(location.get("repo", ""))
        repo = repos.get(repo_name)
        if not interface_id or not repo or interface.get("protocol") != "rest":
            continue
        root_value = _oracle_repo_root(repo)
        if not root_value.is_dir():
            continue
        root = root_value.resolve()
        relative = str(location.get("file", ""))
        source = (root / relative).resolve()
        if not relative.endswith(".java") or root not in source.parents:
            continue
        try:
            source_text = source.read_text(encoding="utf-8")
            lines = source_text.splitlines()
            start = int(location["start_line"])
            end = int(location["end_line"])
        except (OSError, UnicodeDecodeError, KeyError, TypeError, ValueError):
            continue
        if start < 1 or end < start or end > len(lines):
            continue
        controller_method = "\n".join(lines[start - 1 : end])
        controller_code = _java_source_mask(controller_method)
        if controller_code is None:
            continue

        def add(relative_file: str, span: tuple[int, int, str], role: str) -> None:
            span_start, span_end, excerpt = span
            key = (repo_name, relative_file, span_start, span_end)
            entry = found.setdefault(key, {
                "category": "call_chain_implementation", "repo": repo_name,
                "file": relative_file, "start_line": span_start, "end_line": span_end,
                "excerpt": _SECRET_VALUE.sub(r"\1\2<redacted>", excerpt),
                "excerpt_truncated": False, "interface_ids": [], "role": role,
            })
            if interface_id not in entry["interface_ids"]:
                entry["interface_ids"].append(interface_id)

        def add_constants(relative_file: str, owner_source: str, method_text: str) -> None:
            masked_source = _java_source_mask(owner_source)
            if masked_source is None:
                return
            declarations = list(re.finditer(
                r"\b(?:private|public|protected)\s+static\s+final\s+"
                r"[\w<>?,.\s]+?\s+([A-Z][A-Z0-9_]*)\s*=\s*[^;]+;",
                masked_source,
            ))
            counts: dict[str, int] = {}
            for match in declarations:
                counts[match.group(1)] = counts.get(match.group(1), 0) + 1
            for match in declarations:
                name = match.group(1)
                if counts[name] != 1 or not re.search(
                    r"\b" + re.escape(name) + r"\b", method_text
                ):
                    continue
                span_start = owner_source.count("\n", 0, match.start()) + 1
                span_end = owner_source.count("\n", 0, match.end()) + 1
                constant_lines = owner_source.splitlines()[span_start - 1 : span_end]
                add(relative_file, (span_start, span_end, "\n".join(constant_lines)),
                    "called_method_constant")

        # Follow a bounded chain of unique, same-file method declarations. This
        # includes private validation helpers and the methods invoked by a
        # resolved service implementation, without guessing receiver types.
        seeds = [(relative, source_text, controller_method)]
        imports = {
            imported.rsplit(".", 1)[-1]: imported
            for imported in re.findall(
                r"^\s*import\s+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)\s*;",
                source_text, re.M,
            )
        }
        repo_files = [
            str(file.get("path", "")) for file in repo.get("files") or []
            if isinstance(file, dict)
        ]

        # Controller collaborators are often constructor-injected fields. The
        # previous collector followed direct same-file helpers and static
        # Type.method() calls, but skipped `injectedField.method()` entirely.
        # Resolve only declared field types through explicit imports (or the
        # controller's own package), then require a unique source file and
        # unique method signature before admitting the excerpt as call-chain
        # evidence.
        masked_controller = _java_source_mask(source_text)
        package_match = re.search(
            r"^\s*package\s+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*;",
            source_text, re.M,
        )
        package_name = package_match.group(1) if package_match else ""
        field_types: dict[str, str] = {}
        if masked_controller is not None:
            for field in re.finditer(
                r"^\s*(?:private|protected|public)\s+(?:(?:static|final)\s+)*"
                r"(?P<type>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)(?:\s*<[^;\n]+?>)?\s+"
                r"(?P<name>[A-Za-z_$][\w$]*)\s*(?:[;=])",
                masked_controller, re.M,
            ):
                field_types[field.group("name")] = field.group("type")

        def resolve_declared_type(type_name: str) -> list[str]:
            qualified = type_name if "." in type_name else imports.get(type_name, "")
            if not qualified and package_name:
                qualified = f"{package_name}.{type_name}"
            if not qualified:
                return []
            suffix = "/" + qualified.replace(".", "/") + ".java"
            return [candidate for candidate in repo_files
                    if candidate.endswith(suffix) and candidate.endswith(".java")]

        for match in re.finditer(
            r"\b(?P<receiver>[A-Za-z_$][\w$]*)\s*\.\s*"
            r"(?P<method>[A-Za-z_$][\w$]*)\s*\(",
            controller_code,
        ):
            receiver = match.group("receiver")
            declared_type = field_types.get(receiver)
            if not declared_type:
                continue
            candidates = resolve_declared_type(declared_type)
            if len(candidates) != 1:
                continue
            collaborator_file = candidates[0]
            collaborator_path = (root / collaborator_file).resolve()
            if root not in collaborator_path.parents:
                continue
            try:
                collaborator_source = collaborator_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            opening = controller_code.find("(", match.start("method"), match.end())
            closing = _java_matching_delimiter(controller_code, opening, "(", ")")
            if closing is None:
                continue
            helper = _java_method_excerpt(
                collaborator_source,
                match.group("method"),
                _java_parameter_count(controller_code[opening + 1 : closing]),
            )
            if helper is not None:
                add(collaborator_file, helper, "called_method")
                add_constants(collaborator_file, collaborator_source, helper[2])
                seeds.append((collaborator_file, collaborator_source, helper[2]))
            # An injected interface declaration is not its runtime return logic.
            # Admit a source implementation only when its declared interface
            # identity resolves to the same imported type and is unique.
            if re.search(r"\binterface\s+" + re.escape(Path(collaborator_file).stem) + r"\b", collaborator_source):
                implementations: list[tuple[str, str, tuple[int, int, str]]] = []
                interface_type = Path(collaborator_file).stem
                interface_package = re.search(r"^\s*package\s+([\w.]+)\s*;", collaborator_source, re.M)
                qualified_type = f"{interface_package.group(1)}.{interface_type}" if interface_package else interface_type
                for candidate_file in repo_files:
                    if not candidate_file.endswith(".java"):
                        continue
                    candidate_path = (root / candidate_file).resolve()
                    if root not in candidate_path.parents:
                        continue
                    try:
                        candidate_source = candidate_path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    if not _java_type_implements(candidate_source, interface_type):
                        continue
                    candidate_package = re.search(r"^\s*package\s+([\w.]+)\s*;", candidate_source, re.M)
                    same_package = bool(candidate_package and interface_package and candidate_package.group(1) == interface_package.group(1))
                    if not same_package and not re.search(r"\bimport\s+" + re.escape(qualified_type) + r"\s*;", candidate_source):
                        continue
                    implementation = _java_method_excerpt(candidate_source, match.group("method"), _java_parameter_count(controller_code[opening + 1 : closing]))
                    if implementation is not None:
                        implementations.append((candidate_file, candidate_source, implementation))
                if len(implementations) == 1:
                    implementation_file, implementation_source, implementation = implementations[0]
                    add(implementation_file, implementation, "called_method")
                    add_constants(implementation_file, implementation_source, implementation[2])
                    seeds.append((implementation_file, implementation_source, implementation[2]))

        for evidence in seed_evidence:
            if (evidence.get("role") != "service_impl"
                    or interface_id not in evidence.get("interface_ids", [])):
                continue
            service_file = str(evidence.get("file", ""))
            service_path = (root / service_file).resolve()
            if not service_file.endswith(".java") or root not in service_path.parents:
                continue
            try:
                service_text = service_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            seeds.append((service_file, service_text, str(evidence.get("excerpt", ""))))
        for owner_file, owner_source, seed_text in seeds:
            queue = [(seed_text, 0)]
            visited: set[tuple[int, int]] = set()
            while queue:
                method_text, depth = queue.pop(0)
                method_code = _java_source_mask(method_text)
                if method_code is None:
                    continue
                for call in re.finditer(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", method_code):
                    name = call.group(1)
                    if name in {"if", "for", "while", "switch", "catch", "return", "new"}:
                        continue
                    opening = method_code.find("(", call.start(), call.end())
                    closing = _java_matching_delimiter(method_code, opening, "(", ")")
                    if closing is None:
                        continue
                    helper = _java_method_excerpt(
                        owner_source, name,
                        _java_parameter_count(method_code[opening + 1 : closing]),
                    )
                    if helper is None or method_text in helper[2]:
                        continue
                    span = (helper[0], helper[1])
                    if span in visited:
                        continue
                    visited.add(span)
                    add(owner_file, helper, "called_method")
                    add_constants(owner_file, owner_source, helper[2])
                    queue.append((helper[2], depth + 1))

        # Resolve only explicit imports in the locked snapshot. Name-only
        # matches across the repository could bind a call to the wrong class.
        for match in re.finditer(
            r"\b([A-Z][A-Za-z0-9_$]*)\.([A-Za-z_$][\w$]*)\s*\(",
            controller_code,
        ):
            type_name, method_name = match.groups()
            imported = imports.get(type_name)
            if not imported:
                continue
            suffix = "/" + imported.replace(".", "/") + ".java"
            candidates = [file for file in repo_files if ("/" + file).endswith(suffix)]
            if len(candidates) != 1:
                continue
            imported_file = candidates[0]
            imported_source = (root / imported_file).resolve()
            if root not in imported_source.parents:
                continue
            try:
                imported_text = imported_source.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            opening = controller_code.find("(", match.start(), match.end())
            closing = _java_matching_delimiter(controller_code, opening, "(", ")")
            if closing is None:
                continue
            helper = _java_method_excerpt(
                imported_text, method_name,
                _java_parameter_count(controller_code[opening + 1 : closing]),
            )
            if helper is None:
                continue
            add(imported_file, helper, "called_method")
            add_constants(imported_file, imported_text, helper[2])
    return list(found.values())


def _collect_oracle_enum_evidence(
    inventory: dict[str, Any], interfaces: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """沿控制器的 Enum.parse 调用追到允许值集合和未知值处理。"""
    repos = {
        str(repo.get("name", "")): repo
        for repo in inventory.get("repositories") or [] if isinstance(repo, dict)
    }
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for interface in interfaces:
        interface_id = str(interface.get("id", ""))
        location = interface.get("code_location") or {}
        repo_name = str(location.get("repo", ""))
        repo = repos.get(repo_name)
        if not interface_id or not repo or interface.get("protocol") != "rest":
            continue
        root_value = _oracle_repo_root(repo)
        if not root_value.is_dir():
            continue
        root = root_value.resolve()
        controller = (root / str(location.get("file", ""))).resolve()
        if root not in controller.parents:
            continue
        try:
            lines = controller.read_text(encoding="utf-8").splitlines()
            start = int(location.get("start_line") or 0)
            end = int(location.get("end_line") or 0)
        except (OSError, UnicodeDecodeError, TypeError, ValueError):
            continue
        if start < 1 or end < start or end > len(lines):
            continue
        method = "\n".join(lines[start - 1:end])
        masked_method = _java_source_mask(method)
        if masked_method is None:
            continue
        enum_names = set(re.findall(r"\b([A-Za-z_$][\w$]*Enum)\.parse\s*\(", masked_method))
        for enum_name in enum_names:
            for file_entry in repo.get("files") or []:
                if not isinstance(file_entry, dict):
                    continue
                relative = str(file_entry.get("path", ""))
                if Path(relative).name != f"{enum_name}.java":
                    continue
                source = (root / relative).resolve()
                if root not in source.parents:
                    continue
                try:
                    source_text = source.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                extracted_type = _java_type_excerpt(source_text, enum_name)
                if extracted_type is None:
                    continue
                type_start, type_end, excerpt = extracted_type
                key = (repo_name, relative)
                entry = found.setdefault(key, {
                    "category": "call_chain_implementation",
                    "repo": repo_name,
                    "file": relative,
                    "start_line": type_start,
                    "end_line": type_end,
                    "excerpt": _SECRET_VALUE.sub(r"\1\2<redacted>", excerpt),
                    "excerpt_truncated": False,
                    "interface_ids": [],
                    "role": "enum_definition",
                })
                if interface_id not in entry["interface_ids"]:
                    entry["interface_ids"].append(interface_id)
    return list(found.values())


def _collect_oracle_dao_evidence(
    inventory: dict[str, Any], interfaces: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """沿 Service 的 Mapper 调用读取对应 SQL 块，而非把整个 DAO 当作证据。"""
    repos = {
        str(repo.get("name", "")): repo
        for repo in inventory.get("repositories") or []
        if isinstance(repo, dict)
    }
    found: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    mapper_xml_by_repo: dict[str, dict[str, list[Path]]] = {}
    for interface in interfaces:
        interface_id = str(interface.get("id", ""))
        repo_name = str((interface.get("code_location") or {}).get("repo", ""))
        repo = repos.get(repo_name)
        if not interface_id or not repo:
            continue
        root_value = _oracle_repo_root(repo)
        if not root_value.is_dir():
            continue
        root = root_value.resolve()
        service_files = [
            str(item.get("file", ""))
            for item in interface.get("related_files") or []
            if isinstance(item, dict) and item.get("role") == "service"
            and str(item.get("file", "")).endswith("Impl.java")
        ]
        service_spans = [
            span for span in interface.get("code_spans") or []
            if isinstance(span, dict) and span.get("role") == "service"
        ]
        for service_file in service_files:
            source = (root / service_file).resolve()
            if root not in source.parents:
                continue
            try:
                source_text = source.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            method_signatures: set[tuple[str, int]] = _controller_service_invocations(
                root, interface
            )
            for span in service_spans:
                declaration = (root / str(span.get("file", ""))).resolve()
                if root not in declaration.parents:
                    continue
                try:
                    declaration_text = declaration.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                method_name = str(span.get("method_name") or "")
                parameter_count = _method_span_parameter_count(
                    declaration_text, span, method_name
                ) if method_name else None
                if method_name and parameter_count is not None:
                    method_signatures.add((method_name, parameter_count))
            for method_name, parameter_count in method_signatures:
                extracted = _java_method_excerpt(source_text, method_name, parameter_count)
                if extracted is None:
                    continue
                masked_method = _java_source_mask(extracted[2])
                if masked_method is None:
                    continue
                for mapper_var, sql_id in set(re.findall(
                    r"\b([A-Za-z_$][\w$]*Mapper)\.([A-Za-z_$][\w$]*)\s*\(",
                    masked_method,
                )):
                    mapper_stem = mapper_var[0].upper() + mapper_var[1:]
                    if repo_name not in mapper_xml_by_repo:
                        by_stem: dict[str, list[Path]] = {}
                        for xml_file in root.rglob("*.xml"):
                            if "mapper" in {part.lower() for part in xml_file.parts}:
                                by_stem.setdefault(xml_file.stem, []).append(xml_file)
                        mapper_xml_by_repo[repo_name] = by_stem
                    for candidate in mapper_xml_by_repo[repo_name].get(mapper_stem, []):
                        sql_file = candidate.resolve()
                        if root not in sql_file.parents:
                            continue
                        relative = sql_file.relative_to(root).as_posix()
                        try:
                            sql_text = sql_file.read_text(encoding="utf-8")
                        except (OSError, UnicodeDecodeError):
                            continue
                        pattern = re.compile(
                            r"<(select|update|insert|delete)\b[^>]*\bid\s*=\s*"
                            + r"[\"']" + re.escape(sql_id) + r"[\"'][^>]*>.*?</\1>",
                            re.S,
                        )
                        match = pattern.search(sql_text)
                        if match is None:
                            continue
                        start = sql_text.count("\n", 0, match.start()) + 1
                        end = sql_text.count("\n", 0, match.end()) + 1
                        excerpt = match.group(0)
                        key = (repo_name, relative, start, end)
                        entry = found.setdefault(key, {
                            "category": "call_chain_implementation",
                            "repo": repo_name,
                            "file": relative,
                            "start_line": start,
                            "end_line": end,
                            "excerpt": _SECRET_VALUE.sub(r"\1\2<redacted>", excerpt),
                            "excerpt_truncated": False,
                            "interface_ids": [],
                            "role": "dao_sql",
                        })
                        if interface_id not in entry["interface_ids"]:
                            entry["interface_ids"].append(interface_id)
    return list(found.values())


def _java_source_mask(source_text: str) -> str | None:
    """屏蔽 Java 注释和字面量，同时保留字符/换行位置供语法范围定位。"""
    if re.search(r"\\u[0-9a-fA-F]{4}", source_text):
        # Java 先做 Unicode escape 预处理；本地扫描器不模拟该阶段时不猜源码结构。
        return None
    masked = list(source_text)
    index = 0
    size = len(source_text)

    def blank(start: int, end: int) -> None:
        for position in range(start, end):
            if source_text[position] not in "\r\n":
                masked[position] = " "

    while index < size:
        if source_text.startswith("//", index):
            end = source_text.find("\n", index)
            end = size if end < 0 else end
            blank(index, end)
            index = end
            continue
        if source_text.startswith("/*", index):
            end_marker = source_text.find("*/", index + 2)
            if end_marker < 0:
                return None
            end = end_marker + 2
            blank(index, end)
            index = end
            continue
        if source_text.startswith('"""', index):
            start = index
            index += 3
            while index < size:
                if source_text[index] == "\\":
                    index += 2
                    continue
                if source_text.startswith('"""', index):
                    index += 3
                    break
                index += 1
            else:
                return None
            blank(start, index)
            continue
        char = source_text[index]
        if char in {'"', "'"}:
            quote = char
            start = index
            index += 1
            while index < size:
                if source_text[index] == "\\":
                    index += 2
                    continue
                if source_text[index] == quote:
                    index += 1
                    break
                if source_text[index] in "\r\n" and quote == "'":
                    return None
                index += 1
            else:
                return None
            blank(start, index)
            continue
        index += 1
    return "".join(masked)


def _java_matching_delimiter(
    source: str, opening_index: int, opening: str, closing: str
) -> int | None:
    if opening_index < 0 or opening_index >= len(source) or source[opening_index] != opening:
        return None
    depth = 0
    for index in range(opening_index, len(source)):
        if source[index] == opening:
            depth += 1
        elif source[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _java_parameter_count(parameters: str) -> int:
    if not parameters.strip():
        return 0
    parentheses = brackets = braces = angles = 0
    commas = 0
    for char in parameters:
        if char == "(":
            parentheses += 1
        elif char == ")":
            parentheses -= 1
        elif char == "[":
            brackets += 1
        elif char == "]":
            brackets -= 1
        elif char == "{":
            braces += 1
        elif char == "}":
            braces -= 1
        elif char == "<":
            angles += 1
        elif char == ">" and angles:
            angles -= 1
        elif char == "," and not (parentheses or brackets or braces or angles):
            commas += 1
    return commas + 1


def _java_declaration_prefix_start(code: str, offset: int) -> int:
    """找同一类体层级的声明边界，跳过注解参数、数组初始值里的括号。"""
    target_depth = 0
    parentheses = brackets = 0
    for char in code[:offset]:
        if char == "(":
            parentheses += 1
        elif char == ")":
            parentheses = max(0, parentheses - 1)
        elif char == "[":
            brackets += 1
        elif char == "]":
            brackets = max(0, brackets - 1)
        elif char == "{" and parentheses == 0 and brackets == 0:
            target_depth += 1
        elif char == "}" and parentheses == 0 and brackets == 0:
            target_depth = max(0, target_depth - 1)

    depth = parentheses = brackets = 0
    boundary = 0
    for index, char in enumerate(code[:offset]):
        if char == ";" and depth == target_depth and parentheses == 0 and brackets == 0:
            boundary = index + 1
        elif char == "(":
            parentheses += 1
        elif char == ")":
            parentheses = max(0, parentheses - 1)
        elif char == "[":
            brackets += 1
        elif char == "]":
            brackets = max(0, brackets - 1)
        elif char == "{" and parentheses == 0 and brackets == 0:
            if depth == target_depth or depth + 1 == target_depth:
                boundary = index + 1
            depth += 1
        elif char == "}" and parentheses == 0 and brackets == 0:
            depth = max(0, depth - 1)
            if depth == target_depth:
                boundary = index + 1
    return boundary


def _method_span_parameter_count(
    source_text: str, span: dict[str, Any], method_name: str
) -> int | None:
    declared = span.get("parameter_count")
    if isinstance(declared, int) and declared >= 0:
        return declared
    try:
        start = int(span["start_line"])
        end = int(span["end_line"])
    except (KeyError, TypeError, ValueError):
        return None
    lines = source_text.splitlines()
    if start < 1 or end < start or end > len(lines):
        return None
    code = _java_source_mask("\n".join(lines[start - 1 : end]))
    if code is None:
        return None
    pattern = re.compile(r"\b" + re.escape(method_name) + r"\s*\(")
    matches = list(pattern.finditer(code))
    if len(matches) != 1:
        return None
    opening = code.find("(", matches[0].start(), matches[0].end())
    closing = _java_matching_delimiter(code, opening, "(", ")")
    if closing is None:
        return None
    return _java_parameter_count(code[opening + 1 : closing])


def _java_method_excerpt(
    source_text: str, method_name: str, parameter_count: int
) -> tuple[int, int, str] | None:
    """按词法结构提取唯一同名同参数数的方法完整定义；重载不唯一时拒绝猜测。"""
    code = _java_source_mask(source_text)
    if code is None:
        return None
    pattern = re.compile(r"\b" + re.escape(method_name) + r"\s*\(")
    matches: list[tuple[int, int, int]] = []
    for match in pattern.finditer(code):
        opening = code.find("(", match.start(), match.end())
        closing = _java_matching_delimiter(code, opening, "(", ")")
        if closing is None or _java_parameter_count(code[opening + 1 : closing]) != parameter_count:
            continue
        boundary = _java_declaration_prefix_start(code, match.start())
        prefix = code[boundary : match.start()]
        modifiers = list(re.finditer(r"\b(?:public|protected|private)\b", prefix))
        if not modifiers:
            continue
        delimiter = re.search(r"[{};]", code[closing + 1 :])
        if delimiter is None:
            continue
        body_open = closing + 1 + delimiter.start()
        if code[body_open] != "{" or "->" in code[closing + 1 : body_open]:
            continue
        if "=" in code[closing + 1 : body_open]:
            continue
        body_close = _java_matching_delimiter(code, body_open, "{", "}")
        if body_close is None:
            continue
        matches.append((boundary, body_open, body_close))
    if len(matches) != 1:
        return None
    start, _body_open, end = matches[0]
    lines = source_text.splitlines()
    start_line = source_text.count("\n", 0, start) + 1
    end_line = source_text.count("\n", 0, end) + 1
    if start_line < 1 or end_line > len(lines):
        return None
    return start_line, end_line, "\n".join(lines[start_line - 1 : end_line])


def _java_type_implements(source_text: str, interface_name: str) -> bool:
    code = _java_source_mask(source_text)
    if code is None:
        return False
    for match in re.finditer(r"\bclass\s+[A-Za-z_$][\w$]*\b([^{};]*)\{", code):
        header = match.group(1)
        implemented = re.search(r"\bimplements\b(?P<types>[\s\S]*)", header)
        if implemented and re.search(
            r"\b" + re.escape(interface_name) + r"\b", implemented.group("types")
        ):
            return True
    return False


def _java_type_excerpt(
    source_text: str, type_name: str
) -> tuple[int, int, str] | None:
    """按完整类型声明取证，并保留其直接修饰注解，不整文件截前若干字符。"""
    code = _java_source_mask(source_text)
    if code is None:
        return None
    pattern = re.compile(
        r"\b(?:class|interface|enum|record)\s+" + re.escape(type_name) + r"\b"
        r"[^{};]*\{"
    )
    declarations: list[tuple[int, int]] = []
    for match in pattern.finditer(code):
        opening = code.rfind("{", match.start(), match.end())
        closing = _java_matching_delimiter(code, opening, "{", "}")
        if closing is not None:
            declarations.append((match.start(), closing))
    if len(declarations) != 1:
        return None
    start, end = declarations[0]
    lines = source_text.splitlines()
    start_line = source_text.count("\n", 0, start) + 1
    end_line = source_text.count("\n", 0, end) + 1
    if start_line < 1 or end_line > len(lines):
        return None
    # Type annotations such as @RestControllerAdvice are part of the executable
    # contract. Keep directly preceding annotations in the evidence excerpt so
    # downstream oracle derivation can cite what was verified in the full file.
    annotation_line = start_line - 1
    while annotation_line > 0:
        preceding = lines[annotation_line - 1].strip()
        if not preceding:
            annotation_line -= 1
            continue
        if preceding.startswith("@"):
            annotation_line -= 1
            continue
        break
    start_line = annotation_line + 1
    return start_line, end_line, "\n".join(lines[start_line - 1 : end_line])


def _eligible_control_fixtures(fixtures: Any) -> list[dict[str, str]]:
    """Return only complete case-scoped controls, without their endpoint or payload."""
    if not isinstance(fixtures, list):
        return []
    grouped: dict[tuple[str, str, str, str, str], dict[str, int]] = {}
    for item in fixtures:
        if not isinstance(item, dict) or item.get("scope") != "case":
            continue
        kind = str(item.get("control_kind", ""))
        interface_id = str(item.get("interface_id", ""))
        scenario_contains = str(item.get("scenario_contains", ""))
        name = str(item.get("name", ""))
        if (
            kind not in {"downstream_response", "application_config", "service_registry"}
            or not interface_id or not scenario_contains or not name
            or not str(item.get("base_url", "")).startswith(("http://", "https://"))
            or item.get("required", True) is not True
        ):
            continue
        key = (
            name, kind, interface_id, scenario_contains,
            str(item.get("base_url", "")),
        )
        phases = grouped.setdefault(key, {"setup": 0, "teardown": 0})
        phase = str(item.get("phase", "setup"))
        if phase in phases:
            phases[phase] += 1
    return [
        {
            "name": name,
            "kind": kind,
            "interface_id": interface_id,
            "scenario_contains": scenario_contains,
        }
        for (name, kind, interface_id, scenario_contains, _base_url), phases in grouped.items()
        if phases == {"setup": 1, "teardown": 1}
    ]


def _control_fixture_kinds_for_oracle(
    oracle: Any,
    interface_id: str,
    control_fixtures: list[dict[str, Any]],
) -> set[str]:
    if not isinstance(oracle, dict):
        return set()
    scenario = str(oracle.get("scenario", "")).casefold()
    return {
        str(item.get("kind"))
        for item in control_fixtures
        if str(item.get("interface_id", "")) == interface_id
        and str(item.get("scenario_contains", "")).casefold() in scenario
    }


async def _derive_test_oracles_with_llm(
    llm: SupportsComplete,
    prompts_dir: Path,
    interfaces: list[dict[str, Any]],
    code_evidence: list[dict[str, Any]],
    auto_record_fixtures: bool = False,
    runtime_observations: list[dict[str, Any]] | None = None,
    control_fixtures: list[dict[str, Any]] | None = None,
    claims: list[dict[str, Any]] | None = None,
    max_concurrency: int = 1,
) -> tuple[list[dict[str, Any]], list[str]]:
    """按接口把源码行为提炼成可执行预期；证据不足或调用失败时保持关闭。"""
    interface_by_id = {
        str(item.get("id", "")): item for item in interfaces if item.get("id")
    }
    runtime_oracles, runtime_warnings = _derive_runtime_observed_exception_oracles(
        interfaces, code_evidence, runtime_observations or []
    )

    def runtime_observation_covers(candidate: Any, interface_id: str) -> bool:
        """A rejected candidate matching a fixed observed request is already covered."""
        if not isinstance(candidate, dict) or candidate.get("category") != "exception":
            return False
        interface_evidence = [
            item for item in code_evidence
            if interface_id in (item.get("interface_ids") or [])
            and item.get("category") != "runtime_observation"
        ]
        interface = interface_by_id.get(interface_id, {})
        if not candidate.get("branch_id") or _oracle_branch_control_reason(
            candidate, interface_evidence, interface, set()
        ):
            # 同一次请求满足宽泛前置条件，不代表覆盖了下游 null/配置分支。
            return False
        branch = next((branch for branch in _oracle_source_branches(
            interface, {str(item.get("evidence_id", "")): str(item.get("excerpt", ""))
                        for item in interface_evidence if item.get("evidence_id")},
        ) if branch["branch_id"] == candidate["branch_id"]), None)
        if branch is None or branch["control_kind"] != "request":
            return False
        try:
            candidate_preconditions = normalize_response_assertions(
                candidate.get("preconditions", [])
            )
        except ValueError:
            return False

        candidate_preconditions = _canonicalize_request_condition_paths(
            candidate_preconditions, interface,
            "\n".join(
                str(item.get("excerpt", "")) for item in code_evidence
                if interface_id in (item.get("interface_ids") or [])
            ),
        )
        body_definitions = [
            item for item in interface.get("params", [])
            if isinstance(item, dict) and item.get("in") == "body" and item.get("name")
        ]
        if len(body_definitions) != 1:
            return False
        body_name = str(body_definitions[0]["name"])
        for observed in runtime_observations or []:
            if str(observed.get("interface_id", "")) != interface_id:
                continue
            guard = re.sub(r"\s+", "", branch["quote"])
            if not any(
                (re.sub(r"\s+", "", str(quote.get("quote", ""))) in guard
                 or quote.get("evidence_ref") == branch["evidence_ref"])
                for quote in observed.get("source_evidence_quotes") or []
                if isinstance(quote, dict) and quote.get("quote")
            ):
                continue
            params = {body_name: observed.get("request_body")}
            for condition in observed.get("preconditions") or []:
                if condition.get("op") == "equals" and condition.get("path") in {
                    str(param.get("name")) for param in interface.get("params", [])
                    if param.get("in") == "path"
                }:
                    params[str(condition["path"])] = condition.get("value")
            outcomes = evaluate_response_assertions(params, candidate_preconditions)
            if outcomes and all(item.get("passed") is True for item in outcomes):
                return True
        return False

    evidence_by_interface: dict[str, list[dict[str, Any]]] = {}
    truncated_evidence_interfaces: set[str] = set()
    for source_evidence in code_evidence:
        if source_evidence.get("category") not in {
            "interface_implementation", "call_chain_implementation"
        }:
            continue
        for interface_id in source_evidence.get("interface_ids") or []:
            normalized_id = str(interface_id)
            if source_evidence.get("excerpt_truncated"):
                truncated_evidence_interfaces.add(normalized_id)
                continue
            evidence_by_interface.setdefault(normalized_id, []).append(source_evidence)

    warnings: list[str] = []
    warnings.extend(
        f"接口 {interface_id} 的源码证据范围不完整或无法读取，未用于生成可执行预期；需先补齐完整源码范围"
        for interface_id in sorted(truncated_evidence_interfaces)
    )
    eligible: list[dict[str, Any]] = []
    for interface_id, interface in interface_by_id.items():
        evidence: list[dict[str, Any]] = []
        seen_excerpts: set[tuple[str, int, int, str, str]] = set()
        for candidate in evidence_by_interface.get(interface_id, []):
            excerpt_key = (
                str(candidate.get("file", "")),
                int(candidate.get("start_line") or 0),
                int(candidate.get("end_line") or 0),
                str(candidate.get("excerpt", "")),
                str(candidate.get("role", "")),
            )
            if excerpt_key not in seen_excerpts:
                seen_excerpts.add(excerpt_key)
                evidence.append(candidate)
        if not evidence:
            continue
        eligible.append({
            "interface": {
                "id": interface_id,
                "protocol": interface.get("protocol"),
                "signature": interface.get("signature"),
                "params": interface.get("params") or [],
                "returns": interface.get("returns"),
            },
            "evidence": [
                {
                    "evidence_id": item.get("evidence_id"),
                    "category": item.get("category"),
                    "role": item.get("role"),
                    "file": item.get("file"),
                    "start_line": item.get("start_line"),
                    "end_line": item.get("end_line"),
                    "excerpt": item.get("excerpt"),
                    "excerpt_truncated": bool(item.get("excerpt_truncated")),
                }
                for item in evidence
            ],
            "claims": claims or [],
            "control_fixtures": [
                {
                    "name": fixture["name"],
                    "kind": fixture["kind"],
                    "scenario_contains": fixture["scenario_contains"],
                }
                for fixture in (control_fixtures or [])
                if fixture.get("interface_id") == interface_id
            ],
        })

    if not eligible:
        if interface_by_id:
            warnings.append(
                f"{len(interface_by_id)} 个接口没有接口实现源码证据，无法生成可执行预期；请先在 Code Review 补齐证据"
            )
        return runtime_oracles, warnings + runtime_warnings

    parsed: list[dict[str, Any]] = []
    transient_failures: dict[str, str] = {}
    fixture_plans = (
        _derive_record_fixture_plans(interfaces, code_evidence)
        if auto_record_fixtures else []
    )
    fixture_plan_by_interface = {
        str(item.get("interface_id", "")): item
        for item in fixture_plans
        if item.get("interface_id")
    }
    try:
        prompt = load_prompt(prompts_dir, _TEST_ORACLE_PROMPT)
    except (OSError, ValueError) as exc:
        logger.warning("源码预期契约 Prompt 不可用：%s", exc)
        return runtime_oracles, warnings + [
            "源码预期契约 Prompt 不可用；相关用例不会作为缺陷判定依据"
        ] + runtime_warnings
    # 各接口的解析闭包、修复列表和告警完全隔离；同接口修复仍按依赖串行执行。
    async def derive_interface(item: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
        parsed: list[dict[str, Any]] = []
        warnings: list[str] = []
        transient_failures: dict[str, str] = {}
        interface_id = str(item["interface"]["id"])
        try:
            batch_interfaces = [item["interface"]]
            evidence_text_by_interface = {
                str(item["interface"]["id"]): {
                    str(entry["evidence_id"]): str(entry.get("excerpt", ""))
                    for entry in item["evidence"]
                    if entry.get("evidence_id")
                }
            }
            item["branches"] = _oracle_source_branches(
                item["interface"], evidence_text_by_interface[interface_id]
            )
            content = json.dumps({"interfaces": [item]}, ensure_ascii=False)
            rejected_oracles: list[tuple[Any, str]] = []

            def parse_oracles(response: str) -> list[dict[str, Any]]:
                rejected_oracles.clear()
                response = _canonicalize_default_status_citations(response, item)
                response = _remove_empty_path_assertions(response)
                first_pass_rejections: list[tuple[Any, str]] = []
                candidates = _parse_test_oracle_response(
                    response, batch_interfaces, evidence_text_by_interface,
                    rejected=first_pass_rejections,
                )
                for raw, error in first_pass_rejections:
                    repaired = _repair_default_http_status_citation(raw, error, item)
                    if repaired is None:
                        rejected_oracles.append((raw, error))
                        continue
                    try:
                        candidates.extend(_parse_test_oracle_response(
                            json.dumps({"oracles": [repaired]}, ensure_ascii=False),
                            batch_interfaces,
                            evidence_text_by_interface,
                            strict_items=True,
                        ))
                    except PipelineError as exc:
                        rejected_oracles.append((raw, exc.message))
                executable_candidates: list[dict[str, Any]] = []
                for candidate in candidates:
                    issue = _oracle_uncontrolled_state_reason(
                        candidate, item["evidence"], item["interface"],
                        control_fixture_kinds=CONTROL_KINDS,
                    )
                    if issue and _repairable_oracle_quality_issue(issue):
                        candidate["design_status"] = "pending_expectation"
                        candidate["execution_blockers"] = [issue]
                    executable_candidates.append(candidate)
                return executable_candidates

            candidates = await complete_with_json_repair(
                llm, NODE_NAME, prompt, content, parse_oracles,
            )
            derived_dto = _derive_dto_validation_oracles(item)
            deterministic_success = _derive_controller_success_oracle(
                item, fixture_plan_by_interface.get(interface_id)
            )
            repairable_rejections = [
                (raw, error) for raw, error in rejected_oracles
                if not _dto_oracle_replaced_by_deterministic_derivation(
                    raw, derived_dto
                )
                and not _success_oracle_replaced_by_deterministic_derivation(
                    raw, deterministic_success
                )
                and not runtime_observation_covers(raw, interface_id)
            ]
            # 单个坏条目不能抹掉同接口的其它有效分支。针对坏条目单独修复，
            # 确定性 DTO 或成功预期已经替代的候选不重复修复或告警。
            # 其余条目修复仍失败时仅记录该条缺口。
            for raw, error in repairable_rejections:
                preflight_reason = _oracle_candidate_preflight_reason(
                    raw, item["evidence"], item["interface"],
                    control_fixture_kinds=CONTROL_KINDS,
                )
                if (preflight_reason and not _oracle_environment_gap(preflight_reason)
                        and preflight_reason != "源码分支定位已失效，需重新提取本接口的 branch_id"):
                    scenario = str(raw.get("scenario", "")) if isinstance(raw, dict) else ""
                    label = f"“{scenario}”" if scenario else "一条源码预期"
                    warnings.append(
                        f"接口 {interface_id} 的{label}未进入执行清单：{preflight_reason}"
                    )
                    continue
                repair_content = (
                    content + "\n\n仅修复以下一条 oracle；可证明时只输出这一条，"
                    "无法证明则输出空 oracles。不得重复其它分支。\n"
                    + json.dumps({"invalid_oracle": raw, "validation_error": error}, ensure_ascii=False)
                )
                retain_pending_design = False
                try:
                    def parse_single_repair(response: str) -> list[dict[str, Any]]:
                        normalized = _remove_empty_path_assertions(
                            _canonicalize_default_status_citations(response, item)
                        )
                        repaired_oracles = _parse_test_oracle_response(
                            normalized, batch_interfaces, evidence_text_by_interface,
                            strict_items=True,
                        )
                        for repaired_oracle in repaired_oracles:
                            issue = _oracle_uncontrolled_state_reason(
                                repaired_oracle, item["evidence"], item["interface"],
                                control_fixture_kinds=CONTROL_KINDS,
                            )
                            if issue and _repairable_oracle_quality_issue(issue):
                                raise PipelineError(
                                    code=X_LLM_02, message=issue, node_id=NODE_NAME
                                )
                        return repaired_oracles

                    repaired = await complete_with_json_repair(
                        llm, NODE_NAME, prompt, repair_content,
                        parse_single_repair,
                    )
                    candidates.extend(repaired)
                    for repaired_oracle in repaired:
                        warnings.append(
                            f"接口 {interface_id} 的“{repaired_oracle['scenario']}”已自动修复并通过源码校验"
                        )
                    if not repaired:
                        retain_pending_design = True
                        scenario = str(raw.get("scenario", "")) if isinstance(raw, dict) else ""
                        label = f"“{scenario}”" if scenario else "一条源码预期"
                        warnings.append(
                            f"接口 {interface_id} 的{label}修复后仍无可证断言，已隔离该分支：{error}"
                        )
                except (PipelineError, OSError, ValueError) as exc:
                    if isinstance(exc, PipelineError) and exc.code == X_LLM_01:
                        transient_failures[interface_id] = exc.message
                    elif isinstance(exc, PipelineError) and exc.code == X_LLM_02:
                        retain_pending_design = True
                    scenario = str(raw.get("scenario", "")) if isinstance(raw, dict) else ""
                    label = f"“{scenario}”" if scenario else "一条源码预期"
                    warnings.append(
                        f"接口 {interface_id} 的{label}校验失败，已隔离该分支：{exc}"
                    )
                if retain_pending_design:
                    subset = _recover_verified_assertion_subset(
                        raw, batch_interfaces, evidence_text_by_interface,
                    )
                    if subset is not None:
                        recovered, isolated = subset
                        candidates.extend(recovered)
                        for recovered_oracle in recovered:
                            warnings.append(
                                f"接口 {interface_id} 的“{recovered_oracle['scenario']}”已隔离未证实的响应断言 "
                                f"{', '.join(isolated)}；保留 {len(recovered_oracle['assertions'])} 条可证断言，"
                                "已清空需求覆盖声明；被隔离字段未计为已验证"
                            )
                        continue
                    pending = _retain_pending_oracle_design(
                        raw, batch_interfaces, evidence_text_by_interface,
                    )
                    if pending is not None:
                        candidates.append(pending)
                        warnings.append(
                            f"接口 {interface_id} 的“{pending['scenario']}”源码分支设计已保留；"
                            "响应断言修复未通过，暂不执行，也不计入业务覆盖"
                        )
            normal = [oracle for oracle in candidates if oracle.get("category") == "normal"]
            if deterministic_success is not None:
                normal.append(deterministic_success)
            if not any((oracle.get("assertions") or oracle.get("expected_status") not in {None, "unresolved"})
                       and not required_control_kinds(oracle)
                       and oracle.get("design_status") != "pending_expectation" for oracle in normal):
                def parse_normal_entry(response: str) -> list[dict[str, Any]]:
                    normal_entries = [oracle for oracle in parse_oracles(response)
                                      if oracle.get("category") == "normal" and not required_control_kinds(oracle)]
                    if not normal_entries:
                        raise PipelineError(code=X_LLM_02, node_id=NODE_NAME,
                                            message="仍缺少普通有效 HTTP 请求的正常返回设计。请单独输出入口校验通过后的常规联调路径；业务响应值未知时保留已有 HTTP/结构检查或 expected_behavior，不要仅返回需要制造下游 null/错误的路径，也不要把业务断言不完整当作删除正常请求的理由。")
                    return normal_entries
                try:
                    focused = await complete_with_json_repair(
                        llm, NODE_NAME, prompt,
                        content + "\n请单独补齐该接口正常联调的返回处理预期。沿调用链追踪实际 return、DTO 字段、setter/getter 和序列化名称。"
                        "验证本接口自己的通道选择、字段映射、结果透传或组装，不要求下游业务一定成功。"
                        "必须至少提供一条普通有效请求的设计，不要只返回需要强制制造下游 null、错误或删配置的场景。"
                        "这条普通入口检查使用 oracle_basis=integration_entry，不填写 branch_id/branch_outcome，"
                        "可确认入口的 HTTP/响应结构、请求编号回显，以及控制器直接写入返回对象的固定字段。"
                        "固定字段必须引用本控制器对实际返回对象的赋值，不能借用另一个异常分支或工厂的值。"
                        "不声称某个内部 if 分支已覆盖，covered_claim_ids=[]。"
                        "仍需引用入口 return、控制器注解、真实 DTO 字段及有效请求条件的原文。"
                        "无法确定业务响应值时，也保留已证实的 HTTP/结构检查，注明业务预期待补；不能因此删除正常请求。"
                        "serialNo/请求编号存在不能证明通道选择或证券代码规则；请求回显可以用 equals_request。"
                        "保留每条前序入口校验所需的有效请求条件。若确实无法确定响应关系，保留 expected_behavior 的待补设计，不编造返回值。",
                        parse_normal_entry,
                    )
                    candidates.extend(focused)
                except PipelineError as exc:
                    if exc.code == X_LLM_01:
                        transient_failures[interface_id] = exc.message
                    else:
                        warnings.append(f"接口 {interface_id} 的正常返回预期待补充：{exc.message}")
            # Weak models tend to repeat a request-id echo when asked to cover
            # a whole interface. Revisit observable request-selected paths one
            # at a time, retaining the complete source input in every call.
            branches_by_id = {branch["branch_id"]: branch for branch in _oracle_source_branches(item["interface"], evidence_text_by_interface[interface_id])}
            refinement_targets: dict[tuple[str, bool], dict[str, Any]] = {}
            for candidate in candidates:
                branch = branches_by_id.get(str(candidate.get("branch_id", "")))
                if (candidate.get("category") != "normal" or candidate.get("assertion_scope") == "business"
                        or branch is None or branch["control_kind"] != "request"):
                    continue
                selectable_input = any(
                    "." in str(condition.get("path", ""))
                    and condition.get("op") in {"equals", "not_equals"}
                    and condition.get("value") is not None
                    for condition in candidate.get("preconditions", [])
                )
                if selectable_input:
                    refinement_targets[(branch["branch_id"], bool(candidate.get("branch_outcome", True)))] = candidate
            for (target_branch_id, target_outcome), target in refinement_targets.items():
                def parse_request_path(response: str) -> list[dict[str, Any]]:
                    refinements = parse_oracles(response)
                    accepted = [oracle for oracle in refinements
                                if oracle.get("category") == "normal"
                                and oracle.get("branch_id") == target_branch_id
                                and oracle.get("branch_outcome", True) == target_outcome
                                and oracle.get("assertion_scope") == "business"
                                and not required_control_kinds(oracle)]
                    if not accepted:
                        raise PipelineError(code=X_LLM_02, node_id=NODE_NAME,
                                            message="当前单分支补查仍没有本接口自身的可观测响应关系；编号回显与 exists 不够。请检查返回 DTO 的默认空值、请求选择导致的结果字段有无及结果组装，不能要求下游返回固定成功码。无法证明时返回空 oracles，原设计会保留。")
                    return accepted
                try:
                    refinements = await complete_with_json_repair(
                        llm, NODE_NAME, prompt,
                        content + "\n现在只补查下面这一条请求可控正常分支，输出至多一条 oracle。保留全部原始源码作为依据。\n"
                        + json.dumps({"target_branch_id": target_branch_id, "branch_outcome": target_outcome,
                                      "request_conditions": target.get("preconditions", [])}, ensure_ascii=False)
                        + "\n验证本接口因该请求选择而组装的响应：哪些结果字段应有内容，哪些未选择的结果字段保持 Java 默认 null（序列化后可用 null_or_missing）。"
                        "读取完整响应 DTO 和赋值路径，不能把仅有请求编号或字段 exists 当作业务断言。"
                        "下游业务失败也是合法输入，不得要求下游 ErrorCode/errCode 等固定为成功码。"
                        "只验证当前目标路径，不能换成下游成功/null 分支或增加需要强制状态的条件。",
                        parse_request_path,
                    )
                    candidates.extend(refinements)
                except PipelineError as exc:
                    if exc.code == X_LLM_01:
                        transient_failures[interface_id] = exc.message
                    else:
                        logger.info("接口 %s 请求分支 %s 的业务预期仍待补，保留原有 HTTP/结构设计", interface_id, target_branch_id)
            # Choose the strongest complete expectation for the same source path.
            # Do not accumulate renamed copies of a generic structure check.
            source_candidates: dict[str, dict[str, Any]] = {}
            for candidate in candidates:
                key = json.dumps({
                    "interface": interface_id, "category": candidate.get("category"),
                    "branch": candidate.get("branch_id", candidate.get("oracle_basis")),
                    "outcome": candidate.get("branch_outcome", True),
                    "preconditions": candidate.get("preconditions", []),
                    "states": sorted((c["kind"], c["mode"], c["evidence_ref"], c["quote"])
                                     for c in candidate.get("state_conditions", [])),
                }, ensure_ascii=False, sort_keys=True)
                rank = {"business": 3, "response_structure": 2, "framework_boundary": 2, "status_only": 1}
                previous = source_candidates.get(key)
                if previous is None or rank.get(candidate.get("assertion_scope", ""), 0) > rank.get(previous.get("assertion_scope", ""), 0):
                    source_candidates[key] = candidate
            unique_candidates = list(source_candidates.values())
            for oracle in unique_candidates:
                fixture_entry = fixture_plan_by_interface.get(interface_id)
                fixture_supports_success = bool(
                    fixture_entry
                    and _fixture_plan_supports_success_oracle(
                        oracle, item["interface"], fixture_entry
                    )
                )
                control_fixture_kinds = _control_fixture_kinds_for_oracle(
                    oracle, interface_id, control_fixtures or []
                )
                state_gap = _oracle_uncontrolled_state_reason(
                    oracle,
                    item["evidence"],
                    item["interface"],
                    fixture_supports_success=fixture_supports_success,
                    control_fixture_kinds=CONTROL_KINDS,
                )
                if state_gap and not _oracle_environment_gap(state_gap) and not _repairable_oracle_quality_issue(state_gap):
                    if runtime_observation_covers(oracle, interface_id):
                        # 只合并同一请求来源分支已有的运行契约；不得按宽泛输入
                        # 把下游状态分支误记为已覆盖。
                        continue
                    # DTO 确定性 oracle 已经覆盖的同一非法输入，不再把模型
                    # 重复提出的 Service 分支记作额外缺口。
                    if _dto_oracle_replaced_by_deterministic_derivation(
                        oracle, derived_dto
                    ):
                        continue
                    warnings.append(f"接口 {interface_id} 的“{oracle['scenario']}”未作为可执行预期：{state_gap}")
                else:
                    oracle["covered_claim_ids"] = [value for value in oracle.get("covered_claim_ids", [])
                                                       if value in {str(claim.get("claim_id")) for claim in claims or []}
                                                       and oracle.get("assertion_scope") == "business"]
                    required = required_control_kinds(oracle)
                    if required:
                        oracle["control_kinds"] = sorted(required)
                    if state_gap:
                        oracle["execution_blockers"] = list(dict.fromkeys(list(oracle.get("execution_blockers") or []) + [state_gap]))
                    design_blockers = execution_blockers(oracle)
                    oracle["execution_blockers"] = design_blockers
                    missing_design_controls = required - control_fixture_kinds
                    oracle["execution_readiness"] = (
                        "blocked" if missing_design_controls or design_blockers else
                        "conditional" if oracle.get("state_conditions") else "ready"
                    )
                    parsed.append(oracle)
            if deterministic_success is not None:
                success_gap = _oracle_uncontrolled_state_reason(
                    deterministic_success,
                    item["evidence"],
                    item["interface"],
                    fixture_supports_success=bool(
                        fixture_plan_by_interface.get(interface_id)
                    ),
                )
                already_covered = any(
                    str(oracle.get("interface_id")) == interface_id
                    and oracle.get("category") == "normal"
                    and any(
                        assertion.get("path") == "returnMsg"
                        and assertion.get("op") == "equals"
                        and assertion.get("value") == deterministic_success["assertions"][0]["value"]
                        for assertion in oracle.get("assertions", [])
                        if isinstance(assertion, dict)
                    )
                    for oracle in parsed
                )
                if not success_gap and not already_covered:
                    parsed.append(deterministic_success)
        except (PipelineError, OSError, ValueError) as exc:
            if isinstance(exc, PipelineError) and exc.code == X_LLM_01:
                transient_failures[interface_id] = exc.message
            logger.warning("接口 %s 源码预期提取失败：%s", interface_id, exc)
            warnings.append(
                f"接口 {interface_id} 源码预期提取失败：{exc}；需在 Code Review 核对源码与测试前置数据"
            )

        return parsed, warnings, transient_failures

    interface_results = await ordered_parallel_map(
        eligible, derive_interface, concurrency=max_concurrency,
    )
    for interface_oracles, interface_warnings, interface_failures in interface_results:
        parsed.extend(interface_oracles)
        warnings.extend(interface_warnings)
        transient_failures.update(interface_failures)

    # 网络/网关故障不能降为普通源码缺口。否则节点会被标记完成，后续用例
    # 生成只看到缺失的 oracle，最终产出一份貌似正常但覆盖不全的报告。
    if transient_failures:
        affected = "、".join(sorted(transient_failures))
        first_reason = next(iter(transient_failures.values()))
        raise PipelineError(
            code=X_LLM_01,
            message=f"源码预期模型调用失败，影响 {affected}；未写入不完整评审结果。{first_reason}",
            node_id=NODE_NAME,
        )

    for item in eligible:
        texts = {str(e["evidence_id"]): str(e.get("excerpt", "")) for e in item["evidence"]}
        boundary = framework_body_design(item["interface"], texts)
        if boundary is not None and not any(o.get("interface_id") == boundary["interface_id"] and o.get("oracle_basis") == "spring_required_body" for o in parsed):
            parsed.extend(_parse_test_oracle_response(json.dumps({"oracles": [boundary]}, ensure_ascii=False), [item["interface"]], {str(item["interface"]["id"]): texts}))
    # Required-body failures and a blank field inside a valid JSON body are
    # separate HTTP inputs. Recover the latter from an exact unconditional guard.
    for item in eligible:
        interface = item["interface"]
        interface_id = str(interface["id"])
        texts = {str(e["evidence_id"]): str(e.get("excerpt", "")) for e in item["evidence"]}
        branches = _oracle_source_branches(interface, texts)
        for guard in blank_request_guards(interface, texts):
            if any(o.get("interface_id") == interface_id and o.get("category") != "normal"
                   and any(c.get("path") == guard["path"] and c.get("op") == "equals" and c.get("value") == ""
                           for c in o.get("preconditions", [])) for o in parsed):
                continue
            branch = next((b for b in branches if guard["quote"].startswith(b["quote"])), None)
            if branch is None:
                continue
            raw = {
                "interface_id": interface_id, "category": "exception",
                "scenario": f"请求体内 {guard['path']} 为空字符串时触发业务校验",
                "branch_id": branch["branch_id"], "branch_outcome": True,
                "preconditions": [{"path": guard["path"], "op": "equals", "value": ""}],
                "expected_status": "unresolved", "assertions": [],
                "expected_behavior": {"description": "字段空白时抛出源码指定业务异常；HTTP 异常映射待确认",
                                      "evidence_ref": guard["evidence_ref"], "quote": guard["quote"]},
                "evidence_refs": [guard["evidence_ref"]],
                "evidence_quotes": [{"target": target, "evidence_ref": guard["evidence_ref"], "quote": guard["quote"]}
                                    for target in ("expected_status", "preconditions[0]")],
            }
            recovered = _parse_test_oracle_response(json.dumps({"oracles": [raw]}, ensure_ascii=False), [interface], {interface_id: texts})
            for oracle in recovered:
                oracle["execution_readiness"] = "blocked"
                oracle["execution_blockers"] = ["本接口业务异常的 HTTP 映射待确认"]
            parsed.extend(recovered)
    parsed.extend(runtime_oracles)
    warnings.extend(runtime_warnings)

    for item in eligible:
        interface_id = str(item["interface"]["id"])
        derived = _derive_dto_validation_oracles(item)
        if derived:
            parsed.extend(derived)
        enum_oracle = _derive_enum_rejection_oracle(item)
        if enum_oracle is not None:
            parsed.append(enum_oracle)

    # A fixture plan proves how to create and clean a record; it does not prove
    # the target endpoint's HTTP status or response assertion. Bind it only to an
    # oracle already validated from source and never synthesize a response oracle.
    bound_fixture_interface_ids: set[str] = set()
    interface_by_id = {str(item.get("id", "")): item for item in interfaces}
    for fixture_plan in fixture_plans:
        message = str(fixture_plan.get("success_message", ""))
        interface_id = str(fixture_plan.get("interface_id", ""))
        matches = [
            oracle for oracle in parsed
            if str(oracle.get("interface_id")) == interface_id
            and oracle.get("category") == "normal"
            and any(item.get("path") == "returnMsg" and item.get("op") == "equals"
                    and str(item.get("value")) == message
                    for item in oracle.get("assertions", []))
        ]
        if matches:
            for oracle in matches:
                oracle["fixture_plan"] = fixture_plan["fixture_plan"]
                oracle["execution_blockers"] = [
                    reason for reason in oracle.get("execution_blockers", [])
                    if not _oracle_environment_gap(reason)
                ]
                oracle["execution_readiness"] = "conditional"
            bound_fixture_interface_ids.add(interface_id)
        else:
            interface = interface_by_id.get(interface_id, {})
            path = str((interface.get("signature") or {}).get("path", ""))
            action = path.rsplit("/", 1)[-1]
            warnings.append(
                f"接口 {interface_id} 的 {action} 成功源码预期未通过审核；"
                "自动造数计划未绑定到未经验证的状态码或响应断言，该成功场景不会执行"
            )
    if bound_fixture_interface_ids:
        warnings = [
            warning for warning in warnings
            if not any(
                f"接口 {interface_id} " in warning and
                ("数据库" in warning or "测试记录" in warning) and
                ("成功" in warning or "校验通过" in warning)
                for interface_id in bound_fixture_interface_ids
            )
        ]
    if auto_record_fixtures:
        interface_by_id = {str(item.get("id", "")): item for item in interfaces}
        success_messages = {"add": "新增成功", "update": "更新成功", "delete": "删除成功"}
        warned_fixture_gaps: set[str] = set()
        for oracle in parsed:
            interface_id = str(oracle.get("interface_id", ""))
            interface = interface_by_id.get(interface_id, {})
            path = str((interface.get("signature") or {}).get("path", ""))
            action = path.rsplit("/", 1)[-1]
            if (
                action not in success_messages
                or oracle.get("category") != "normal"
                or oracle.get("fixture_plan")
                or not any(
                    item.get("path") == "returnMsg" and item.get("op") == "equals"
                    and item.get("value") == success_messages[action]
                    for item in oracle.get("assertions", [])
                )
                or interface_id in warned_fixture_gaps
            ):
                continue
            warned_fixture_gaps.add(interface_id)
            warnings.append(
                f"接口 {interface_id} 的 {action} 成功分支未形成可核验的独立造数与软删除计划；"
                "成功用例设计保留，执行前需准备测试记录；不使用臆造或未经确认的历史 ID"
            )

    by_interface: dict[str, int] = {}
    for oracle in parsed:
        interface_id = str(oracle["interface_id"])
        # Deterministic body/DTO/enum designs are appended after the model loop.
        # Give every final design the same readiness calculation, including
        # unknown HTTP mappings; no item may say ready while carrying blockers.
        oracle["assertion_scope"] = ("framework_boundary" if oracle.get("oracle_basis") == "spring_required_body"
                                     else response_assertion_scope(oracle.get("assertions", [])))
        oracle["execution_blockers"] = execution_blockers(oracle)
        required = required_control_kinds(oracle)
        available = _control_fixture_kinds_for_oracle(oracle, interface_id, control_fixtures or [])
        oracle["control_kinds"] = sorted(required)
        oracle["execution_readiness"] = (
            "blocked" if oracle["execution_blockers"] or required - available else
            "conditional" if oracle.get("state_conditions") or oracle.get("fixture_plan") else "ready"
        )
        by_interface[interface_id] = by_interface.get(interface_id, 0) + 1
        oracle["oracle_id"] = f"ORC-{interface_id}-{by_interface[interface_id]:03d}"
    interfaces_with_oracles = {str(item["interface_id"]) for item in parsed}
    missing = sorted(set(interface_by_id) - interfaces_with_oracles)
    if missing:
        eligible_by_id = {
            str(item["interface"]["id"]): item for item in eligible
        }
        for interface_id in missing:
            missing_item = eligible_by_id.get(interface_id)
            if missing_item is None:
                continue
            evidence = missing_item["evidence"]
            if (
                not any(entry.get("role") == "exception_advice" for entry in evidence)
                and any("throw new " in str(entry.get("excerpt", ""))
                        for entry in evidence if entry.get("role") in {
                            "controller", "called_method", "service_impl",
                        })
            ):
                warnings.append(
                    f"接口 {interface_id} 的异常响应映射缺口：源码能证明抛出异常，"
                    "但本次扫描未找到异常处理器或显式 HTTP 状态；需纳入实际运行时使用的"
                    "异常处理源码，才能确定状态码和响应体"
                )
        warnings.append(
            "以下接口未形成源码可证的执行预期；需在 Code Review 核对实现分支与测试数据，"
            "用例生成不会为其编造预期："
            + ", ".join(missing)
            + (f" 等 {len(missing)} 个" if len(missing) > 40 else "")
        )
    return parsed, list(dict.fromkeys(warnings))


def _derive_record_fixture_plans(
    interfaces: list[dict[str, Any]], evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """从同仓库增/查/改/删源码构造真实记录的 setup/cleanup 计划。"""
    by_path = {
        str(item.get("signature", {}).get("path", "")): item
        for item in interfaces if isinstance(item.get("signature"), dict)
    }
    by_interface: dict[str, list[dict[str, Any]]] = {}
    for item in evidence:
        for interface_id in item.get("interface_ids") or []:
            by_interface.setdefault(str(interface_id), []).append(item)

    def source(interface_id: str, role: str, needle: str) -> dict[str, Any] | None:
        return next((item for item in by_interface.get(interface_id, [])
                     if item.get("role") == role and needle in str(item.get("excerpt", ""))), None)

    result: list[dict[str, Any]] = []
    for target in interfaces:
        signature = target.get("signature") or {}
        path = str(signature.get("path", ""))
        match = re.fullmatch(r"(/[^/]+)/(add|update|delete)", path)
        if not match or str(signature.get("method", "")).upper() != "POST":
            continue
        prefix, action = match.groups()
        add, query, delete = (by_path.get(f"{prefix}/{name}")
                              for name in ("add", "query", "delete"))
        if add is None or query is None or delete is None:
            continue
        target_repo = str((target.get("code_location") or {}).get("repo", ""))
        if not target_repo or any(
            str((item.get("code_location") or {}).get("repo", "")) != target_repo
            for item in (add, query, delete)
        ):
            continue
        ids = {name: str(item.get("id", "")) for name, item in
               (("target", target), ("add", add), ("query", query), ("delete", delete))}
        message = {"add": "新增成功", "update": "更新成功", "delete": "删除成功"}[action]
        target_controller = source(ids["target"], "controller", f'setReturnMsg("{message}")')
        target_service = source(ids["target"], "service_impl", "insertSelective(") if action == "add" else source(
            ids["target"], "service_impl", "selectOneById"
        )
        add_controller = source(ids["add"], "controller", 'setReturnMsg("新增成功")')
        add_service = source(ids["add"], "service_impl", "insertSelective(")
        query_controller = source(ids["query"], "controller", "response.setBody(list)")
        query_sql = source(ids["query"], "dao_sql", "DELETED = 0")
        delete_service = source(ids["delete"], "service_impl", "updateDeletedById")
        delete_sql = source(ids["delete"], "dao_sql", "SET DELETED = ID")
        if (target_controller is None or target_service is None or add_controller is None
                or add_service is None or query_controller is None or query_sql is None
                or delete_service is None or delete_sql is None):
            continue
        target_text = str(target_service["excerpt"])
        if action != "add" and (
            "getDeleted()" not in target_text or "throw new BaseException" not in target_text
        ):
            continue
        add_param = next((p for p in add.get("params", []) if p.get("in") == "body"
                          and isinstance(p.get("fields"), list)), None)
        query_param = next((p for p in query.get("params", []) if p.get("in") == "body"
                            and isinstance(p.get("fields"), list)), None)
        target_param = next((p for p in target.get("params", []) if p.get("in") == "body"
                             and isinstance(p.get("fields"), list)), None)
        delete_param = next((p for p in delete.get("params", []) if p.get("in") == "body"
                             and isinstance(p.get("fields"), list)), None)
        if (add_param is None or query_param is None
                or target_param is None or delete_param is None):
            continue
        add_fields = {str(f.get("name")): f for f in add_param["fields"]}
        target_fields = {str(f.get("name")): f for f in target_param["fields"]}
        query_fields = {str(f.get("name")): f for f in query_param["fields"]}
        id_field = "ids" if action == "delete" else "id"
        if (action == "update" and id_field not in target_fields) or (
            "ids" not in {str(f.get("name")) for f in delete_param["fields"]}
        ):
            continue
        marker_field = next((name for name in ("riskSerial", "remarkText")
                             if name in add_fields
                             and (action != "update" or name in target_fields)), None)
        if not marker_field or "businessType" not in add_fields or "businessType" not in query_fields:
            continue
        add_body: dict[str, Any] = {}
        for name, field in add_fields.items():
            if name == marker_field:
                add_body[name] = "__AUTOTEST_MARKER__"
            elif field.get("required"):
                if field.get("description_origin") != "source":
                    break
                description = str(field.get("description", ""))
                code = re.search(r"(?<!\d)\d{2}(?!\d)", description)
                value = code.group() if code else ("AAA" if "AAA" in description else None)
                if value is None:
                    break
                add_body[name] = [value] if str(field.get("type", "")).startswith("List<") else value
        else:
            if marker_field not in add_body:
                continue
            if action == "update" and any(name not in target_fields for name in add_body):
                continue
            target_body = dict(add_body) if action in {"add", "update"} else {}
            if action == "update":
                target_body[id_field] = "__AUTOTEST_RECORD_ID__"
            elif action == "delete":
                target_body[id_field] = ["__AUTOTEST_RECORD_ID__"]
            query_body = {"businessType": add_body["businessType"]}
            if marker_field in query_fields:
                query_body[marker_field] = "__AUTOTEST_MARKER__"
            plan = {
                "kind": "create_query_soft_delete",
                "add_interface_id": ids["add"],
                "query_interface_id": ids["query"],
                "delete_interface_id": ids["delete"],
                "marker_field": marker_field,
                "add_body": add_body,
                "query_body": query_body,
                "target_body": target_body,
                "id_field": id_field,
                "add_success_message": "新增成功",
                "query_success_message": "查询成功",
                "delete_success_message": "删除成功",
                "cleanup_mode": "soft_delete",
                "source_refs": [str(x["evidence_id"]) for x in
                                (add_controller, add_service, query_controller, query_sql,
                                 delete_service, delete_sql, target_service)],
            }
            result.append({
                "interface_id": ids["target"],
                "success_message": message,
                "fixture_plan": plan,
            })
    return result


def _dto_oracle_replaced_by_deterministic_derivation(
    raw: Any, derived_oracles: list[dict[str, Any]]
) -> bool:
    """Avoid duplicate warnings when a deterministic DTO case covers the same input."""
    if not isinstance(raw, dict):
        return False
    deterministic_conditions = {
        (
            str(condition.get("path", "")),
            json.dumps(condition.get("value"), ensure_ascii=False, sort_keys=True),
        )
        for oracle in derived_oracles
        for condition in oracle.get("preconditions", [])
        if isinstance(condition, dict) and condition.get("op") == "equals"
    }
    raw_conditions = raw.get("preconditions")
    # One exact invalid field is required. Multi-field values can produce a
    # different validation message/order and are not replaced by a single-field case.
    if not isinstance(raw_conditions, list) or len(raw_conditions) != 1:
        return False
    condition = raw_conditions[0]
    if not isinstance(condition, dict) or condition.get("op") != "equals":
        return False
    signature = (
        str(condition.get("path", "")),
        json.dumps(condition.get("value"), ensure_ascii=False, sort_keys=True),
    )
    return signature in deterministic_conditions


def _success_oracle_replaced_by_deterministic_derivation(
    raw: Any, derived_oracle: dict[str, Any] | None
) -> bool:
    """Drop a malformed duplicate when a source-derived success oracle covers it."""
    if (
        not isinstance(raw, dict)
        or derived_oracle is None
        or raw.get("category") != "normal"
        or str(raw.get("expected_status", "")).strip() != derived_oracle.get("expected_status")
    ):
        return False
    expected = (derived_oracle.get("assertions") or [{}])[0]
    return any(
        isinstance(assertion, dict)
        and assertion.get("path") == expected.get("path")
        and assertion.get("op") == expected.get("op")
        and assertion.get("value") == expected.get("value")
        for assertion in raw.get("assertions") or []
    )


def _default_http_status_evidence(
    item: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]] | None:
    """Prove Spring's default 200 for a mapped REST handler returning a plain object."""
    interface = item.get("interface") or {}
    if interface.get("protocol") != "rest":
        return None
    signature = interface.get("signature") or {}
    expected_method = str(signature.get("method", "")).upper()
    expected_path = re.sub(r"/+$", "", str(signature.get("path", "")))
    if not expected_method or not expected_path.startswith("/"):
        return None
    evidence = item.get("evidence") or []
    headers = [entry for entry in evidence if entry.get("role") == "controller_header"]
    controllers = [entry for entry in evidence if entry.get("role") == "controller"]
    for header in headers:
        header_text = str(header.get("excerpt", ""))
        response_body_quote = "@RestController" if "@RestController" in header_text else None
        if response_body_quote is None:
            continue
        # The parser records each class annotation as its own evidence entry.
        # Read the mapping from the same controller file, not just the line
        # containing @RestController.
        class_headers = "\n".join(
            str(entry.get("excerpt", "")) for entry in headers
            if entry.get("file") == header.get("file")
        )
        if "@ResponseStatus" in class_headers:
            continue
        class_mapping = re.search(
            r"@RequestMapping\s*\(\s*(?:(?:value|path)\s*=\s*)?\"([^\"]*)\"",
            class_headers,
        )
        base_path = class_mapping.group(1) if class_mapping else ""
        base_path = "/" + "/".join(part for part in base_path.split("/") if part)
        if base_path == "/":
            base_path = ""
        for controller in controllers:
            if controller.get("file") != header.get("file"):
                continue
            body = str(controller.get("excerpt", ""))
            if controller.get("excerpt_truncated") or "@ResponseStatus" in body:
                continue
            if re.search(
                r"\b(?:ResponseEntity|HttpEntity|ModelAndView|StreamingResponseBody|SseEmitter)\b"
                r"|\b(?:setStatus|sendError)\s*\(|\bHttpStatus\b",
                body,
            ):
                continue
            method_mapping = re.search(
                r"@(Post|Get|Put|Delete|Patch)Mapping\s*(?:\(([^)]*)\))?",
                body,
            )
            mapping_method = None
            mapping_path = None
            if method_mapping:
                mapping_method = {
                    "Post": "POST", "Get": "GET", "Put": "PUT",
                    "Delete": "DELETE", "Patch": "PATCH",
                }[method_mapping.group(1)]
                args = method_mapping.group(2) or ""
                path_match = re.search(
                    r"(?:(?:value|path)\s*=\s*)?\"([^\"]+)\"", args
                )
                mapping_path = path_match.group(1) if path_match else None
            else:
                request_mapping = re.search(r"@RequestMapping\s*\(([^)]*)\)", body)
                if request_mapping:
                    args = request_mapping.group(1)
                    path_match = re.search(
                        r"(?:value|path)\s*=\s*\"([^\"]+)\"|^\s*\"([^\"]+)\"",
                        args,
                    )
                    if path_match:
                        mapping_path = path_match.group(1) or path_match.group(2)
                    method_match = re.search(r"RequestMethod\.(GET|POST|PUT|DELETE|PATCH)", args)
                    mapping_method = method_match.group(1) if method_match else expected_method
            if mapping_path is None or mapping_method != expected_method:
                continue
            actual_path = "/" + "/".join(
                part for part in f"{base_path}/{mapping_path}".split("/") if part
            )
            if actual_path != expected_path:
                continue
            method_declaration = re.search(
                r"\bpublic\s+(?P<return>[\w.$]+(?:\s*<[^;{}]+>)?(?:\[\])?)\s+"
                r"(?P<name>[A-Za-z_$][\w$]*)\s*\(",
                body,
            )
            if method_declaration is None:
                continue
            return_type = re.sub(r"\s+", "", method_declaration.group("return"))
            if return_type in {"void", "ResponseEntity", "HttpEntity"}:
                continue
            return_matches = list(re.finditer(r"\breturn\s+[^;]+;", body))
            if not return_matches:
                continue
            return_statement = return_matches[-1].group(0).strip()
            controller_ref = str(controller.get("evidence_id", ""))
            header_ref = str(header.get("evidence_id", ""))
            if not controller_ref or not header_ref or "@RestController" not in header_text:
                continue
            quotes = [
                {"target": "expected_status", "evidence_ref": header_ref,
                 "quote": response_body_quote},
                {"target": "expected_status", "evidence_ref": controller_ref,
                 "quote": return_statement},
            ]
            return header, controller, quotes
    return None


def _repair_default_http_status_citation(
    raw: Any, error: str, item: dict[str, Any]
) -> dict[str, Any] | None:
    """Repair only a missing/incorrect status citation when source proves default 200."""
    if (
        not isinstance(raw, dict)
        or str(raw.get("expected_status", "")).strip() != "status=200"
        or "expected_status" not in error
    ):
        return None
    proof = _default_http_status_evidence(item)
    if proof is None:
        return None
    header, controller, status_quotes = proof
    refs = raw.get("evidence_refs")
    quotes = raw.get("evidence_quotes")
    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
        return None
    if not isinstance(quotes, list) or any(not isinstance(quote, dict) for quote in quotes):
        return None
    repaired: dict[str, Any] = json.loads(json.dumps(raw, ensure_ascii=False))
    repaired["evidence_refs"] = list(dict.fromkeys(
        [*refs, str(header["evidence_id"]), str(controller["evidence_id"])]
    ))
    repaired["evidence_quotes"] = [
        quote for quote in quotes if quote.get("target") != "expected_status"
    ] + status_quotes
    return repaired


def _canonicalize_default_status_citations(response: str, item: dict[str, Any]) -> str:
    """Replace model status citations with the exact source proof for default HTTP 200."""
    proof = _default_http_status_evidence(item)
    if proof is None:
        return response
    header, controller, status_quotes = proof
    try:
        data = json.loads(strip_code_fence(response))
    except (json.JSONDecodeError, TypeError, ValueError):
        return response
    if not isinstance(data, dict) or not isinstance(data.get("oracles"), list):
        return response
    for oracle in data["oracles"]:
        if not isinstance(oracle, dict) or str(oracle.get("expected_status", "")).strip() != "status=200":
            continue
        refs = oracle.get("evidence_refs")
        quotes = oracle.get("evidence_quotes")
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            continue
        if not isinstance(quotes, list) or any(not isinstance(quote, dict) for quote in quotes):
            continue
        oracle["evidence_refs"] = list(dict.fromkeys(
            [*refs, str(header["evidence_id"]), str(controller["evidence_id"])]
        ))
        oracle["evidence_quotes"] = [
            quote for quote in quotes if quote.get("target") != "expected_status"
        ] + status_quotes
    return json.dumps(data, ensure_ascii=False)


def _remove_empty_path_assertions(response: str) -> str:
    """Drop an unusable blank-path assertion while preserving quoted indices."""
    try:
        data = json.loads(strip_code_fence(response))
    except (json.JSONDecodeError, TypeError, ValueError):
        return response
    if not isinstance(data, dict) or not isinstance(data.get("oracles"), list):
        return response
    for oracle in data["oracles"]:
        if not isinstance(oracle, dict) or not isinstance(oracle.get("assertions"), list):
            continue
        index_map: dict[int, int] = {}
        retained: list[Any] = []
        for index, assertion in enumerate(oracle["assertions"]):
            if isinstance(assertion, dict) and not str(assertion.get("path") or "").strip():
                continue
            index_map[index] = len(retained)
            retained.append(assertion)
        if len(retained) == len(oracle["assertions"]):
            continue
        oracle["assertions"] = retained
        quotes = oracle.get("evidence_quotes")
        if not isinstance(quotes, list):
            continue
        retained_quotes: list[Any] = []
        for quote in quotes:
            if not isinstance(quote, dict):
                retained_quotes.append(quote)
                continue
            target = str(quote.get("target", ""))
            match = re.fullmatch(r"assertions\[(\d+)\]", target)
            if match is None:
                retained_quotes.append(quote)
                continue
            old_index = int(match.group(1))
            if old_index in index_map:
                retained_quotes.append({**quote, "target": f"assertions[{index_map[old_index]}]"})
        oracle["evidence_quotes"] = retained_quotes
    return json.dumps(data, ensure_ascii=False)


def _derive_controller_success_oracle(
    item: dict[str, Any], fixture_entry: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Derive a fixed success-message oracle from a mapped controller branch."""
    interface = item.get("interface") or {}
    if interface.get("protocol") != "rest":
        return None
    signature = interface.get("signature") or {}
    action = str(signature.get("path", "")).rstrip("/").rsplit("/", 1)[-1]
    expected_messages = {
        "add": "新增成功", "update": "更新成功", "delete": "删除成功",
    }
    if action in expected_messages and fixture_entry is None:
        return None
    proof = _default_http_status_evidence(item)
    if proof is None:
        return None
    _, controller, status_quotes = proof
    body = str(controller.get("excerpt", ""))
    message_matches = list(re.finditer(
        r"(?P<statement>(?P<response>[A-Za-z_$][\w$]*)\.setReturnMsg\(\s*\"(?P<message>[^\"]+)\"\s*\)\s*;)",
        body,
    ))
    for match in message_matches:
        message = match.group("message")
        if action in expected_messages and message != expected_messages[action]:
            continue
        if action == "query" and not message.endswith("查询成功"):
            continue
        if action not in {*expected_messages, "query"}:
            continue
        message_index = match.start()
        try_matches = list(re.finditer(r"\btry\s*\{", body[:message_index]))
        try_index = try_matches[-1].start() if try_matches else -1
        catch_index = body.find("catch", try_index if try_index >= 0 else 0)
        if try_index < 0 or (catch_index >= 0 and catch_index < message_index):
            continue
        if not re.search(
            r"\b[A-Za-z_$][\w$]*[Ss]ervice\s*\.\s*[A-Za-z_$][\w$]*\s*\(",
            body[try_index:message_index],
        ):
            continue
        return_statement = f"return {match.group('response')};"
        return_statements = list(re.finditer(r"\breturn\s+[^;]+;", body[match.end():]))
        if not return_statements or return_statements[-1].group(0).strip() != return_statement:
            continue
        controller_ref = str(controller.get("evidence_id", ""))
        evidence_refs = list(dict.fromkeys([
            str(quote["evidence_ref"]) for quote in status_quotes
        ] + [controller_ref]))
        oracle = {
            "interface_id": str(interface.get("id", "")),
            "category": "normal",
            "scenario": f"Service调用正常返回时Controller返回{message}",
            "preconditions": [],
            "expected_status": "status=200",
            "assertions": [
                {"path": "returnMsg", "op": "equals", "value": message}
            ],
            "evidence_refs": evidence_refs,
            "evidence_quotes": status_quotes + [
                {"target": "assertions[0]", "evidence_ref": controller_ref,
                 "quote": match.group("statement")},
            ],
        }
        if fixture_entry is not None:
            oracle["fixture_plan"] = fixture_entry["fixture_plan"]
        return oracle
    return None


def _fixture_plan_supports_success_oracle(
    oracle: dict[str, Any], interface: dict[str, Any], fixture_entry: dict[str, Any]
) -> bool:
    """A source-derived fixture controls DB state only for its matching CRUD success."""
    if oracle.get("category") != "normal":
        return False
    expected_message = str(fixture_entry.get("success_message", ""))
    action = str((interface.get("signature") or {}).get("path", "")).rsplit("/", 1)[-1]
    action_message = {"add": "新增成功", "update": "更新成功", "delete": "删除成功"}.get(action)
    if not expected_message or expected_message != action_message:
        return False
    return any(
        item.get("path") == "returnMsg"
        and item.get("op") == "equals"
        and str(item.get("value")) == expected_message
        for item in oracle.get("assertions", [])
        if isinstance(item, dict)
    )


def _canonicalize_request_condition_paths(
    conditions: list[dict[str, Any]], interface: dict[str, Any], source_text: str
) -> list[dict[str, Any]]:
    """只将有请求字段证据且归属唯一的裸路径补成参数根路径。"""
    params = [
        item for item in interface.get("params", [])
        if isinstance(item, dict) and item.get("name")
    ]
    parameter_names = {str(item["name"]) for item in params}
    constants = dict(re.findall(
        r'\bString\s+([A-Za-z_$][\w$]*)\s*=\s*"([^"\n]+)"', source_text
    ))
    body_fields: dict[str, set[str]] = {}
    for param in params:
        if param.get("in") != "body":
            continue
        name = str(param["name"])
        fields = {
            str(field["name"]) for field in param.get("fields", [])
            if isinstance(field, dict) and field.get("name")
        }
        # 只读取请求参数本身的 JSON accessor，不能把任意内部对象的 getter
        # 当成 HTTP 输入；字段名常量必须同时出现在这次接口的证据中。
        for literal, constant in re.findall(
            r"\b" + re.escape(name)
            + r'\s*\.\s*get[A-Za-z0-9_]*\s*\(\s*(?:"([^"\n]+)"|([A-Za-z_$][\w$]*))\s*[,)]',
            source_text,
        ):
            key = literal or constants.get(constant, "")
            if key:
                fields.add(key)
        body_fields[name] = fields

    normalized: list[dict[str, Any]] = []
    for condition in conditions:
        path = str(condition.get("path", ""))
        match = re.match(r"[A-Za-z_][A-Za-z0-9_-]*", path)
        root = match.group(0) if match else ""
        owners = [name for name, fields in body_fields.items() if root in fields]
        if root not in parameter_names and len(owners) == 1:
            path = f"{owners[0]}.{path}"
        normalized.append({**condition, "path": path})
    return normalized


def _oracle_source_branches(
    interface: dict[str, Any], evidence_texts: dict[str, str]
) -> list[dict[str, str]]:
    """从完整源码提取平衡的 if 条件，并沿局部赋值追踪控制来源。"""
    request_names = {
        str(param["name"]) for param in interface.get("params", [])
        if isinstance(param, dict) and param.get("name")
    }
    def direct_origins(expression: str) -> set[str]:
        result: set[str] = set()
        code = _java_source_mask(expression) or expression
        if re.search(r"getComplexExecutors|complexExecutors|configuredExecutors|executorProperties", code):
            result.add("application_config")
        if re.search(r"executor_?service_?cache|executorServiceCache|serviceRegistry|detailProcessorCache", code, re.I):
            result.add("service_registry")
        if re.search(r"\b\w*(?:Service|Client)\w*\s*\.\s*\w+\s*\(|\.\s*(?:exchange|execute)\s*\(", code):
            result.add("downstream_response")
        if request_names.intersection(re.findall(r"\b[A-Za-z_$][\w$]*\b", code)):
            result.add("request")
        return result

    branches: list[dict[str, str]] = []
    for ref, text in evidence_texts.items():
        masked = _java_source_mask(text)
        if masked is None:
            continue
        # Local names in different methods must not share data provenance.
        assignments: dict[str, list[str]] = {}
        for match in re.finditer(r"\b([A-Za-z_$][\w$]*)\s*=(?!=)([^;]+);", masked):
            assignments.setdefault(match.group(1), []).append(
                text[match.start(2):match.end(2)]
            )
        for match in re.finditer(
            r"\bfor\s*\(\s*[\w.<>?]+\s+(\w+)\s*:\s*(\w+)\s*\)", masked
        ):
            assignments.setdefault(match.group(1), []).append(match.group(2))
        # 聚合结果的 put 写入也属于定义，不能仅看到 Map 初始化就丢失远端来源。
        for match in re.finditer(r"\b(\w+)\s*\.\s*(?:put|putAll|add|addAll)\s*\(([^;]+);", masked):
            assignments.setdefault(match.group(1), []).append(
                text[match.start(2):match.end(2)]
            )

        provenance: dict[str, set[str]] = {name: set() for name in assignments}
        for _ in range(len(provenance) + 1):
            changed = False
            for name, values in assignments.items():
                tags = set(provenance[name])
                for value in values:
                    tags.update(direct_origins(value))
                    for token in re.findall(r"\b[A-Za-z_$][\w$]*\b", _java_source_mask(value) or value):
                        tags.update(provenance.get(token, set()))
                if tags != provenance[name]:
                    provenance[name] = tags
                    changed = True
            if not changed:
                break

        def origins(expression: str) -> set[str]:
            tags = direct_origins(expression)
            for token in re.findall(r"\b[A-Za-z_$][\w$]*\b", _java_source_mask(expression) or expression):
                tags.update(provenance.get(token, set()))
            return tags

        for index, match in enumerate(re.finditer(r"\bif\s*\(", masked), 1):
            opening = masked.find("(", match.start(), match.end())
            closing = _java_matching_delimiter(masked, opening, "(", ")")
            if closing is None:
                continue
            expression = text[opening + 1:closing]
            dependencies = origins(expression)
            control = next((kind for kind in (
                "downstream_response", "service_registry", "application_config", "request"
            ) if kind in dependencies), "unresolved")
            branches.append({
                "branch_id": f"{ref}:if:{index}", "evidence_ref": ref,
                "quote": text[match.start():closing + 1], "control_kind": control,
                "required_controls": ",".join(sorted(dependencies.intersection({
                    "application_config", "service_registry", "downstream_response",
                }))),
            })
    branches.extend(controller_exits(evidence_texts))
    return branches


def _oracle_branch_control_reason(
    oracle: dict[str, Any], evidence: list[dict[str, Any]],
    interface: dict[str, Any], controls: set[str],
) -> str | None:
    branch_id = oracle.get("branch_id")
    if not branch_id:
        return None
    branch = next((item for item in _oracle_source_branches(
        interface, {str(item.get("evidence_id", "")): str(item.get("excerpt", ""))
                    for item in evidence if item.get("evidence_id")},
    ) if item["branch_id"] == branch_id), None)
    if branch is None:
        return "源码分支定位已失效，需重新提取本接口的 branch_id"
    kind = branch["control_kind"]
    required_controls = set(filter(None, branch.get("required_controls", "").split(",")))
    integration_kinds = {
        str(condition["kind"]) for condition in oracle.get("state_conditions", [])
        if condition.get("mode") == "integration"
        and condition.get("evidence_ref") == branch["evidence_ref"]
        and condition.get("quote") == branch["quote"]
    }
    missing_controls = required_controls - controls - integration_kinds
    if missing_controls:
        missing = ", ".join(sorted(missing_controls))
        return (
            f"源码分支 {branch_id} 的条件 {branch['quote']} 依赖 {missing}；"
            f"需提供匹配该场景的 {missing} setup/teardown fixture 并验证接线"
        )
    if kind == "unresolved":
        return "目标源码分支的条件来源尚未能绑定到 HTTP 请求或控制夹具；需补齐调用链数据来源证据"
    if kind == "request":
        source = "\n".join(str(item.get("excerpt", "")) for item in evidence)
        conditions = _canonicalize_request_condition_paths(
            oracle.get("preconditions") or [], interface, source
        )
        expression = branch["quote"]
        comparison = re.fullmatch(
            r'if\s*\(\s*(\w+)\s*(==|!=)\s*(null|-?\d+|"[^"\n]*"|[A-Z_][A-Z0-9_]*)\s*\)',
            expression,
        )
        if comparison is None:
            reversed_comparison = re.fullmatch(
                r'if\s*\(\s*(null|-?\d+|"[^"\n]*"|[A-Z_][A-Z0-9_]*)\s*(==|!=)\s*(\w+)\s*\)',
                expression,
            )
            if reversed_comparison:
                variable, operator, literal = (
                    reversed_comparison.group(3), reversed_comparison.group(2),
                    reversed_comparison.group(1),
                )
            else:
                variable = operator = literal = ""
        else:
            variable, operator, literal = comparison.groups()
        if oracle.get("branch_outcome", True) is False:
            operator = {"==": "!=", "!=": "=="}.get(operator, operator)
        if variable:
            normalized = _canonicalize_request_condition_paths(
                [{"path": variable, "op": "exists"}], interface, source
            )[0]["path"]
            parameter_names = {str(param.get("name")) for param in interface.get("params", [])}
            if normalized != variable or variable in parameter_names:
                constant = re.search(
                    r"\b" + re.escape(literal) + r'\s*=\s*(null|-?\d+|"[^"\n]*")\s*;',
                    source,
                ) if literal not in {"null"} and not literal.startswith('"') else None
                value_text = constant.group(1) if constant else literal
                try:
                    value = json.loads(value_text)
                except (ValueError, TypeError):
                    return "源码比较常量未能解析，不能证明请求命中了目标分支"
                proven = any(
                    condition.get("path") == normalized
                    and (
                        (operator == "==" and condition.get("op") == "equals"
                         and condition.get("value") == value)
                        or (operator == "!=" and condition.get("op") == "not_equals"
                            and condition.get("value") == value)
                        or (operator == "!=" and condition.get("op") == "equals"
                            and condition.get("value") != value)
                    ) for condition in conditions
                )
                if not proven:
                    return "请求前置条件不能证明选中的源码比较条件；参数存在或取其它值不代表命中该分支"
    return None


def _oracle_environment_gap(reason: str) -> bool:
    """Environment readiness must not erase a source-backed test design."""
    return any(token in reason for token in (
        "setup/teardown fixture", "确定性 fixture", "数据库查询结果或删除状态",
        "数据库记录存在性", "成功分支依赖数据库记录存在",
        "条件来源尚未能绑定到 HTTP 请求或控制夹具",
    ))


def _repairable_oracle_quality_issue(reason: str) -> bool:
    """缺失业务断言需要修复候选，不能误判为 HTTP 不可达。"""
    return reason.startswith("正常联调请求前置条件未排除") or reason in {
        "仅检查响应字段存在，无法证明命中了目标业务分支",
        "没有可核验的业务响应断言，不能用 HTTP 状态代替业务预期",
    }


def _requires_registry_override(scenario: str) -> bool:
    """普通注册项存在前提不等于主动删除/替换注册项的测试。"""
    normalized = scenario.lower()
    return bool(
        re.search(r"executor_service_cache|服务缓存|服务注册表|注册项", normalized)
        and re.search(r"不存在|缺失|未注册|未找到|没有找到|清空|删除|替换|missing|absent", normalized)
    )


def _oracle_candidate_preflight_reason(
    candidate: Any,
    evidence: list[dict[str, Any]],
    interface: dict[str, Any],
    *,
    control_fixture_kinds: set[str] | None = None,
) -> str | None:
    """Classify request/control gaps before spending another LLM repair call.

    The LLM may correctly notice a guard in a called method while proposing an
    input that cannot reach it through this controller. Check request paths and
    obvious caller/configuration constraints before treating a failed response
    citation as a repairable oracle.
    """
    if not isinstance(candidate, dict):
        return None
    control_fixture_kinds = control_fixture_kinds or set()
    try:
        preconditions = normalize_response_assertions(candidate.get("preconditions", []), label="preconditions")
        assertions = normalize_response_assertions(candidate.get("assertions", []))
    except ValueError:
        return None

    request_params = [
        item for item in (interface.get("params") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    request_param_names = {str(item["name"]) for item in request_params}
    body_param_names = {
        str(item["name"]) for item in request_params if item.get("in") == "body"
    }
    source_text = "\n".join(
        str(item.get("excerpt", ""))
        for item in evidence
        if item.get("role") in {
            "controller", "controller_header", "service_impl", "called_method",
            "called_method_constant", "service", "dto_validation",
        }
    )
    preconditions = _canonicalize_request_condition_paths(
        preconditions, interface, source_text
    )
    normalized = {**candidate, "preconditions": preconditions, "assertions": assertions}

    # An API precondition must name fields actually represented by this request
    # body. JSONObject keys are discovered from its accessors and their constants;
    # this covers dynamic JSON endpoints that have no DTO class to inspect.
    request_keys = {
        str(field.get("name"))
        for param in request_params
        for field in (param.get("fields") or [])
        if isinstance(field, dict) and field.get("name")
    }
    request_keys.update(re.findall(
        r"\.\s*get(?:String|Integer|Long|Boolean|JSONArray|JSONObject|Double|Float)\s*\(\s*"
        r"[\"']([^\"']+)[\"']\s*\)",
        source_text,
    ))
    dto_source_text = "\n".join(
        str(item.get("excerpt", ""))
        for item in evidence if item.get("role") == "dto_validation"
    )
    request_keys.update(re.findall(
        r"^\s*(?:private|protected|public)\s+(?:static\s+)?(?:final\s+)?"
        r"[\w.$<>?, \[\]]+\s+([A-Za-z_$][\w$]*)\s*(?:[;=])",
        dto_source_text,
        re.MULTILINE,
    ))
    constants = {
        name: value for name, value in re.findall(
            r"\b(?:String|final\s+String)\s+([A-Z_$][\w$]*)\s*=\s*[\"']([^\"']+)[\"']",
            source_text,
        )
    }
    for name, value in constants.items():
        if re.search(
            r"\.\s*get[A-Za-z_$][\w$]*\s*\(\s*" + re.escape(name) + r"\s*\)",
            source_text,
        ):
            request_keys.add(value)

    for condition in preconditions:
        path = str(condition.get("path", ""))
        root = path.split(".", 1)[0].split("[", 1)[0]
        if request_param_names and root not in request_param_names:
            return (
                f"前置条件 {path} 不是接口请求参数，依赖服务返回值或内部状态；"
                "需要下游 fixture 或环境准备"
            )
        if root not in body_param_names:
            continue
        path_fields = re.findall(r"(?:^|\.)([A-Za-z_][A-Za-z0-9_-]*)", path)[1:]
        missing_fields = [field for field in path_fields if field not in request_keys]
        if missing_fields:
            return (
                f"前置条件 {path} 中的字段 {missing_fields[0]} 没有在接口调用链中作为请求 JSON 字段读取；"
                "它可能是内部方法参数，不能由 HTTP 请求直接设置"
            )

    scenario = str(candidate.get("scenario", ""))
    branch_reason = _oracle_branch_control_reason(
        normalized, evidence, interface, control_fixture_kinds
    )
    if branch_reason and "定位已失效" not in branch_reason:
        return branch_reason
    scenario_lower = scenario.lower()
    quote_text = "\n".join(
        str(item.get("quote", ""))
        for item in candidate.get("evidence_quotes", [])
        if isinstance(item, dict)
    )
    source_and_quotes = source_text + "\n" + quote_text

    if (
        candidate.get("category") != "normal"
        and "o32" in scenario_lower
        and re.search(
            r"if\s*\(\s*[A-Za-z_$][\w$]*\s*==\s*null\s*\).*?return\s+response\s*;",
            source_and_quotes,
            re.DOTALL,
        )
    ):
        return "源码显示 o32Result 为 null 时直接 return response，属于正常返回分支，不是异常"

    if (
        not candidate.get("branch_id")
        and ("未配置" in scenario or "列表为空" in scenario or "列表为空" in scenario_lower)
        and re.search(r"getComplexExecutors|configuredExecutors|complex-executors", source_and_quotes)
        and "application_config" not in control_fixture_kinds
    ):
        return "该分支由复合执行器环境配置决定；需提供匹配该场景的 application_config setup/teardown fixture"

    if (
        not candidate.get("branch_id") and "结果为空" in scenario
        and re.search(r"\b[A-Za-z_$][\w$]*(?:Response|Result|TrialObj)\s*==\s*null\b", source_and_quotes)
        and "downstream_response" not in control_fixture_kinds
    ):
        return "该分支由下游 Service/O32 返回对象决定；需提供匹配该场景的 downstream_response setup/teardown fixture"

    if (
        not candidate.get("branch_id") and _requires_registry_override(scenario)
        and "service_registry" not in control_fixture_kinds
    ):
        return "该分支取决于 Spring 服务注册表/Executor 缓存；需在隔离测试上下文中提供 service_registry setup/teardown fixture"

    # The approval API skips empty symbols before reaching the repository. The
    # repository's own blank-symbol validation is therefore not an API branch.
    empty_symbol = any(
        re.search(r"(?:\.Symbol|\.symbol)$", str(condition.get("path", "")))
        and condition.get("op") == "equals"
        and (condition.get("value") is None or condition.get("value") == "")
        for condition in preconditions
    )
    caller_skips_blank_symbol = re.search(
        r"if\s*\(\s*StringUtils\.isBlank\s*\(\s*symbol\s*\).*?continue\s*;"
        r".*?existsInBondLibrary\s*\(\s*symbol\s*,",
        source_text,
        re.DOTALL,
    )
    if "cone" in scenario_lower and empty_symbol and caller_skips_blank_symbol:
        return "上游解析器在 Symbol 为空时会 continue，cone Repository 的空代码校验对该 API 不可达"

    if (
        "mktno" in scenario_lower or "市场编号" in scenario
    ) and re.search(r"Map\.of\s*\(\s*\"SS\"\s*,\s*1\s*,\s*\"SZ\"\s*,\s*2\s*,\s*\"OTC\"\s*,\s*5\s*\)", source_text):
        return "mktNo 由上游市场映射生成且只可能为 1、2、5；小于等于 0 的 Repository 分支对该 API 不可达"

    state_reason = _oracle_uncontrolled_state_reason(
        normalized, evidence, interface,
        control_fixture_kinds=control_fixture_kinds,
    )
    if state_reason and not _repairable_oracle_quality_issue(state_reason) and not _oracle_environment_gap(state_reason):
        return state_reason

    # Raw exception candidates often have a valid request-triggered throw but
    # lack the shared advice/runtime response proof. Explain that gap directly
    # instead of asking the model to guess the same response fields again.
    if (
        candidate.get("category") == "exception"
        and not (candidate.get("expected_status") == "unresolved" and candidate.get("expected_behavior"))
        and re.search(r"\bthrow\s+new\s+(?:RiskServiceException|RuntimeException)", source_and_quotes)
        and not any(item.get("role") == "exception_advice" for item in evidence)
        and not re.search(r"\bcatch\s*\(", source_text)
    ):
        return "源码能证明该请求会抛出异常，但没有异常处理器或该输入的运行观测来证明 HTTP 响应"

    return None


def _oracle_uncontrolled_state_reason(
    oracle: dict[str, Any], evidence: list[dict[str, Any]],
    interface: dict[str, Any] | None = None,
    *,
    fixture_supports_success: bool = False,
    control_fixture_kinds: set[str] | None = None,
) -> str | None:
    """分支必须可由请求和已给出的前置数据确定，不能只凭 catch 或记录 ID。"""
    if oracle.get("oracle_basis") == "spring_required_body" and interface is not None:
        design = framework_body_design(interface, {str(e.get("evidence_id")): str(e.get("excerpt", "")) for e in evidence})
        if design and design["preconditions"] == oracle.get("preconditions"):
            return None
    assertions = oracle.get("assertions") or []
    conditions = _canonicalize_request_condition_paths(
        oracle.get("preconditions") or [],
        interface or {},
        "\n".join(str(item.get("excerpt", "")) for item in evidence),
    )
    control_fixture_kinds = control_fixture_kinds or set()
    request_param_names = {
        str(item.get("name", ""))
        for item in (interface or {}).get("params", [])
        if isinstance(item, dict) and item.get("name")
    }
    for condition in conditions:
        path = str(condition.get("path", ""))
        root = path.split(".", 1)[0].split("[", 1)[0]
        if request_param_names and root not in request_param_names:
            return (
                f"前置条件 {path} 不是接口请求参数，依赖服务返回值或内部状态；"
                "请求无法设置该值"
            )

    branch_reason = _oracle_branch_control_reason(
        oracle, evidence, interface or {}, control_fixture_kinds
    )
    if branch_reason:
        return branch_reason

    # A branch selected by a downstream response cannot be turned into an
    # API test from source assertions alone. The request does not control a
    # Service/O32 result; such a branch needs a deterministic fixture or a
    # mock plan. Keep it out of the executable oracle list until one exists.
    scenario = str(oracle.get("scenario", ""))
    scenario_lower = scenario.lower()
    quote_text = "\n".join(
        str(item.get("quote", ""))
        for item in oracle.get("evidence_quotes") or []
        if isinstance(item, dict)
    )
    if (
        oracle.get("category") != "normal"
        and "o32" in scenario_lower
        and re.search(
            r"if\s*\(\s*[A-Za-z_$][\w$]*\s*==\s*null\s*\).*?return\s+response\s*;",
            quote_text,
            re.DOTALL,
        )
    ):
        return "源码显示 O32 结果为 null 时直接返回 response，这是正常返回分支，不能作为异常预期"
    if (
        not oracle.get("branch_id")
        and ("未配置" in scenario or "列表为空" in scenario or "列表为空" in scenario_lower)
        and "application_config" not in control_fixture_kinds
    ):
        return "该分支由复合执行器环境配置决定；需提供匹配该场景的 application_config setup/teardown fixture"
    if (
        not oracle.get("branch_id") and _requires_registry_override(scenario)
        and "service_registry" not in control_fixture_kinds
    ):
        return "该分支取决于 Spring 服务注册表/Executor 缓存；需在隔离测试上下文中提供 service_registry setup/teardown fixture"

    if not oracle.get("branch_id") and not (
        oracle.get("category") == "normal" and fixture_supports_success
    ):
        quote_text = "\n".join(
            str(item.get("quote", ""))
            for item in oracle.get("evidence_quotes") or []
            if isinstance(item, dict)
        )
        service_result_branch = re.search(
            r"\b[A-Za-z_$][\w$]*(?:Response|Result|TrialObj|OutList)\s*"
            r"(?:==|!=)\s*null\b|"
            r"(?:!\s*\"0\"\.equals\s*\([^\n)]*getErrorCode\s*\(\)|"
            r"getErrorCode\s*\(\)\s*(?:!=|==))",
            quote_text,
        )
        if service_result_branch and "downstream_response" not in control_fixture_kinds:
            return (
                "目标分支由 Service/外部执行器的返回对象决定；当前请求前置条件不能控制该结果，"
                "且没有确定性 fixture 或 mock 计划"
            )
    controller_bodies = [
        str(item.get("excerpt", "")) for item in evidence
        if item.get("role") == "controller"
    ]
    # @RequestBody 默认 required=true。整个 body 为 null 时，Spring 在调用控制器
    # 和 Service 前就会拒绝请求；不能把 Service 内的 null 分支当 HTTP 5xx 预期。
    for condition in conditions:
        path = str(condition.get("path", ""))
        if "." in path or condition.get("op") != "equals" or condition.get("value") is not None:
            continue
        if any(re.search(
            r"@RequestBody(?!\s*\(\s*required\s*=\s*false\b)"
            r"(?:\s*\([^)]*\))?[^,)]*?\b" + re.escape(path) + r"\b",
            body,
        ) for body in controller_bodies):
            return "@RequestBody 请求体缺失会在控制器调用前被框架拦截；可单独作为 HTTP 框架边界请求实测，但不能引用 Service 的 null 分支"
    dto_issue = None
    if interface is not None:
        evidence_by_id = {
            str(item.get("evidence_id", "")): item
            for item in evidence if item.get("evidence_id")
        }
        dto_issue = dto_validation_oracle_issue(oracle, interface, evidence_by_id)
    if dto_issue:
        return dto_issue
    if interface is not None:
        unreachable_issue = dto_precondition_reachability_issue(
            oracle, interface, evidence
        )
        if unreachable_issue:
            return unreachable_issue
    if _oracle_has_invalid_dto_condition(oracle, evidence, interface):
        return "DTO 参数校验预期由源码规则生成"
    if not oracle.get("state_conditions") and oracle.get("category") == "exception" and conditions and all(
        condition.get("op") in {"exists", "non_empty"} for condition in conditions
    ):
        return "仅有参数存在性条件，不能保证 Service 返回空值或抛出异常"
    if not oracle.get("state_conditions") and any(word in str(oracle.get("scenario", "")) for word in ("异常", "抛出")) and not any(
        condition.get("op") in {"equals", "not_equals", "contains", "greater_than", "less_than", "in", "not_in"}
        for condition in conditions
    ):
        return "异常触发依赖服务或外部状态，缺少请求可控的必经条件"
    if oracle.get("category") == "normal" and any(
        condition.get("mode") == "integration" for condition in oracle.get("state_conditions", [])
    ):
        source = "\n".join(str(item.get("excerpt", "")) for item in evidence)
        constants = dict(re.findall(r'\bString\s+(\w+)\s*=\s*"([^"\n]+)"', source))
        for name in request_param_names:
            pattern = (r'StringUtils\.isBlank\s*\(\s*' + re.escape(name)
                       + r'\.getString\s*\(\s*("[^"\n]+"|[A-Z_][A-Z0-9_]*)\s*\)\s*\)'
                       + r'[^;{}]*\)\s*\{?\s*throw\b')
            for match in re.finditer(pattern, source):
                key = match.group(1).strip('"') if match.group(1).startswith('"') else constants.get(match.group(1))
                if not key:
                    continue
                path = f"{name}.{key}"
                if not any(condition.get("path") == path and (
                    condition.get("op") == "not_blank" or
                    (condition.get("op") == "equals" and isinstance(condition.get("value"), str)
                     and bool(condition["value"].strip()))
                ) for condition in conditions):
                    return f"正常联调请求前置条件未排除入口必填字段校验失败：{path}；需提供非空有效值并引用校验源码"
    if (oracle.get("category") == "normal" and not assertions
            and not (oracle.get("oracle_basis") == "integration_entry"
                     and oracle.get("expected_status") not in {None, "unresolved"})):
        return "没有可核验的业务响应断言，不能用 HTTP 状态代替业务预期"
    if oracle.get("category") == "exception" and not oracle.get("preconditions") and not oracle.get("state_conditions"):
        return "只有通用 catch 分支，没有请求可控的异常触发条件"
    evidence_by_id = {str(item.get("evidence_id", "")): item for item in evidence}
    precondition_quotes = [
        quote for quote in oracle.get("evidence_quotes") or []
        if str(quote.get("target", "")).startswith("preconditions[")
    ]
    if (
        oracle.get("category") == "exception"
        and oracle.get("expected_status") == "status=200"
        and any("throw " in str(quote.get("quote", ""))
                for quote in oracle.get("evidence_quotes") or [])
        and not any(item.get("role") == "exception_advice" for item in evidence)
        and not any(re.search(r"\bcatch\s*\(", body) for body in controller_bodies)
    ):
        return "源码只证明会抛出异常，未找到将该异常映射为 HTTP 200 的处理分支"
    if (
        oracle.get("category") == "exception"
        and oracle.get("expected_status") == "status=200"
        and not assertions
        and not any(item.get("role") == "exception_advice" for item in evidence)
    ):
        return "异常场景只有默认 HTTP 200，缺少能证明异常响应的处理器或业务断言"
    if any(re.search(r"\b(?:oldSetting|setting|getDeleted)\b", str(q.get("quote", "")))
           and evidence_by_id.get(str(q.get("evidence_ref", "")), {}).get("role") == "service_impl"
           for q in precondition_quotes):
        if not fixture_supports_success:
            return "前置条件依赖数据库查询结果或删除状态，请求参数不能证明该分支会执行"
    service_bodies = [
        str(item.get("excerpt", ""))
        for item in evidence
        if item.get("role") == "service_impl"
    ]
    if service_bodies and any(
        word in str(oracle.get("scenario", ""))
        for word in ("已删除", "不存在", "存在且", "记录缺失")
    ) and any(re.search(r"\b(?:selectOneById|findById|getById)\s*\(", body)
              for body in service_bodies):
        if not fixture_supports_success:
            return "场景依赖数据库记录存在性或删除状态，缺少可核验的测试记录前置条件"
    if oracle.get("category") != "normal" or not assertions:
        return None
    for body in service_bodies:
        looks_up_record = re.search(r"\b(?:selectOneById|findById|getById)\s*\(", body)
        rejects_missing = re.search(r"\bif\s*\([^)]*==\s*null\s*\)\s*\{?\s*throw\b", body)
        rejects_deleted = "getDeleted()" in body and "throw new" in body
        if looks_up_record and (rejects_missing or rejects_deleted) and not fixture_supports_success:
            return (
                "成功分支依赖数据库记录存在且状态有效；源码展示了记录缺失/已删除分支，"
                "当前仅凭请求参数无法保证成功。需先提供可复用的测试记录及其状态。"
            )
    return None


def _literal_supported_by_quote(value: Any, quote: str) -> bool:
    """表达式或常量名不能冒充 Java 响应字面值。"""
    if isinstance(value, str):
        return f'"{value}"' in quote or f"'{value}'" in quote
    if value is None:
        return bool(re.search(r"\bnull\b", quote))
    if isinstance(value, bool):
        return bool(re.search(r"\b" + str(value).lower() + r"\b", quote))
    if isinstance(value, (int, float)):
        return bool(re.search(r"(?<![\w.])" + re.escape(str(value)) + r"(?![\w.])", quote))
    if value == []:
        return "Collections.emptyList()" in quote or "[]" in quote
    if value == {}:
        return "Collections.emptyMap()" in quote or "{}" in quote
    return False


def _explicit_http_status_supported(expected_status: str, quotes: list[str]) -> bool:
    """Require an actual HTTP status expression, not a Java throw statement."""
    status_names = {
        "OK": 200, "CREATED": 201, "NO_CONTENT": 204,
        "BAD_REQUEST": 400, "UNAUTHORIZED": 401, "FORBIDDEN": 403,
        "NOT_FOUND": 404, "CONFLICT": 409, "UNPROCESSABLE_ENTITY": 422,
        "INTERNAL_SERVER_ERROR": 500, "BAD_GATEWAY": 502,
        "SERVICE_UNAVAILABLE": 503, "GATEWAY_TIMEOUT": 504,
    }
    cited: set[int] = set()
    for quote in quotes:
        for name in re.findall(r"\bHttpStatus\.([A-Z_]+)\b", quote):
            if name in status_names:
                cited.add(status_names[name])
        for match in re.finditer(
            r"\b(?:status_code|status|setStatus|sendError|valueOf)\s*"
            r"(?:=|\()\s*(?:HTTP_)?([1-5]\d{2})(?:_[A-Z_]+)?\b",
            quote,
        ):
            cited.add(int(match.group(1)))
    if expected_status.startswith("status="):
        return int(expected_status.split("=", 1)[1]) in cited
    expected_class = expected_status.split("=", 1)[1][0]
    return any(str(status).startswith(expected_class) for status in cited)


def _oracle_has_invalid_dto_condition(
    oracle: dict[str, Any], evidence: list[dict[str, Any]],
    interface: dict[str, Any] | None = None,
) -> bool:
    """识别请求先被 Bean Validation 拦截的分支，禁止引用后续 Service/catch。"""
    if not any(
        item.get("role") == "controller" and "@Validated" in str(item.get("excerpt", ""))
        for item in evidence
    ):
        return False
    if interface is None:
        return False
    evidence_by_id = {
        str(item.get("evidence_id", "")): item
        for item in evidence if item.get("evidence_id")
    }
    # Only correctly bound source annotations count. In particular, an annotation
    # on the preceding field must never turn this into a DTO validation oracle.
    contracts = dto_field_contracts(interface, list(evidence_by_id.values()))
    for index, condition in enumerate(oracle.get("preconditions") or []):
        path = str(condition.get("path", ""))
        if condition.get("op") != "equals" or path.count(".") != 1:
            continue
        param_name, field_name = path.split(".", 1)
        contract = contracts.get((param_name, field_name))
        annotation = field_validation_annotation(contract)
        if not annotation or contract is None or contract.get("validation_conflict"):
            continue
        quote_matches_field = any(
            isinstance(quote, dict)
            and str(quote.get("target", "")) == f"preconditions[{index}]"
            and evidence_by_id.get(str(quote.get("evidence_ref", "")), {}).get("role") == "dto_validation"
            and validation_kind(quote.get("quote")) == contract.get("validation_kind")
            and str(quote.get("quote", "")) in annotation
            for quote in oracle.get("evidence_quotes") or []
        )
        if quote_matches_field and value_violates_validation(
            str(contract.get("type", "")), annotation, condition.get("value")
        ):
            return True
    return False


def _derive_dto_validation_oracles(item: dict[str, Any]) -> list[dict[str, Any]]:
    """从 DTO 注解和全局处理器确定性还原每个参数校验拒绝分支。"""
    interface = item["interface"]
    evidence = item["evidence"]
    if interface.get("protocol") != "rest":
        return []
    controller = next((entry for entry in evidence
                       if entry.get("role") == "controller"
                       and "@Validated" in str(entry.get("excerpt", ""))), None)
    advice = next((entry for entry in evidence
                   if entry.get("role") == "exception_advice"
                   and "@RestControllerAdvice" in str(entry.get("excerpt", ""))
                   and "@ExceptionHandler(MethodArgumentNotValidException.class)" in str(entry.get("excerpt", ""))
                   and "response.setReturnCode(-1);" in str(entry.get("excerpt", ""))
                   and "return response;" in str(entry.get("excerpt", ""))), None)
    if controller is None or advice is None:
        return []
    body_params = [str(param.get("name", "")) for param in interface.get("params") or []
                   if isinstance(param, dict) and param.get("in") == "body"]
    if len(body_params) != 1:
        return []
    result: list[dict[str, Any]] = []
    contracts = dto_field_contracts(interface, evidence)
    body_param_names = {
        str(param.get("name", ""))
        for param in interface.get("params") or []
        if isinstance(param, dict) and param.get("in") == "body"
    }
    for (body_param, field_name), contract in contracts.items():
        if body_param not in body_param_names or contract.get("validation_conflict"):
            continue
        validation = field_validation_annotation(contract)
        if validation is None:
            continue
        supported, invalid_value = invalid_value_for_validation(
            str(contract.get("type", "")), validation
        )
        if not supported:
            continue
        dto = next((entry for entry in evidence
                    if str(entry.get("evidence_id", "")) == contract.get("source_evidence_id")), None)
        if dto is None:
            continue
        assertions: list[dict[str, Any]] = [
            {"path": "returnCode", "op": "equals", "value": -1}
        ]
        quotes = [
            {"target": "expected_status", "evidence_ref": str(advice["evidence_id"]),
             "quote": "@RestControllerAdvice"},
            {"target": "expected_status", "evidence_ref": str(advice["evidence_id"]),
             "quote": "return response;"},
            {"target": "preconditions[0]", "evidence_ref": str(dto["evidence_id"]),
             "quote": validation},
            {"target": "assertions[0]", "evidence_ref": str(advice["evidence_id"]),
             "quote": "response.setReturnCode(-1);"},
        ]
        message_match = re.search(r'\bmessage\s*=\s*"([^"]+)"', validation)
        if message_match:
            assertions.append({
                "path": "returnMsg", "op": "contains", "value": message_match.group(1)
            })
            quotes.append({
                "target": "assertions[1]", "evidence_ref": str(dto["evidence_id"]),
                "quote": validation,
            })
        refs = list(dict.fromkeys([
            str(controller["evidence_id"]), str(dto["evidence_id"]),
            str(advice["evidence_id"]),
        ]))
        result.append({
            "interface_id": str(interface["id"]),
            "category": "exception",
            "scenario": f"{field_name} 不满足 DTO 的 {validation.split('(', 1)[0]} 校验",
            "preconditions": [{"path": f"{body_param}.{field_name}",
                               "op": "equals", "value": invalid_value}],
            "expected_status": "status=200",
            "assertions": assertions,
            "evidence_refs": refs,
            "evidence_quotes": quotes,
        })
    return result


def _derive_enum_rejection_oracle(item: dict[str, Any]) -> dict[str, Any] | None:
    """枚举 parse 对未知 code 返回 null 且控制器明确构造错误响应时，构造可控输入。"""
    interface = item["interface"]
    evidence = item["evidence"]
    if interface.get("protocol") != "rest":
        return None
    header = next((entry for entry in evidence
                   if entry.get("role") == "controller_header"
                   and "@RestController" in str(entry.get("excerpt", ""))), None)
    helper = next((entry for entry in evidence
                   if entry.get("role") == "other"
                   and 'result.put("resultCode", 1);' in str(entry.get("excerpt", ""))
                   and 'result.put("message", message);' in str(entry.get("excerpt", ""))), None)
    if header is None or helper is None:
        return None
    param_names = {str(param.get("name", "")) for param in interface.get("params") or []
                   if isinstance(param, dict)}
    for controller in evidence:
        if controller.get("role") != "controller":
            continue
        body = str(controller.get("excerpt", ""))
        branch = re.search(
            r"\b(?P<enum>[A-Za-z_$][\w$]*Enum)\.parse\s*\(\s*(?P<param>[A-Za-z_$][\w$]*)\s*\)"
            r"[\s\S]*?\bif\s*\(\s*[A-Za-z_$][\w$]*\s*==\s*null\s*\)"
            r"[\s\S]*?\breturn\s+(?P<helper>[A-Za-z_$][\w$]*)\s*\([^,]+,\s*"
            r'"(?P<message>[^"]+)"\s*\+\s*(?P=param)\s*\);',
            body,
        )
        if branch is None or branch.group("param") not in param_names:
            continue
        if branch.group("helper") not in str(helper.get("excerpt", "")):
            continue
        enum = next((entry for entry in evidence
                     if entry.get("role") == "enum_definition"
                     and Path(str(entry.get("file", ""))).stem == branch.group("enum")), None)
        if enum is None or enum.get("excerpt_truncated"):
            continue
        enum_text = str(enum.get("excerpt", ""))
        allowed_codes = set(re.findall(r'^\s*[A-Z][A-Z0-9_]*\("([^"]+)"\)', enum_text, re.M))
        if not allowed_codes or "Objects.equals(code, value.getCode())" not in enum_text or "return null;" not in enum_text:
            continue
        invalid_code = "__llm_autotest_unknown_code__"
        while invalid_code in allowed_codes:
            invalid_code += "_"
        message = branch.group("message")
        controller_ref = str(controller["evidence_id"])
        helper_ref = str(helper["evidence_id"])
        enum_ref = str(enum["evidence_id"])
        header_ref = str(header["evidence_id"])
        return {
            "interface_id": str(interface["id"]),
            "category": "exception",
            "scenario": "请求 code 不属于源码枚举值时返回不支持的请求类型",
            "preconditions": [{"path": branch.group("param"), "op": "equals", "value": invalid_code}],
            "expected_status": "status=200",
            "assertions": [
                {"path": "resultCode", "op": "equals", "value": 1},
                {"path": "message", "op": "contains", "value": message},
            ],
            "evidence_refs": [controller_ref, helper_ref, enum_ref, header_ref],
            "evidence_quotes": [
                {"target": "preconditions[0]", "evidence_ref": enum_ref, "quote": "return null;"},
                {"target": "expected_status", "evidence_ref": header_ref, "quote": "@RestController"},
                {"target": "assertions[0]", "evidence_ref": helper_ref,
                 "quote": 'result.put("resultCode", 1);'},
                {"target": "assertions[1]", "evidence_ref": controller_ref,
                 "quote": f'"{message}"'},
            ],
        }
    return None


def _recover_verified_assertion_subset(
    raw: Any, interfaces: list[dict[str, Any]],
    evidence_by_interface: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], list[str]] | None:
    """After model repair fails, isolate only explicitly rejected assertions.

    Conditions, source paths, HTTP mapping and all retained assertions are parsed
    again. Never rewrite expected values or retain requirement coverage after
    removing an assertion. The original candidate remains in the model audit.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("assertions"), list):
        return None
    interface = next((item for item in interfaces if item.get("id") == raw.get("interface_id")), None)
    if interface is None:
        return None
    candidate = normalize_oracle_bookkeeping(raw, interface, evidence_by_interface.get(str(raw.get("interface_id")), {}))
    candidate["covered_claim_ids"] = []
    isolated: list[str] = []
    while True:
        if not candidate.get("assertions") and candidate.get("oracle_basis") != "integration_entry":
            return None
        try:
            accepted = _parse_test_oracle_response(
                json.dumps({"oracles": [candidate]}, ensure_ascii=False), interfaces,
                evidence_by_interface, strict_items=True,
            )
            return (accepted, isolated) if isolated and accepted else None
        except (PipelineError, ValueError, TypeError) as exc:
            message = exc.message if isinstance(exc, PipelineError) else str(exc)
            match = re.search(r"(?<!preconditions)assertions\[(\d+)\]", message)
            if match is None:
                return None
            index = int(match.group(1))
            assertions = list(candidate["assertions"])
            if not 0 <= index < len(assertions):
                return None
            invalid = assertions.pop(index)
            isolated.append(str(invalid.get("path", f"assertions[{index}]")))
            quotes = []
            for original in candidate.get("evidence_quotes") or []:
                if not isinstance(original, dict):
                    return None
                quote = dict(original)
                target = re.fullmatch(r"assertions\[(\d+)\]", str(quote.get("target", "")))
                if target:
                    position = int(target.group(1))
                    if position == index:
                        continue
                    if position > index:
                        quote["target"] = f"assertions[{position - 1}]"
                quotes.append(quote)
            candidate = {**candidate, "assertions": assertions, "evidence_quotes": quotes}
    return None


def _retain_pending_oracle_design(
    raw: Any,
    interfaces: list[dict[str, Any]],
    evidence_by_interface: dict[str, dict[str, str]],
) -> dict[str, Any] | None:
    """Retain verified source conditions when only response assertions remain invalid.

    This never supplies a replacement expected value. The normal parser must
    still validate the branch, request/state conditions and HTTP source proof;
    the resulting design is explicitly blocked from execution and coverage.
    """
    if not isinstance(raw, dict):
        return None
    interface = next((item for item in interfaces if item.get("id") == raw.get("interface_id")), None)
    if interface is None:
        return None
    texts = evidence_by_interface.get(str(raw.get("interface_id", "")), {})
    raw = normalize_oracle_bookkeeping(raw, interface, texts)
    if (raw.get("category") not in {"normal", "exception", "boundary"}
            or not raw.get("branch_id") or not isinstance(raw.get("assertions"), list)
            or not raw["assertions"] or not isinstance(raw.get("evidence_quotes"), list)):
        return None
    candidate = {
        **raw, "assertions": [], "covered_claim_ids": [],
        "evidence_quotes": [q for q in raw["evidence_quotes"]
                            if not isinstance(q, dict)
                            or not str(q.get("target", "")).startswith("assertions[")],
    }
    if not candidate.get("expected_behavior"):
        branch = next((item for item in _oracle_source_branches(interface, texts)
                       if item["branch_id"] == candidate["branch_id"]), None)
        if branch is None:
            return None
        candidate["expected_behavior"] = {
            "description": "已定位该接口处理出口；固定响应断言未通过校验，保留条件化设计",
            "evidence_ref": branch["evidence_ref"],
            "quote": branch.get("body_quote", branch["quote"]),
        }
    try:
        parsed = _parse_test_oracle_response(
            json.dumps({"oracles": [candidate]}, ensure_ascii=False),
            interfaces, evidence_by_interface, strict_items=True,
        )
    except (PipelineError, TypeError, ValueError):
        return None
    if len(parsed) != 1:
        return None
    oracle = parsed[0]
    oracle["design_status"] = "pending_expectation"
    oracle["execution_blockers"] = ["响应断言未通过源码校验；保留分支设计，暂不执行"]
    return oracle


def _parse_test_oracle_response(
    text: str,
    interfaces: list[dict[str, Any]],
    evidence_by_interface: dict[str, dict[str, str]],
    *,
    rejected: list[tuple[Any, str]] | None = None,
    strict_items: bool = False,
) -> list[dict[str, Any]]:
    """只接收引用本接口源码证据、且可由执行器判定的 oracle 合约。"""
    try:
        data = json.loads(strip_code_fence(text))
    except json.JSONDecodeError as exc:
        raise PipelineError(
            code=X_LLM_02, message="源码预期契约不是合法 JSON", node_id=NODE_NAME
        ) from exc
    if not isinstance(data, dict) or set(data) != {"oracles"} or not isinstance(data["oracles"], list):
        raise PipelineError(
            code=X_LLM_02, message="源码预期契约必须只有 oracles 数组", node_id=NODE_NAME
        )
    if not strict_items:
        valid: list[dict[str, Any]] = []
        for raw in data["oracles"]:
            try:
                valid.extend(_parse_test_oracle_response(
                    json.dumps({"oracles": [raw]}, ensure_ascii=False),
                    interfaces, evidence_by_interface, strict_items=True,
                ))
            except PipelineError as exc:
                if rejected is None:
                    raise
                rejected.append((raw, exc.message))
        return valid
    interface_by_id = {str(item.get("id", "")): item for item in interfaces}
    result: list[dict[str, Any]] = []
    for raw in data["oracles"]:
        if not isinstance(raw, dict):
            raise PipelineError(code=X_LLM_02, message="oracle 条目必须是对象", node_id=NODE_NAME)
        raw_interface = interface_by_id.get(str(raw.get("interface_id", "")))
        if raw_interface is not None:
            raw = normalize_oracle_bookkeeping(
                raw, raw_interface, evidence_by_interface.get(str(raw.get("interface_id", "")), {})
            )
        required_keys = {
            "interface_id", "category", "scenario", "preconditions",
            "expected_status", "assertions", "evidence_refs", "evidence_quotes",
        }
        if not required_keys.issubset(raw) or set(raw) - required_keys - {"branch_id", "branch_outcome", "state_conditions", "expected_behavior", "oracle_basis", "covered_claim_ids"}:
            raise PipelineError(
                code=X_LLM_02, message="oracle 字段与输出契约不一致", node_id=NODE_NAME
            )
        interface_id = str(raw.get("interface_id", ""))
        interface = interface_by_id.get(interface_id)
        if interface is None:
            raise PipelineError(
                code=X_LLM_02, message=f"oracle 引用了本批不存在的接口：{interface_id}", node_id=NODE_NAME
            )
        branch_catalog = _oracle_source_branches(
            interface, evidence_by_interface.get(interface_id, {})
        )
        branch_id = raw.get("branch_id")
        selected_branch = next(
            (branch for branch in branch_catalog if branch["branch_id"] == branch_id), None
        )
        framework_design = framework_body_design(interface, evidence_by_interface.get(interface_id, {}))
        framework_boundary = bool(framework_design and raw.get("oracle_basis") == "spring_required_body"
                                  and raw.get("preconditions") == framework_design["preconditions"])
        integration_entry = raw.get("oracle_basis") == "integration_entry"
        if integration_entry and (raw.get("category") != "normal" or branch_id is not None
                                  or raw.get("covered_claim_ids")):
            raise PipelineError(code=X_LLM_02, node_id=NODE_NAME,
                                message="integration_entry 只保留普通入口的 HTTP/结构检查，不能冒充内部业务分支或强制状态设计")
        if not framework_boundary and not integration_entry and (branch_catalog or branch_id is not None) and selected_branch is None:
            raise PipelineError(
                code=X_LLM_02, node_id=NODE_NAME,
                message=f"oracle {interface_id} 缺少本接口可核验的 branch_id，不能只凭场景描述绑定业务分支",
            )
        branch_outcome = raw.get("branch_outcome", True)
        if not isinstance(branch_outcome, bool):
            raise PipelineError(code=X_LLM_02, message="branch_outcome 必须是布尔值", node_id=NODE_NAME)
        category = str(raw.get("category", ""))
        if category not in {"normal", "boundary", "exception"}:
            raise PipelineError(code=X_LLM_02, message="oracle category 不合法", node_id=NODE_NAME)
        scenario = str(raw.get("scenario", "")).strip()
        expected_status = str(raw.get("expected_status", "")).strip()
        if not scenario or not re.fullmatch(r"(?:status=[1-5]\d{2}|status_class=[1-5]xx|unresolved)", expected_status):
            raise PipelineError(
                code=X_LLM_02,
                message="oracle 缺少场景或源码可证的 HTTP 状态预期",
                node_id=NODE_NAME,
            )
        if not isinstance(raw.get("preconditions"), list) or not isinstance(raw.get("assertions"), list):
            raise PipelineError(
                code=X_LLM_02,
                message=f"oracle {interface_id} 的 preconditions/assertions 必须是数组",
                node_id=NODE_NAME,
            )
        try:
            preconditions = normalize_response_assertions(raw.get("preconditions", []), label="preconditions")
            preconditions = _canonicalize_request_condition_paths(
                preconditions, interface,
                "\n".join(evidence_by_interface.get(interface_id, {}).values()),
            )
            assertions = normalize_response_assertions(raw.get("assertions", []))
        except ValueError as exc:
            raise PipelineError(
                code=X_LLM_02, message=f"oracle 条件/断言格式错误：{exc}", node_id=NODE_NAME
            ) from exc
        for assertion_index, assertion in enumerate(assertions):
            value = assertion.get("value")
            if (
                assertion.get("op") in {"equals", "not_equals", "contains"}
                and isinstance(value, str)
                and re.search(r"\b[A-Za-z_$][\w$]*\.[A-Za-z_$][\w$]*\s*\(", value)
            ):
                raise PipelineError(
                    code=X_LLM_02,
                    message=f"oracle {interface_id} 的 assertions[{assertion_index}] 把 Java 表达式当成响应字面值：{value}",
                    node_id=NODE_NAME,
                )
        for condition in preconditions:
            value = condition.get("value")
            if (
                condition.get("op") in {"equals", "not_equals", "contains"}
                and isinstance(value, str)
                and re.search(r"\b[A-Za-z_$][\w$]*\.[A-Za-z_$][\w$]*\s*\(", value)
            ):
                raise PipelineError(
                    code=X_LLM_02,
                    message=f"oracle {interface_id} 把 Java 表达式当成请求参数值：{value}",
                    node_id=NODE_NAME,
                )
        raw_evidence_refs = raw.get("evidence_refs")
        if not isinstance(raw_evidence_refs, list) or any(
            not isinstance(item, str) for item in raw_evidence_refs
        ):
            raise PipelineError(
                code=X_LLM_02, message=f"oracle {interface_id} evidence_refs 必须是字符串数组", node_id=NODE_NAME
            )
        evidence_refs = list(dict.fromkeys(raw_evidence_refs))
        evidence_text_by_id = evidence_by_interface.get(interface_id, {})
        allowed_refs = set(evidence_text_by_id)
        if not evidence_refs or any(item not in allowed_refs for item in evidence_refs):
            raise PipelineError(
                code=X_LLM_02,
                message=f"oracle {interface_id} 缺少本接口的有效源码证据引用",
                node_id=NODE_NAME,
            )
        if selected_branch and selected_branch["evidence_ref"] not in evidence_refs:
            evidence_refs.append(selected_branch["evidence_ref"])
        if category == "normal" and not assertions and not raw.get("expected_behavior") and selected_branch:
            raw = {**raw, "expected_behavior": {
                "description": "保留该接口路径的测试设计；响应字段或结果关系预期待补充",
                "evidence_ref": selected_branch["evidence_ref"], "quote": selected_branch["quote"],
            }}
        try:
            expected_behavior = normalize_expected_behavior(
                raw.get("expected_behavior"),
                {ref: {"excerpt": source, "interface_ids": [interface_id]}
                 for ref, source in evidence_text_by_id.items()}, interface_id, evidence_refs,
            )
            if expected_status == "unresolved" and expected_behavior is None:
                raise ValueError("HTTP 映射未确认的设计必须提供源码可证的 expected_behavior")
            state_conditions = normalize_state_conditions(
                raw.get("state_conditions"),
                {ref: {"excerpt": source, "interface_ids": [interface_id]}
                 for ref, source in evidence_text_by_id.items()},
                interface_id, evidence_refs,
            )
        except ValueError as exc:
            raise PipelineError(code=X_LLM_02, message=str(exc), node_id=NODE_NAME) from exc
        if integration_entry and any(c.get("mode") == "controlled" for c in state_conditions):
            raise PipelineError(code=X_LLM_02, node_id=NODE_NAME,
                                message="integration_entry 只保留普通入口的 HTTP/结构检查，不能冒充内部业务分支或强制状态设计")
        if integration_entry and response_assertion_scope(assertions) == "business":
            entry_issue = entry_assertion_issue(assertions, evidence_text_by_id)
            if entry_issue:
                raise PipelineError(code=X_LLM_02, node_id=NODE_NAME, message=entry_issue)
        if selected_branch:
            kinds = set(filter(None, selected_branch.get("required_controls", "").split(",")))
            for kind in sorted(kinds):
                if any(c["kind"] == kind and c["evidence_ref"] == selected_branch["evidence_ref"]
                       and c["quote"] == selected_branch["quote"] for c in state_conditions):
                    continue
                same_origin = next((c for c in state_conditions if c["kind"] == kind), None)
                if same_origin is not None:
                    if kind == selected_branch["control_kind"] and re.match(r"\s*if\s*\(", same_origin["quote"]):
                        same_origin.update({"evidence_ref": selected_branch["evidence_ref"],
                                            "quote": selected_branch["quote"],
                                            "description": f"目标接口条件 {selected_branch['quote']} 取 {str(branch_outcome).lower()}"})
                    continue
                normal_environment = (kind in {"application_config", "service_registry"}
                                      and selected_branch["control_kind"] != kind)
                normal_result = category == "normal" and branch_outcome is False
                state_conditions.append({
                    "kind": kind,
                    "description": ("常规环境依赖正常可用" if normal_environment else
                                    f"源码条件 {selected_branch['quote']} 取 {str(branch_outcome).lower()}"),
                    "mode": "integration" if normal_environment or normal_result else "controlled", "evidence_ref": selected_branch["evidence_ref"],
                    "quote": selected_branch["quote"],
                })
        for condition in state_conditions:
            if condition["mode"] != "integration":
                continue
            # Live integration is for ordinary success prerequisites. Deliberate
            # null/empty/error states still need control, even when handled normally.
            guard = re.sub(r"\s+", "", condition["quote"])
            abnormal_arm = bool(selected_branch and condition["quote"] == selected_branch["quote"] and (
                ("==null" in guard and branch_outcome)
                or ("!=null" in guard and not branch_outcome)
                or (("isEmpty(" in guard or '!"0".equals' in guard) and branch_outcome)
            ))
            controlling_kind = selected_branch["control_kind"] if selected_branch else ""
            ordinary_ancestor = condition["kind"] in {"application_config", "service_registry"} and controlling_kind != condition["kind"]
            if (category != "normal" or abnormal_arm) and not ordinary_ancestor:
                raise PipelineError(code=X_LLM_02, message="特定空值/错误状态必须作为 controlled 场景条件，不能冒充正常联调前提", node_id=NODE_NAME)
        raw_quotes = raw.get("evidence_quotes")
        if not isinstance(raw_quotes, list):
            raise PipelineError(
                code=X_LLM_02, message=f"oracle {interface_id} evidence_quotes 必须是数组", node_id=NODE_NAME
            )
        raw_quotes = list(raw_quotes)
        required_quote_targets = {"expected_status"}
        if expected_behavior:
            required_quote_targets.add("expected_behavior")
            raw_quotes.append({"target": "expected_behavior", "evidence_ref": expected_behavior["evidence_ref"],
                               "quote": expected_behavior["quote"]})
            if expected_status == "unresolved":
                raw_quotes.append({"target": "expected_status", "evidence_ref": expected_behavior["evidence_ref"],
                                   "quote": expected_behavior["quote"]})
        for index, condition in enumerate(state_conditions):
            target = f"state_conditions[{index}]"
            required_quote_targets.add(target)
            raw_quotes.append({"target": target, "evidence_ref": condition["evidence_ref"],
                               "quote": condition["quote"]})
        required_quote_targets.update(f"preconditions[{index}]" for index in range(len(preconditions)))
        required_quote_targets.update(f"assertions[{index}]" for index in range(len(assertions)))
        if selected_branch:
            required_quote_targets.add("branch")
            raw_quotes.append({
                "target": "branch", "evidence_ref": selected_branch["evidence_ref"],
                "quote": selected_branch["quote"],
            })
        for index, condition in enumerate(preconditions):
            target = f"preconditions[{index}]"
            if condition.get("op") != "exists" or any(q.get("target") == target for q in raw_quotes if isinstance(q, dict)):
                continue
            name = str(condition.get("path", ""))
            if not any(param.get("name") == name and param.get("in") == "body" for param in interface.get("params", [])):
                continue
            for ref, source in evidence_text_by_id.items():
                binding = re.search(r"@RequestBody(?:\s*\([^)]*\))?\s+(?:@\w+(?:\([^)]*\))?\s+)*[\w.<>]+\s+" + re.escape(name) + r"\b", source)
                if binding:
                    if ref not in evidence_refs:
                        evidence_refs.append(ref)
                    raw_quotes.append({"target": target, "evidence_ref": ref, "quote": binding.group(0)})
                    break
        quoted_targets: set[str] = set()
        quoted_refs: set[str] = set()
        normalized_quotes: list[dict[str, str]] = []
        for quote in raw_quotes:
            if not isinstance(quote, dict) or set(quote) != {"target", "evidence_ref", "quote"}:
                raise PipelineError(
                    code=X_LLM_02, message=f"oracle {interface_id} evidence quote 结构不合法", node_id=NODE_NAME
                )
            if any(not isinstance(quote.get(key), str) for key in ("target", "evidence_ref", "quote")):
                raise PipelineError(
                    code=X_LLM_02, message=f"oracle {interface_id} evidence quote 字段必须是字符串", node_id=NODE_NAME
                )
            target = quote["target"]
            evidence_ref = quote["evidence_ref"]
            quoted_text = quote["quote"]
            if (
                target not in required_quote_targets
                or evidence_ref not in evidence_text_by_id
                or evidence_ref not in evidence_refs
                or not quoted_text
                or quoted_text not in evidence_text_by_id[evidence_ref]
            ):
                raise PipelineError(
                    code=X_LLM_02,
                    message=f"oracle {interface_id} 的 {target or '未知目标'} 未被源码原文引用支持",
                    node_id=NODE_NAME,
                )
            quoted_targets.add(target)
            quoted_refs.add(evidence_ref)
            normalized_quotes.append({
                "target": target, "evidence_ref": evidence_ref, "quote": quoted_text
            })
        if not required_quote_targets.issubset(quoted_targets):
            missing_targets = sorted(required_quote_targets - quoted_targets)
            raise PipelineError(
                code=X_LLM_02,
                message=f"oracle {interface_id} 的预期缺少逐项源码原文：{', '.join(missing_targets)}",
                node_id=NODE_NAME,
            )
        status_quotes = [
            quote["quote"] for quote in normalized_quotes
            if quote["target"] == "expected_status"
        ]
        if not framework_boundary and expected_status not in {"status=200", "unresolved"} and not _explicit_http_status_supported(
            expected_status, status_quotes
        ):
            raise PipelineError(
                code=X_LLM_02,
                message=f"oracle {interface_id} 的 expected_status 缺少明确的 HTTP 状态源码依据",
                node_id=NODE_NAME,
            )
        for index, assertion in enumerate(assertions):
            if assertion.get("op") == "equals_request":
                value = str(assertion.get("value", ""))
                root, _, field = value.partition(".")
                response_field = str(assertion["path"]).split(".")[-1]
                mapping_quote = "\n".join(q["quote"] for q in normalized_quotes if q["target"] == f"assertions[{index}]")
                if (root not in {str(param.get("name")) for param in interface.get("params", [])}
                        or not field or "." in field
                        or not re.search(re.escape(root) + r'\.get\w*\(\s*"' + re.escape(field) + r'"', mapping_quote)
                        or not re.search(r"\.set" + re.escape(response_field[:1].upper() + response_field[1:]) + r"\s*\(", mapping_quote)):
                    raise PipelineError(code=X_LLM_02, node_id=NODE_NAME, message=f"assertions[{index}] equals_request 缺少本接口请求 getter 与响应 setter 映射依据")
            if assertion.get("op") == "not_exists":
                field = str(assertion["path"]).split(".")[-1]
                own_quotes = [quote["quote"] for quote in normalized_quotes
                              if quote["target"] == f"assertions[{index}]"]
                removal = re.compile(r'\.remove\s*\(\s*"' + re.escape(field) + r'"\s*\)')
                if not any(removal.search(quote) or ("@JsonIgnore" in quote and field in quote)
                           for quote in own_quotes):
                    raise PipelineError(
                        code=X_LLM_02, node_id=NODE_NAME,
                        message=(f"oracle {interface_id} 的 assertions[{index}] 缺少字段删除/序列化忽略证据；"
                                 "return response 或普通 setter 不能证明 JSON 字段不存在"),
                    )
            if assertion.get("op") not in {
                "equals", "not_equals", "contains", "greater_than", "less_than",
                "in", "not_in",
            }:
                continue
            quotes = [
                str(quote["quote"]) for quote in normalized_quotes
                if quote["target"] == f"assertions[{index}]"
            ]
            values = assertion["value"] if assertion.get("op") in {"in", "not_in"} else [assertion.get("value")]
            if not all(any(_literal_supported_by_quote(value, quote) for quote in quotes) for value in values):
                raise PipelineError(
                    code=X_LLM_02,
                    message=f"oracle {interface_id} 的 assertions[{index}] 期望值不是引用源码中的字面值",
                    node_id=NODE_NAME,
                )
        factory_issue = factory_assertion_issue({**raw, "evidence_quotes": normalized_quotes}, evidence_text_by_id)
        if factory_issue:
            raise PipelineError(code=X_LLM_02, message=factory_issue, node_id=NODE_NAME)
        assignment_issue = exception_assignment_issue(raw, evidence_text_by_id)
        if assignment_issue:
            raise PipelineError(code=X_LLM_02, message=assignment_issue, node_id=NODE_NAME)
        if not quoted_refs:
            raise PipelineError(
                code=X_LLM_02, message=f"oracle {interface_id} 没有可核对的源码原文", node_id=NODE_NAME
            )
        parsed_oracle: dict[str, Any] = {
            "interface_id": interface_id,
            "category": category,
            "scenario": scenario,
            "preconditions": preconditions,
            "expected_status": expected_status,
            "assertions": assertions,
            "evidence_refs": evidence_refs,
            "evidence_quotes": normalized_quotes,
        }
        if framework_boundary:
            parsed_oracle["oracle_basis"] = "spring_required_body"
        elif integration_entry:
            parsed_oracle["oracle_basis"] = "integration_entry"
        if selected_branch:
            parsed_oracle["branch_id"] = selected_branch["branch_id"]
            parsed_oracle["branch_outcome"] = branch_outcome
        if state_conditions:
            parsed_oracle["state_conditions"] = state_conditions
        if expected_behavior:
            parsed_oracle["expected_behavior"] = expected_behavior
        parsed_oracle["assertion_scope"] = "framework_boundary" if framework_boundary else response_assertion_scope(assertions)
        parsed_oracle["covered_claim_ids"] = [str(value) for value in raw.get("covered_claim_ids", [])
                                                if isinstance(value, str) and re.fullmatch(r"CLM-\d{3,}", value)]
        if (category == "normal" and not assertions
                and not (integration_entry and expected_status != "unresolved")):
            parsed_oracle["design_status"] = "pending_expectation"
            parsed_oracle["execution_blockers"] = ["响应字段或结果关系预期待补充"]
        result.append(parsed_oracle)
    return result


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
    claims: list[dict[str, Any]], interfaces: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """将全量接口清单交给模型，避免相关性预筛漏掉语义相关入口。"""
    del claims  # 保留签名，便于调用点明确该清单用于 claim 评审。
    return list(interfaces)


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
    return evidence


def _collect_code_evidence(
    inventory: dict[str, Any], interfaces: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """抽取可引用的服务端行为证据片段（确定性、全量、脱敏）。

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
    # 变更文件优先：本节点评审的是「本次改动」，但优先级只影响顺序，不会裁掉其它接口。
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
    for item in ordered_sources:
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
        excerpt = _SECRET_VALUE.sub(r"\1\2<redacted>", excerpt)
        if not excerpt.strip():
            continue
        evidence.append(
            {
                "category": item["category"],
                "repo": repo_name,
                "file": relative_path,
                "start_line": snippet_start,
                "end_line": snippet_end,
                "_source_span": {
                    "start_line": int(item["start_line"]),
                    "end_line": int(item["end_line"]),
                },
                "excerpt": excerpt,
                "interface_ids": item["interface_ids"],
                "role": item["role"],
            }
        )
    for repo in inventory.get("repositories") or []:
        if not isinstance(repo, dict):
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
        for path in root.rglob("*"):
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
        )
        for relative_path in candidates:
            path = (root / relative_path).resolve()
            if root not in path.parents or path.suffix.lower() not in _REVIEWABLE_SUFFIXES:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            captured_categories: set[str] = set()
            for line_no, line in enumerate(lines, start=1):
                matched_category = next(
                    (name for name, pattern in _EVIDENCE_RULES.items() if pattern.search(line)),
                    None,
                )
                if matched_category is None or matched_category in captured_categories:
                    continue
                start = max(1, line_no - 2)
                end = min(len(lines), line_no + 2)
                excerpt = "\n".join(lines[start - 1 : end])
                excerpt = _SECRET_VALUE.sub(r"\1\2<redacted>", excerpt)
                evidence.append(
                    {
                        "category": matched_category,
                        "repo": str(repo.get("name", "")),
                        "file": relative_path,
                        "start_line": start,
                        "end_line": end,
                        "excerpt": excerpt,
                    }
                )
                # 单个文件/类别一个片段足够；避免同一配置重复占满上下文。
                captured_categories.add(matched_category)
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
        body_spans = [
            item
            for item in interface.get("code_spans") or []
            if isinstance(item, dict) and item.get("kind") == "body"
        ]
        if isinstance(graph, dict):
            entry_span = body_spans[0] if body_spans else {}
            graph_edges = graph.get("edges") or []
            chains.append(
                {
                    "interface_id": interface_id,
                    "entry": {
                        "repo": repo_name,
                        "file": relative,
                        "role": str(entry_span.get("role", "controller")),
                        "method_name": str(entry_span.get("method_name", "")),
                        "start_line": entry_span.get("start_line", location.get("start_line")),
                        "end_line": entry_span.get("end_line", location.get("end_line")),
                    },
                    "callees": [
                        {
                            "file": str(item.get("file", "")),
                            "role": str(item.get("role", "other")),
                            "method_name": str(item.get("method_name", "")),
                            "start_line": item.get("start_line"),
                            "end_line": item.get("end_line"),
                            "depth": item.get("depth", 1),
                            "call_line": item.get("call_line"),
                        }
                        for item in spans
                    ],
                    "edges": [
                        {
                            key: edge.get(key)
                            for key in (
                                "caller_file",
                                "caller_role",
                                "caller_method",
                                "caller_start_line",
                                "caller_end_line",
                                "callee_file",
                                "callee_role",
                                "callee_method",
                                "callee_start_line",
                                "callee_end_line",
                                "depth",
                                "call_line",
                            )
                            if edge.get(key) is not None
                        }
                        for edge in graph_edges
                        if isinstance(edge, dict)
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
        end = min(len(lines), int(location.get("end_line", len(lines))))
        fragment = "\n".join(lines[start:end])
        callees = sorted({match.group(1) for match in call_pattern.finditer(fragment)})
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
    """逐页发送完整证据，以 claim 为单位汇总语义命中的证据。

    中转站没有 embedding 接口（`/v1/embeddings` → 401 key_model_access_denied），
    语义召回只能交给模型。完整源码按连续片段分页传入，不再发送 160 字预览，也不设置
    候选总数上限。若某页输出无法核验，则保留该页全部证据，避免模型检索输出导致证据
    被静默丢弃。
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
            "end_line": item.get("end_line"),
            "interface_ids": list(item.get("interface_ids") or []),
            "excerpt": str(item.get("excerpt", "")),
            **(
                {
                    "excerpt_part": item["excerpt_part"],
                    "excerpt_parts": item["excerpt_parts"],
                }
                if item.get("excerpt_part") is not None
                else {}
            ),
        }
        for item in _split_evidence_for_transport(code_evidence, max_excerpt_chars=8_000)
        if item.get("evidence_id") and not item.get("excerpt_truncated")
    ]
    pages = chunk_json_items(catalog, max_chars=12_000)
    by_id = {
        str(item.get("evidence_id", "")): item
        for item in code_evidence
        if item.get("evidence_id") and not item.get("excerpt_truncated")
    }
    selected_ids: set[str] = set()
    for page_index, page in enumerate(pages, start=1):
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
                "catalog_page": {"index": page_index, "total": len(pages), "items": page},
            },
            ensure_ascii=False,
        )
        result = await llm.complete(_EVIDENCE_SELECT_PROMPT, prompt, user_content)
        try:
            data = json.loads(strip_code_fence(result.text))
        except json.JSONDecodeError:
            logger.warning("证据语义召回第 %d/%d 页输出不是合法 JSON，保留该页全部证据", page_index, len(pages))
            selected_ids.update(str(item["evidence_id"]) for item in page)
            continue
        expected_claim_ids = sorted(str(claim.get("claim_id", "")) for claim in claims)
        raw_claim_ids = data.get("claim_ids") if isinstance(data, dict) else None
        raw_ids = data.get("evidence_ids") if isinstance(data, dict) else None
        page_ids = {str(item.get("evidence_id", "")) for item in page}
        if (
            not isinstance(raw_ids, list)
            or not isinstance(raw_claim_ids, list)
            or sorted(str(value) for value in raw_claim_ids) != expected_claim_ids
            or any(str(value) not in page_ids for value in raw_ids)
        ):
            logger.warning("证据语义召回第 %d/%d 页引用无法核验，保留该页全部证据", page_index, len(pages))
            selected_ids.update(page_ids)
            continue
        selected_ids.update(str(value) for value in raw_ids)

    return [item for evidence_id, item in by_id.items() if evidence_id in selected_ids]


def _split_evidence_for_transport(
    evidence_items: list[dict[str, Any]], *, max_excerpt_chars: int
) -> list[dict[str, Any]]:
    """按完整字符边界拆分超长源码片段；所有片段都会发送，不丢字符。"""
    fragments: list[dict[str, Any]] = []
    for item in evidence_items:
        excerpt = str(item.get("excerpt", ""))
        if len(excerpt) <= max_excerpt_chars:
            fragments.append(item)
            continue
        pieces: list[tuple[int, int, str]] = []
        offset = 0
        while offset < len(excerpt):
            end = min(len(excerpt), offset + max_excerpt_chars)
            if end < len(excerpt):
                newline = excerpt.rfind("\n", offset, end)
                if newline > offset:
                    end = newline + 1
            pieces.append((offset, end, excerpt[offset:end]))
            offset = end
        original_start = int(item.get("start_line") or 1)
        for part_index, (start, end, text_part) in enumerate(pieces, start=1):
            fragment = dict(item)
            fragment["excerpt"] = text_part
            fragment["excerpt_part"] = part_index
            fragment["excerpt_parts"] = len(pieces)
            fragment["start_line"] = original_start + excerpt[:start].count("\n")
            fragment["end_line"] = fragment["start_line"] + text_part.count("\n")
            fragments.append(fragment)
    return fragments


def _select_evidence_for_claims(
    claims: list[dict[str, Any]],
    code_evidence: list[dict[str, Any]],
    preferred_interface_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """按 claim 关键词、接口关联和高风险类别召回证据，不设候选数量上限。"""
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
        if evidence.get("excerpt_truncated"):
            continue
        evidence_text = " ".join(
            str(evidence.get(key, "")) for key in ("file", "excerpt", "category", "interface_ids")
        )
        overlap = len(claim_tokens & _content_tokens(evidence_text))
        linked_interfaces = set(str(value) for value in evidence.get("interface_ids", []))
        if preferred_interface_ids and linked_interfaces.intersection(preferred_interface_ids):
            overlap += 12
        if evidence.get("category") in categories:
            overlap += 24
        if overlap == 0:
            continue
        ranked.append((overlap, evidence))
    ranked.sort(key=lambda entry: entry[0], reverse=True)
    return [entry[1] for entry in ranked]


def _scan_coverage(
    inventory: dict[str, Any],
    code_evidence: list[dict[str, Any]] | None = None,
    interfaces: list[dict[str, Any]] | None = None,
    scan_integrity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """扫描覆盖率与"模型到底看全了没有"（P0①/②）。

    `complete=true` 的语义是"仓库扫描完整，你已看到全部接口"——prompt 直接依赖它。
    LLM 请求按 claim、接口和证据页拆分但不丢弃条目；源码读失败、接口缺证据或扫描完整性校验
    缺失仍会让 complete=false。
    """
    repositories = [item for item in inventory.get("repositories", []) if isinstance(item, dict)]
    evidence_limit_reached = False
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
        and not evidence.get("excerpt_truncated")
        for interface_id in evidence.get("interface_ids") or []
        if str(interface_id)
    }
    missing_interface_ids = sorted(expected_interface_ids - covered_interface_ids)
    interface_evidence_truncated = bool(missing_interface_ids)
    call_chain_truncated = False
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
        "interface_evidence_limit": 0,
        "interface_evidence_count": len(covered_interface_ids),
        "interface_evidence_missing": missing_interface_ids,
        "call_chain_truncated": call_chain_truncated,
        "call_chain_limit": 0,
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
        note_parts: list[str] = []
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


def _merge_interface_claim_assessments(
    claim: dict[str, Any],
    assessments: list[dict[str, Any]],
    *,
    scan_complete: bool,
) -> dict[str, Any]:
    """把逐接口审查结果合成单条 claim 结论，保留全部证据引用与条件。"""
    statuses = [str(item.get("status", "undetermined")) for item in assessments]
    # 一个接口上的局部结论不能覆盖另一个相关接口上的明确缺口。
    if "partial" in statuses:
        status = "partial"
    elif "implemented" in statuses:
        status = "implemented"
    elif statuses and all(value == "missing" for value in statuses) and scan_complete:
        status = "missing"
    else:
        status = "undetermined"

    decisive = [
        item for item in assessments
        if item.get("status") in {"implemented", "partial", "missing"}
    ]
    confidence_source = decisive or assessments
    confidence = min(
        (float(item.get("confidence", 0.0)) for item in confidence_source),
        default=0.0,
    )

    def collect(field: str) -> list[str]:
        return list(dict.fromkeys(
            str(value)
            for item in assessments
            for value in item.get(field, [])
            if str(value)
        ))

    verification_needed = collect("verification_needed")
    if status == "undetermined" and not verification_needed:
        verification_needed.append("逐接口检查后仍缺少足以支持整体结论的直接源码证据。")
    return {
        "claim_id": str(claim["claim_id"]),
        "status": status,
        "confidence": round(confidence, 2),
        "interface_ids": collect("interface_ids"),
        "evidence_refs": collect("evidence_refs"),
        "met_conditions": collect("met_conditions"),
        "unmet_conditions": collect("unmet_conditions"),
        "verification_needed": verification_needed,
    }


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
        relevant = _select_evidence_for_claims([claim], code_evidence)
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
    store: ArtifactStore, job_id: str, index: int, total: int, count: int,
    *, event: str = "started", duration_ms: int = 0, concurrency: int = 1,
) -> None:
    """记录单条 Claim 开始/完成时间；并行完成顺序不代表最终产物顺序。"""
    entry = {
        "call_id": f"REVIEW-{index:03d}",
        "timestamp": datetime.now(UTC).isoformat(),
        "node": NODE_NAME,
        "event": event,
        "concurrency": concurrency,
        "batch": {"index": index, "total": total, "claims": count},
        "ok": True,
        "duration_ms": duration_ms,
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
        entry = {
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
    unmatched_input = unmatched

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
                for item in (chain.get("callees") or [])
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
                {"id": str(item.get("id", "")), "summary": str(item.get("summary", ""))}
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
