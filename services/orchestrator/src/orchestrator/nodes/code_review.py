"""code_review 节点：三视图产出（F-REVW-01~05）——按原子声明分批审查，
风险清单与遗漏功能清单程序化派生（R-REVW-02：与映射表 100% 一致）；
双分支配置时程序化派生视图四·变更范围（C-09，不经 LLM）。

产物：review_mapping.json（Day1 冻结口径：三视图合一单产物；
R-REVW-03 的三文件名是规格草案表述，以 schemas/ 冻结文件名为准）。

程序化派生（数值/清单不经 LLM）：
- risks：status=partial 自动进入（R-REVW-04：实现不完整即风险）；
- missing_features：status=missing 条目提取，title 从需求条目回填；
- evidence.code_location：从 interface_id 对应接口回填（LLM 给的忽略）；
- change_scope（C-09）：code_inventory.changed_files × interfaces.code_location
  文件关联 → 变更接口清单，covered_by 从映射表回填（空 = 未被需求覆盖）；
- unmatched_changed_files（C-09）：变更但未命中任何接口定义文件的文件
  （业务逻辑层变更等——接口签名未变但行为可能变更，需人工复核）；
- removed_files（C-09）：被测侧已删除文件单列（无源码可关联接口）。

code_inventory.json 无 changed_files 键（未配基线分支）时两视图为空数组。

校验（R-REVW-01 覆盖率 100% + 虚构防护）：
- 每条输入 REQ 必须有且仅一个映射条目，缺/多/虚构 REQ → X-LLM-02；
- interface_id 必须存在于 interfaces.json → 虚构即 X-LLM-02。

失败口径（docs/12）：
- LLM 输出不可解析/未过 Schema → X-LLM-02；前置产物缺失/不合规 → X-ORCH-01。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from orchestrator.artifacts import ArtifactStore
from orchestrator.engine import Engine, JobState
from orchestrator.errors import X_LLM_02, X_ORCH_01, PipelineError
from orchestrator.llm import LlmResult
from orchestrator.llm_batch import chunk_json_items
from orchestrator.llm_repair import complete_with_json_repair
from orchestrator.prompts_loader import PromptSpec, load_prompt

NODE_NAME = "code_review"

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
_MAX_EVIDENCE_FILES = 160
_MAX_EVIDENCE_ITEMS = 72
_MAX_EVIDENCE_CHARS = 520
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
    """执行 code_review 全流程：读前置产物 → LLM 映射 → 派生三视图 → 落盘。

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
        # 在代码证据提取前按需求选择接口入口，优先确保本任务相关业务代码进入证据池。
        interface_index = _compact_interfaces(interfaces)
        all_claims = _flatten_requirement_claims(requirements)
        review_batches = chunk_json_items(all_claims, max_chars=5_000)
        relevant_interfaces: list[dict[str, Any]] = []
        relevant_interface_ids: set[str] = set()
        candidates_by_batch = [
            _select_interfaces_for_claims(batch, interface_index) for batch in review_batches
        ]
        # 轮转批次挑选，避免前 36 个接口挤占证据预算、后续需求完全没有业务代码上下文。
        for rank in range(max((len(items) for items in candidates_by_batch), default=0)):
            for candidates in candidates_by_batch:
                if rank >= len(candidates):
                    continue
                item = candidates[rank]
                interface_id = str(item.get("id", ""))
                if interface_id and interface_id not in relevant_interface_ids:
                    relevant_interfaces.append(item)
                    relevant_interface_ids.add(interface_id)
                if len(relevant_interfaces) >= 36:
                    break
            if len(relevant_interfaces) >= 36:
                break
        behavior_evidence = _behavior_evidence(inventory)
        code_evidence = _collect_code_evidence(inventory, relevant_interfaces)
        for evidence_index, evidence in enumerate(code_evidence, start=1):
            evidence["evidence_id"] = f"EVD-{evidence_index:04d}"
        call_chains = _derive_call_chains(inventory, interfaces)
        incremental_focus = _collect_incremental_focus(inventory, interfaces)
        deterministic_checks = _derive_deterministic_checks(requirements, code_evidence)
        scan_coverage = _scan_coverage(inventory, code_evidence)

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
        payload: dict[str, Any] = {
            "schema_version": "2.0",
            "job_id": state.job_id,
            "mappings": mappings,
            "evidence_catalog": code_evidence,
            "scan_coverage": scan_coverage,
            "risks": _derive_risks(mappings, deterministic_checks),
            "missing_features": _derive_missing(mappings, requirements),
            "deterministic_checks": deterministic_checks,
            "change_scope": change_scope,
            "removed_files": removed_files,
            "unmatched_changed_files": unmatched,
            "call_chains": call_chains,
            "incremental_findings": incremental_findings,
        }
        try:
            path = store.write(state.job_id, "review_mapping.json", payload)
        except PipelineError as exc:
            # 源头是 LLM 输出不合规：归 X-LLM-02（错误码语义归位）
            if exc.code == X_ORCH_01:
                raise PipelineError(
                    code=X_LLM_02,
                    message=f"LLM 输出未过 Schema 校验：{exc.message}",
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
    claims: list[dict[str, Any]], interfaces: list[dict[str, Any]], limit: int = 24
) -> list[dict[str, Any]]:
    """按声明词项收窄接口上下文，避免每批重复塞入整仓接口目录。"""
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
        str(repo.get("name", "")): Path(str(repo.get("path", "")))
        for repo in inventory.get("repositories") or []
        if isinstance(repo, dict)
    }
    seen_locations: set[tuple[str, str, int]] = set()
    # 普通业务声明也需要代码证据；先为接口实现位置建立有限的直接证据入口，
    # 再补充跨接口的安全、配置、事务等服务端行为片段。
    for interface in (interfaces or [])[:36]:
        location = interface.get("code_location") or {}
        repo_name = str(location.get("repo", ""))
        relative_path = str(location.get("file", ""))
        try:
            start_line = int(location.get("start_line", 0))
        except (TypeError, ValueError):
            continue
        key = (repo_name, relative_path, start_line)
        if not repo_name or not relative_path or start_line < 1 or key in seen_locations:
            continue
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
        if start_line > len(lines):
            continue
        try:
            declared_end = int(location.get("end_line", start_line))
        except (TypeError, ValueError):
            declared_end = start_line
        snippet_start = max(1, start_line - 2)
        snippet_end = min(len(lines), max(start_line + 8, min(declared_end, start_line + 24)) + 2)
        excerpt = "\n".join(lines[snippet_start - 1 : snippet_end])
        excerpt = _SECRET_VALUE.sub(r"\1\2<redacted>", excerpt)[:_MAX_EVIDENCE_CHARS]
        if not excerpt.strip():
            continue
        interface_id = str(interface.get("id", ""))
        evidence.append(
            {
                "category": "interface_implementation",
                "repo": repo_name,
                "file": relative_path,
                "start_line": snippet_start,
                "end_line": snippet_end,
                "excerpt": excerpt,
                "interface_ids": [interface_id] if interface_id else [],
            }
        )
        seen_locations.add(key)

    for repo in inventory.get("repositories") or []:
        if not isinstance(repo, dict) or len(evidence) >= _MAX_EVIDENCE_ITEMS:
            continue
        root = Path(str(repo.get("path", "")))
        if not root.is_dir():
            continue
        root = root.resolve()
        files = repo.get("files") or []
        candidates: list[str] = []
        for item in files[:_MAX_EVIDENCE_FILES]:
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
    """从接口定义附近静态提取一跳服务调用，失败即留空而非补造调用链。"""
    roots = {
        str(repo.get("name", "")): Path(str(repo.get("path", "")))
        for repo in inventory.get("repositories") or []
        if isinstance(repo, dict)
    }
    chains: list[dict[str, Any]] = []
    call_pattern = re.compile(
        r"\b([A-Za-z_][A-Za-z0-9_]*(?:service|repository|dao|mapper|client))\s*\.", re.I
    )
    for interface in interfaces[:120]:
        location = interface.get("code_location") or {}
        repo_name = str(location.get("repo", ""))
        relative = str(location.get("file", ""))
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
                "interface_id": str(interface.get("id", "")),
                "entry": {"repo": repo_name, "file": relative},
                "callees": callees,
                "confidence": "static_one_hop",
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
            changed.add((repo_name, str(file)))
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
        "授权": ("authorize", "permission", "role"),
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
    }
    tokens: set[str] = set()
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", value):
        token = match.group(0).lower()
        tokens.add(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            tokens.update(token[index : index + 2] for index in range(len(token) - 1))
            tokens.update(token[index : index + 3] for index in range(len(token) - 2))
    for token in tuple(tokens):
        tokens.update(part.lower() for part in token.split("_") if len(part) >= 3)
    lowered = value.lower()
    for term, translated in aliases.items():
        if term in lowered:
            tokens.update(translated)
    return tokens


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
    inventory: dict[str, Any], code_evidence: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    repositories = [item for item in inventory.get("repositories", []) if isinstance(item, dict)]
    repository_file_limit = any(
        isinstance(repo.get("files"), list) and len(repo["files"]) > _MAX_EVIDENCE_FILES
        for repo in repositories
    )
    evidence_limit_reached = repository_file_limit or len(code_evidence or []) >= _MAX_EVIDENCE_ITEMS
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
    return {
        "complete": bool(repositories) and not incomplete and not evidence_limit_reached,
        "repositories": incomplete,
        "review_evidence_limited": evidence_limit_reached,
    }


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
        else:
            status = "undetermined"
        interface_ids = list(dict.fromkeys(
            value for result in claim_results for value in result["interface_ids"]
        ))
        evidence_refs = list(dict.fromkeys(
            value for result in claim_results for value in result["evidence_refs"]
        ))
        note_parts = []
        note_parts.extend(value for result in claim_results for value in result["unmet_conditions"])
        note_parts.extend(value for result in claim_results for value in result["verification_needed"])
        mapping: dict[str, Any] = {
            "requirement_id": parent_id,
            "status": status,
            "confidence": round(sum(float(item["confidence"]) for item in claim_results) / len(claim_results), 2) if claim_results else 0.0,
            "interface_ids": interface_ids,
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
            assessment["confidence"] = min(float(assessment.get("confidence", 0)), 0.45)
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
        assessment["confidence"] = min(float(assessment.get("confidence", 0)), 0.45)
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


def _apply_deterministic_gates(
    mappings: list[dict[str, Any]], checks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """确定性门槛优先于 LLM：缺直接证据的高风险行为不可标为已实现。"""
    checks_by_requirement: dict[str, list[dict[str, Any]]] = {}
    for check in checks:
        checks_by_requirement.setdefault(str(check["requirement_id"]), []).append(check)
    for mapping in mappings:
        required_checks = checks_by_requirement.get(str(mapping["requirement_id"]), [])
        cited = set((mapping.get("evidence") or {}).get("refs", []))
        uncovered = [
            check for check in required_checks
            if check["status"] == "needs_verification"
            or not (cited & set(check.get("evidence_ids", [])))
        ]
        if not uncovered or mapping["status"] != "implemented":
            continue
        mapping["status"] = "undetermined"
        evidence = mapping.setdefault("evidence", {})
        previous = str(evidence.get("note", "")).strip()
        categories = "、".join(str(check["category"]) for check in uncovered)
        gate_note = f"确定性证据门槛：未为高风险行为引用直接相关的代码/配置证据（{categories}），须人工核查"
        evidence["note"] = f"{previous}；{gate_note}" if previous else gate_note
        mapping["confidence"] = min(float(mapping.get("confidence", 0.0)), 0.45)
    return mappings


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
    """视图四（C-09）：变更接口 × 需求覆盖（程序化派生，不经 LLM）。

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
            file_map[str(file)] = "added"
        for file in files.get("modified") or []:
            file_map[str(file)] = "modified"
        changed_by_repo[name] = file_map
        for file in files.get("removed") or []:
            removed.append({"repo": name, "file": str(file)})

    if not changed_by_repo and not removed:
        return [], [], []

    # 映射表反向：interface_id → 覆盖它的 REQ 清单
    reqs_by_interface: dict[str, list[str]] = {}
    for entry in mappings:
        interface_ids = entry.get("interface_ids") or ([entry["interface_id"]] if entry.get("interface_id") else [])
        for if_id in interface_ids:
            reqs_by_interface.setdefault(str(if_id), []).append(str(entry["requirement_id"]))

    change_scope: list[dict[str, Any]] = []
    matched: set[tuple[str, str]] = set()  # 已被接口定义命中的 (repo, file)
    for interface in interfaces:
        location = interface.get("code_location") or {}
        repo_name = str(location.get("repo", ""))
        file = str(location.get("file", ""))
        repo_changes = changed_by_repo.get(repo_name)
        if repo_changes is None or file not in repo_changes:
            continue
        matched.add((repo_name, file))
        if_id = str(interface.get("id", ""))
        change_scope.append(
            {
                "interface_id": if_id,
                "repo": repo_name,
                "file": file,
                "change": repo_changes[file],
                "covered_by": reqs_by_interface.get(if_id, []),
            }
        )

    # 未命中接口定义的变更文件（业务逻辑层变更等——行为风险信号）
    unmatched: list[dict[str, Any]] = []
    for repo_name, file_map in changed_by_repo.items():
        for file, change in file_map.items():
            if (repo_name, file) not in matched:
                unmatched.append({"repo": repo_name, "file": file, "change": change})
    return change_scope, removed, unmatched


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
        verification = raw.get("verification_needed", [])
        if not isinstance(verification, list) or any(not isinstance(entry, str) for entry in verification):
            raise PipelineError(code=X_LLM_02, message="LLM 输出的 verification_needed 必须为字符串数组", node_id=NODE_NAME)
        if status in {"implemented", "partial", "missing"} and not used_evidence:
            # 接口 ID、路径线索或搜索未命中都不是行为证据。
            status = "undetermined"
            confidence = min(float(confidence), 0.45)
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
            confidence = min(float(confidence), 0.45)
            verification = list(verification)
            verification.append("结论缺少与状态相匹配的已满足/未满足条件说明，需结合源码证据复核。")
        if status == "undetermined" and not verification:
            verification = ["当前证据不足以判定该声明，请核对相关实现、配置或运行行为。"]
        assessments.append(
            {
                "claim_id": claim_id,
                "status": status,
                "confidence": float(confidence),
                "interface_ids": used_interfaces,
                "evidence_refs": used_evidence,
                "met_conditions": arrays["met_conditions"],
                "unmet_conditions": arrays["unmet_conditions"],
                "verification_needed": verification,
            }
        )

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

    通用 ``_strip_code_fence`` 只提取 JSON 对象，遇到模型错误地返回裸数组时会
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


def _derive_risks(
    mappings: list[dict[str, Any]], deterministic_checks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """视图二：部分实现及证据不足均进入风险清单（程序派生）。"""
    risks: list[dict[str, Any]] = []
    checks_by_req: dict[str, list[dict[str, Any]]] = {}
    for check in deterministic_checks:
        checks_by_req.setdefault(str(check["requirement_id"]), []).append(check)
    for entry in mappings:
        status = entry["status"]
        if status not in {"partial", "undetermined"}:
            continue
        is_undetermined = status == "undetermined"
        risk: dict[str, Any] = {
            "requirement_id": entry["requirement_id"],
            "kind": "evidence_gap" if is_undetermined else "partial_implementation",
            "severity": "P2" if is_undetermined else "P1",
            "action": (
                "补充服务端配置或实现证据后复核"
                if is_undetermined
                else "补齐实现与对应测试后复核"
            ),
            "description": (
                "证据不足（R-REVW-05）：高风险服务端行为待核查，不能按已实现关闭"
                if is_undetermined
                else "部分实现（R-REVW-04）：实现不完整即风险，须复核覆盖缺口"
            ),
        }
        if entry.get("interface_id"):
            risk["interface_id"] = entry["interface_id"]
        note = (entry.get("evidence") or {}).get("note")
        if note:
            risk["description"] += f"；LLM 判定依据：{note}"
        missing_categories = sorted({
            str(check["category"])
            for check in checks_by_req.get(str(entry["requirement_id"]), [])
            if check["status"] == "needs_verification"
        })
        if missing_categories:
            risk["description"] += f"；待核对证据：{'、'.join(missing_categories)}"
            critical = {"authorization", "password", "transaction", "data_consistency"} & set(missing_categories)
            if critical:
                risk["severity"] = "P1" if is_undetermined else "P0"
                if not is_undetermined:
                    risk["action"] = "阻断发布，补齐关键行为实现与安全/一致性回归测试"
            elif {"session", "state_transition", "idempotency"} & set(missing_categories):
                risk["severity"] = "P1"
        risks.append(risk)
    return risks


def _derive_missing(
    mappings: list[dict[str, Any]], requirements: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """视图三（R-REVW-02）：提取 missing 条目，title 从需求回填（程序派生）。"""
    title_by_id: dict[str, str] = {
        str(item["id"]): str(item.get("title", "")) for item in requirements if item.get("id")
    }
    missing: list[dict[str, Any]] = []
    for entry in mappings:
        if entry["status"] != "missing":
            continue
        missing.append(
            {
                "requirement_id": entry["requirement_id"],
                "title": title_by_id.get(entry["requirement_id"], ""),
            }
        )
    return missing
