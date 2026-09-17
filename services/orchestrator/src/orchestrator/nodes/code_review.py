"""code_review 节点：三视图产出（F-REVW-01~05）——一次 LLM 调用产映射表，
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
from orchestrator.nodes.req_parse import _strip_code_fence
from orchestrator.prompts_loader import PromptSpec, load_prompt

NODE_NAME = "code_review"

_VALID_STATUSES = ("implemented", "partial", "missing", "undetermined")

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
        behavior_evidence = _behavior_evidence(inventory)
        code_evidence = _collect_code_evidence(inventory)
        call_chains = _derive_call_chains(inventory, interfaces)
        incremental_focus = _collect_incremental_focus(inventory, interfaces)
        deterministic_checks = _derive_deterministic_checks(requirements, code_evidence)

        prompt = load_prompt(prompts_dir, NODE_NAME)
        # 以需求为独立审查单元分批；接口只发送映射所需的紧凑字段，避免源码上下文重复。
        interface_index = _compact_interfaces(interfaces)
        mappings: list[dict[str, Any]] = []
        # Review 同时携带接口和服务端行为证据，单批预算应明显小于普通 JSON 节点。
        requirement_batches = chunk_json_items(requirements, max_chars=5_000)
        for index, batch in enumerate(requirement_batches, start=1):
            _append_review_progress(
                store, state.job_id, index, len(requirement_batches), len(batch)
            )
            user_content = json.dumps(
                {
                    "batch": {"index": index, "total": len(requirement_batches)},
                    "requirements": batch,
                    "interfaces": interface_index,
                    "behavior_evidence": behavior_evidence,
                    "code_evidence": code_evidence,
                    "call_chains": call_chains,
                    "incremental_focus": incremental_focus,
                    "deterministic_checks": _checks_for_batch(deterministic_checks, batch),
                },
                ensure_ascii=False,
            )

            mappings.extend(
                await complete_with_json_repair(
                    llm,
                    NODE_NAME,
                    prompt,
                    user_content,
                    partial(_merge_llm_mappings, requirements=batch, interfaces=interfaces),
                )
            )

        mappings = _apply_deterministic_gates(mappings, deterministic_checks)
        _annotate_evidence_quality(mappings, deterministic_checks, call_chains)

        change_scope, removed_files, unmatched = _derive_change_scope(
            inventory, interfaces, mappings
        )
        incremental_findings = _derive_incremental_findings(
            change_scope, removed_files, unmatched, mappings
        )
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "job_id": state.job_id,
            "mappings": mappings,
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
        }
        for item in interfaces
    ]


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


def _collect_code_evidence(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    """抽取可引用的服务端行为证据片段（确定性、限量、脱敏）。

    code_inventory 只保存文件清单，因此此处仅从本次扫描所记录的仓库路径读取
    明确允许的源码/配置后缀。找不到文件、越出仓库根目录或无法解码均跳过；
    评审随后会如实输出“待核查”，而不是猜测实现存在。
    """
    evidence: list[dict[str, Any]] = []
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
        "cache": ("缓存", "cache", "redis"),
        "async": ("异步", "消息", "队列", "定时", "kafka", "rabbit"),
    }
    return {
        category
        for category, keywords in rules.items()
        if any(word in text for word in keywords)
    }


def _derive_deterministic_checks(
    requirements: list[dict[str, Any]], code_evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """需求级证据门槛检查：无直接证据只能“待核查”，不可判定已实现。"""
    present = {str(item.get("category", "")) for item in code_evidence}
    checks: list[dict[str, Any]] = []
    for requirement in requirements:
        req_id = str(requirement.get("id", ""))
        for category in sorted(_required_evidence_categories(requirement)):
            found = category in present
            checks.append(
                {
                    "requirement_id": req_id,
                    "rule_id": f"EVID-{category.upper()}",
                    "category": category,
                    "status": "evidence_found" if found else "needs_verification",
                    "description": (
                        f"已提取到 {category} 相关代码/配置片段，仍需结合需求语义核对"
                        if found
                        else f"需求涉及 {category} 行为，但扫描范围未提取到直接代码/配置证据"
                    ),
                }
            )
    return checks


def _checks_for_batch(
    checks: list[dict[str, Any]], requirements: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    req_ids = {str(item.get("id", "")) for item in requirements}
    return [check for check in checks if check["requirement_id"] in req_ids]


def _apply_deterministic_gates(
    mappings: list[dict[str, Any]], checks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """确定性门槛优先于 LLM：缺直接证据的高风险行为不可标为已实现。"""
    blocked = {
        str(check["requirement_id"])
        for check in checks
        if check["status"] == "needs_verification"
    }
    for mapping in mappings:
        if mapping["requirement_id"] not in blocked or mapping["status"] != "implemented":
            continue
        mapping["status"] = "undetermined"
        evidence = mapping.setdefault("evidence", {})
        previous = str(evidence.get("note", "")).strip()
        gate_note = "确定性证据门槛：未提取到该高风险服务端行为的直接代码/配置证据，须人工核查"
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
        if req_id in missing:
            mapping["evidence_quality"] = "insufficient"
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
        "batch": {"index": index, "total": total, "requirements": count},
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
        if_id = entry.get("interface_id")
        if if_id:
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
    status_by_interface = {
        str(item.get("interface_id")): str(item.get("status"))
        for item in mappings
        if item.get("interface_id")
    }
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
    requirements: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """LLM 映射输出合并：溯源字段回填 + 覆盖率与虚构校验。

    Raises:
        PipelineError: X-LLM-02——不可解析 / 覆盖率缺口 / 虚构或重复 REQ / 虚构 IF。
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
    if not isinstance(data, dict) or not isinstance(data.get("mappings"), list):
        raise PipelineError(
            code=X_LLM_02,
            message="LLM 输出缺少 mappings 数组（或结构不符）",
            node_id=NODE_NAME,
        )

    req_ids = {str(item.get("id", "")) for item in requirements}
    if_by_id = {str(item["id"]): item for item in interfaces if item.get("id")}

    covered: set[str] = set()
    mappings: list[dict[str, Any]] = []
    for raw in data["mappings"]:
        if not isinstance(raw, dict):
            raise PipelineError(
                code=X_LLM_02,
                message="LLM 输出的 mappings 含非对象条目",
                node_id=NODE_NAME,
            )
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

        if_id = raw.get("interface_id")
        interface = None
        if if_id is not None:
            if_id = str(if_id)
            interface = if_by_id.get(if_id)
            if interface is None:
                raise PipelineError(
                    code=X_LLM_02,
                    message=f"LLM 引用了不存在的接口：{if_id}（禁止虚构）",
                    node_id=NODE_NAME,
                )

        entry: dict[str, Any] = {
            "requirement_id": req_id,
            "status": status,
            "confidence": raw.get("confidence", 0.0),
        }
        if interface is not None:
            entry["interface_id"] = if_id
            # 证据位置从接口回填（LLM 提供的 code_location 一律忽略）
            evidence: dict[str, Any] = {"code_location": interface.get("code_location")}
            note = raw.get("evidence")
            if isinstance(note, dict) and note.get("note"):
                evidence["note"] = str(note["note"])
            entry["evidence"] = evidence
        elif isinstance(raw.get("evidence"), dict) and raw["evidence"].get("note"):
            entry["evidence"] = {"note": str(raw["evidence"]["note"])}
        mappings.append(entry)

    missing_coverage = sorted(req_ids - covered)
    if missing_coverage:
        raise PipelineError(
            code=X_LLM_02,
            message=f"映射覆盖率不足 100%（R-REVW-01），缺：{missing_coverage}",
            node_id=NODE_NAME,
        )
    if not mappings:
        raise PipelineError(
            code=X_LLM_02,
            message="LLM 输出的 mappings 为空（输入需求为零时不应到达本节点）",
            node_id=NODE_NAME,
        )
    # 需求条目 ID 存在性已隐含在 req_ids 判定中
    return mappings


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
        missing_categories = [
            str(check["category"])
            for check in checks_by_req.get(str(entry["requirement_id"]), [])
            if check["status"] == "needs_verification"
        ]
        if missing_categories:
            risk["description"] += f"；待核对证据：{'、'.join(missing_categories)}"
            critical = {"authorization", "password", "transaction"} & set(missing_categories)
            if critical:
                risk["severity"] = "P1" if is_undetermined else "P0"
                if not is_undetermined:
                    risk["action"] = "阻断发布，补齐关键行为实现与安全/一致性回归测试"
            elif "session" in missing_categories:
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
