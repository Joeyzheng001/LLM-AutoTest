"""interface_reconstruct 节点：R-REV-02 两步法——java-parser 静态定位 → LLM 语义解析。

产物两份（NODE_ARTIFACTS）：
- interface_candidates.json：静态候选落盘（D1 载体，乙的弹药如实入库）；
- interfaces.json：LLM 补全后的接口还原清单（D2 载体）。

诚实性铁律（数值/溯源不经 LLM）：
- interface 的 `id`（IF-<PROTO>-###）由代码按协议序分配；
- `protocol` / `code_location` / `is_fallback` / `confidence` 从候选**回填**
  （LLM 产出此类字段一律忽略）；
- LLM 条目必须引用真实存在的 candidate_id，虚构即 X-LLM-02
  （不重试，人工介入）。

失败口径（docs/12）：
- **未圈定变更范围 → X-ORCH-01**（区分「配置缺失」与「扫描产物丢失」两种成因，
  前者补配置、后者查 code_scan；不再回退全量还原）；
- 变更范围内无接口候选 → X-REV-04；<5 候选 → warning 如实记录（X-REV-04 弱口径）；
- LLM 输出不可解析/引用虚构候选/未过 Schema → X-LLM-02；
- java-parser 通信失败 → X-ORCH-03；契约错误 → 透传。

变更范围口径（C-09 扩展）：接口候选 = **本次代码变更涉及的接口**，此阶段不涉及需求
映射。每个仓库都必须配基线分支或提交号；`code_inventory.repositories[].changed_files`
键存在才算已配（配了但本次无变更时为空集，属合法）。
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
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
from common.prompts_loader import PromptSpec, load_prompt

from orchestrator.javaparser_client import JavaParserClient, RepositoryInput
from orchestrator.python_scan import scan_repository

NODE_NAME = "interface_reconstruct"

# 横切数据约定：CAND-### / IF-<PROTO>-###
_CAND_ID_RE = re.compile(r"^CAND-\d{3}$")
_PROTOCOL_PREFIX = {
    "rest": "REST",
    "grpc": "GRPC",
    "graphql": "GRAPHQL",
    "soap": "SOAP",
    "mq": "MQ",
}
_MIN_CANDIDATES = 5  # X-REV-04 弱口径：低于此数提示换兜底仓库

# 行级变更判定等级（#52）。各级含义见 java-parser change_evidence：
# body=接口本体方法被改；callee=调用链上的代码被改；file=本体文件被改但改动行不在
# 任何跨度内；related_file=只有关联文件被改；none=本次变更没碰到。
_EVIDENCE_LEVELS = ("body", "callee", "file", "related_file", "none")


class SupportsComplete(Protocol):
    """LLM 客户端协议（生产为 LlmClient，测试可注入桩）。"""

    async def complete(self, node: str, prompt: PromptSpec, user_content: str) -> LlmResult:
        """按协议执行一次补全。"""
        ...


async def run_interface_reconstruct(
    engine: Engine,
    state: JobState,
    store: ArtifactStore,
    client: JavaParserClient,
    llm: SupportsComplete,
    prompts_dir: Path,
    repositories: list[RepositoryInput],
    protocols: list[str] | None = None,
) -> Path:
    """执行 interface_reconstruct 全流程。

    Returns:
        interfaces.json 产物路径。

    Raises:
        PipelineError: 各失败口径见模块 docstring。
    """
    engine.check_runnable(state, NODE_NAME)
    engine.mark_running(state, NODE_NAME)
    try:
        # 变更范围是所有仓库的必填项：缺了就只能按全量还原，那不是本次任务的范围。
        # 先读（后面给 java-parser 带 target_sha / changed_lines 也要用），不静默退化全量。
        inventory = _load_code_inventory(store, state.job_id)
        unscoped = _repos_without_change_scope(inventory)
        if unscoped:
            message, details = _unscoped_report(store, state.job_id, unscoped)
            raise PipelineError(
                code=X_ORCH_01,
                message=message,
                node_id=NODE_NAME,
                details=details,
            )
        changed_files = _collect_changed_files(inventory)
        if not changed_files:
            raise PipelineError(
                code=X_ORCH_01,
                message="code_inventory.json 缺少可用的变更范围（changed_files 为空）",
                node_id=NODE_NAME,
            )

        # C-13 语言分流：Java 候选走 java-parser Extract；Python 走内置 AST 扫描。
        # #50：Java 仓库必须带上 target_sha（快照版本）与 changed_lines（行级判定依据）。
        java_repos = [
            _with_change_scope(repo, inventory)
            for repo in repositories
            if repo.language != "python"
        ]
        python_repos = [
            _with_change_scope(repo, inventory)
            for repo in repositories
            if repo.language == "python"
        ]
        raw_candidates: list[Any] = []
        raw_warnings: list[str] = []
        stats: dict[str, Any] = {}
        capabilities: dict[str, Any] = {}
        if java_repos:
            response = await client.extract(state.job_id, java_repos, protocols)
            raw_candidates.extend(response.get("candidates") or [])
            raw_warnings.extend(
                str(item) for item in response.get("warnings") or [] if isinstance(item, str)
            )
            stats = response.get("stats") if isinstance(response.get("stats"), dict) else {}
            capabilities = (
                response.get("capabilities")
                if isinstance(response.get("capabilities"), dict)
                else {}
            )
        python_stats: list[dict[str, Any]] = []
        for repo in python_repos:
            python_result = scan_repository(repo)
            raw_candidates.extend(python_result.candidates)
            raw_warnings.extend(python_result.warnings)
            python_stats.append(
                {
                    "name": repo.name,
                    "status": python_result.status,
                    "files_discovered": python_result.files_discovered,
                    "files_parsed": python_result.files_parsed,
                    "files_failed": python_result.files_failed,
                    "candidates": len(python_result.candidates),
                }
            )
        if python_stats:
            stats = dict(stats)
            java_stats = [
                item for item in stats.get("repositories", []) if isinstance(item, dict)
            ]
            all_stats = java_stats + python_stats
            stats["repositories"] = all_stats
            totals = {
                field: sum(int(item.get(field) or 0) for item in all_stats)
                for field in ("files_discovered", "files_parsed", "files_failed", "candidates")
            }
            stats["totals"] = totals
        candidates, warnings = _normalize_candidates(
            {"candidates": raw_candidates, "warnings": raw_warnings}
        )

        # 完整性闭环门禁（P0①）：扫描是否"看全了"必须可判定，否则"没发现接口"不能成立。
        integrity = _scan_integrity(
            inventory,
            stats,
            capabilities,
            len(candidates),
            expected_repo_names=[repo.name for repo in repositories],
        )
        if not integrity["complete"]:
            raise PipelineError(
                code=X_ORCH_01,
                message="接口扫描不完整（禁止据此判定「未发现接口」）："
                + "；".join(integrity["reasons"]),
                node_id=NODE_NAME,
                details={
                    "error_repos": integrity["error_repos"],
                    "degraded_repos": integrity["degraded_repos"],
                    "missing_repos": integrity["missing_repos"],
                    "unexpected_repos": integrity["unexpected_repos"],
                    "scan_integrity": integrity,
                },
            )

        failed_files = _parse_failed_files(stats)
        scoped = _filter_candidates_by_scope(
            candidates, changed_files, parse_failed_files=failed_files
        )
        if not scoped:
            raise PipelineError(
                code="X-REV-04",
                message="变更范围内无接口候选：变更文件里没有可还原的接口",
                node_id=NODE_NAME,
            )
        warnings.append(
            f"按变更范围过滤：{len(candidates)} 个候选 → {len(scoped)} 个（来自变更文件）"
        )
        candidates = scoped

        if not candidates:
            raise PipelineError(
                code="X-REV-04",
                message="接口候选为零（低于可运行底线，建议检查仓库或换用兜底仓库）",
                node_id=NODE_NAME,
            )
        if len(candidates) < _MIN_CANDIDATES:
            warnings.append(
                f"X-REV-04：候选数 {len(candidates)} < {_MIN_CANDIDATES}，建议换用兜底仓库"
            )

        # 第①步产物：静态候选如实入库（D1 载体）。stats/capabilities 一并落盘：
        # 下游（code_review）要靠它区分"没查到"与"没查全"。
        store.write(
            state.job_id,
            "interface_candidates.json",
            {
                "schema_version": "1.0",
                "job_id": state.job_id,
                "candidates": candidates,
                "warnings": warnings,
                "stats": stats,
                "capabilities": capabilities,
            },
        )
        # 变动接口 + 变动代码的行级口径落盘（#52）：base/target、变更行、逐接口命中证据、
        # unresolved 清单。这是"精确 diff 出变动接口和代码"的机器可读答案。
        store.write(
            state.job_id,
            "change_set.json",
            _build_change_set(state.job_id, inventory, candidates, stats, integrity),
        )

        # 第②步：LLM 语义解析（候选含 snippet 上下文）。候选多时分批，
        # 每批 JSON 独立且可校验，避免单次返回过长被模型截断。
        prompt = load_prompt(prompts_dir, NODE_NAME)
        interfaces: list[dict[str, Any]] = []
        candidate_batches = chunk_json_items(candidates)
        for index, batch in enumerate(candidate_batches, start=1):
            user_content = json.dumps(
                {
                    "batch": {"index": index, "total": len(candidate_batches)},
                    "candidates": batch,
                },
                ensure_ascii=False,
            )
            interfaces.extend(
                await complete_with_json_repair(
                    llm,
                    NODE_NAME,
                    prompt,
                    user_content,
                    partial(_merge_llm_output, candidates=batch),
                )
            )
        interfaces = _renumber_interfaces(interfaces)

        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "job_id": state.job_id,
            "interfaces": interfaces,
        }
        try:
            path = store.write(state.job_id, "interfaces.json", payload)
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


def _load_code_inventory(store: ArtifactStore, job_id: str) -> dict[str, Any]:
    """读 code_inventory.json（C-09 变更范围来源）；缺失返回空（全量）。"""
    try:
        return dict(store.read_json(job_id, "code_inventory.json"))
    except FileNotFoundError:
        return {}


def _collect_changed_files(inventory: dict[str, Any]) -> dict[str, set[str]]:
    """从 code_inventory 提取变更文件（added + modified），按 repo 分组。

    只登记「配了变更范围」的仓库（code_inventory 里存在 changed_files 键）；配了
    但本次无变更的仓库登记为**空集**，仍算已配。是否有仓库没配范围由
    `_repos_without_change_scope` 判定——变更范围必填，不再回退全量。
    """
    changed: dict[str, set[str]] = {}
    for repo in inventory.get("repositories") or []:
        if not isinstance(repo, dict):
            continue
        repo_name = str(repo.get("name", ""))
        changed_files = repo.get("changed_files")
        if not isinstance(changed_files, dict):
            continue
        paths = {
            value
            for key in ("added", "modified")
            for value in (changed_files.get(key) or [])
            if isinstance(value, str) and value
        }
        # 「键存在但无变更」与「键缺失＝未配范围」是两件事，前者合法、后者必须拦。
        # 用 `if paths:` 会把两者压成同一个空值，从而丢掉「已配」这个事实。
        changed[repo_name] = paths
    return changed


def _repos_without_change_scope(inventory: dict[str, Any]) -> list[str]:
    """返回未配置变更范围的仓库名（code_inventory 里没有 changed_files 键）。

    单独成一个函数是为了和 `_collect_changed_files` 的「已配但空集」区分开：
    未配范围属于配置缺失，必须让调用方拒绝推进，而不是静默按全量还原。
    """
    missing: list[str] = []
    for repo in inventory.get("repositories") or []:
        if not isinstance(repo, dict):
            continue
        name = str(repo.get("name", ""))
        if name and not isinstance(repo.get("changed_files"), dict):
            missing.append(name)
    return missing


def _declared_scope_repos(store: ArtifactStore, job_id: str) -> dict[str, bool]:
    """从任务配置读出各仓库是否**声明**了变更范围（基线分支或提交号）。

    读不到配置时返回空 dict——宁可退回「成因未知」的笼统报错，也不猜。
    """
    try:
        payload = json.loads(
            (store.job_dir(job_id) / "job_input.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    declared: dict[str, bool] = {}
    for repo in payload.get("repos") or []:
        if not isinstance(repo, dict):
            continue
        name = str(repo.get("name", ""))
        if not name:
            continue
        baseline = str(repo.get("baseline_branch") or "").strip()
        commits = repo.get("commits")
        if isinstance(commits, (list, tuple)):
            typed_commits = any(str(item).strip() for item in commits)
        else:
            typed_commits = bool(str(commits or "").strip())
        legacy_commit = str(repo.get("commit") or "").strip()
        declared[name] = bool(baseline or typed_commits or legacy_commit)
    return declared


def _unscoped_report(
    store: ArtifactStore, job_id: str, unscoped: list[str]
) -> tuple[str, dict[str, Any]]:
    """生成「无法圈定变更范围」的报错文案与明细，**分清成因**。

    `code_inventory` 缺 `changed_files` 有两种成因，混成一句会指错排查方向：
    ① 任务配置里根本没指定变更范围 → 用户补配置即可；
    ② 配置里指定了、但扫描产物没生成变更清单 → 问题在 code_scan 环节。实测踩过一次：
       java-parser 的 job 幂等缓存命中了上一次结果，重跑拿到的是旧扫描产物，
       此时补配置完全无用，必须先重启 java-parser（或换 job_id）。
    """
    declared = _declared_scope_repos(store, job_id)
    missing_config: list[str] = []
    lost_in_scan: list[str] = []
    if not declared:
        message = (
            "无法圈定本次改动的接口：" + "、".join(unscoped)
            + "（可能是任务配置未指定基线分支/提交号，也可能是 code_scan 未生成变更清单）"
        )
        return message, {
            "repos_without_scope": unscoped,
            "repos_missing_config": missing_config,
            "repos_lost_in_scan": lost_in_scan,
        }
    missing_config = [name for name in unscoped if not declared.get(name)]
    lost_in_scan = [name for name in unscoped if declared.get(name)]
    parts: list[str] = []
    if missing_config:
        parts.append(
            "未在任务配置里指定变更范围（请给基线分支或提交号）："
            + "、".join(missing_config)
        )
    if lost_in_scan:
        parts.append(
            "已配置变更范围但扫描产物没有变更清单，问题在 code_scan 环节"
            "（常见于 java-parser 的 job 幂等缓存命中上一次结果，需重启该服务）："
            + "、".join(lost_in_scan)
        )
    return "无法圈定本次改动的接口：" + "；".join(parts), {
        "repos_without_scope": unscoped,
        "repos_missing_config": missing_config,
        "repos_lost_in_scan": lost_in_scan,
    }


def _filter_candidates_by_scope(
    candidates: list[dict[str, Any]],
    changed_files: dict[str, set[str]],
    *,
    parse_failed_files: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """按变更范围过滤候选：保留「本次变更确实碰到」的候选。

    判定优先级（**行级优先**，#52）：

    1. 候选带 `change_evidence.level` 且 level ≠ none → 保留（等级本身记录了精度：
       body > callee > file > related_file）；
    2. level == none 但该仓库有解析失败的文件 → **保守扩大**保留并标注
       （评审 §3：解析不完整时不能断言"没变"；扩大必须可计数、有标记）；
    3. 没有 `change_evidence`（未圈定变更范围，或旧版 java-parser）→ 退回文件级口径：
       本人文件被改，**或**任一 `related_files[].file` 被改。

    `call_graph_incomplete` / `unresolved` 只表示调用链分析能力边界，不能作为“整仓变更”
    的理由；只有未解析目标本身带有且命中变更文件时才扩大保留。否则一个仓库只改一处
    Service，就会把所有含一跳调用的 Controller 全部算成变动接口。

    未配变更范围的仓库不会走到这里——上游 `_repos_without_change_scope` 已直接拦下。
    removed 文件里的接口已删除，也不在还原范围（`_collect_changed_files` 只收 added + modified）。
    """
    failed = parse_failed_files or {}
    kept: list[dict[str, Any]] = []
    for cand in candidates:
        loc = cand.get("code_location") or {}
        repo = str(loc.get("repo", ""))
        scope = changed_files.get(repo)
        if not scope:
            continue
        evidence = cand.get("change_evidence")
        in_scope = str(loc.get("file", "")) in scope or _touches_changed_file(cand, scope)
        unresolved_in_scope = _touches_unresolved_file(cand, scope)
        # 未解析调用只有在它记录了具体目标文件、且目标文件确实变更时才扩大纳入。
        # 复杂接收者/外部依赖没有可比对的文件，不能因为仓库有任意改动就整仓放大。
        if unresolved_in_scope and not in_scope:
            cand["conservative_expansion"] = {
                "reason": (
                    "未解析调用的目标文件命中本次变更，无法进一步精确定位调用行，"
                    "保守纳入该接口"
                )
            }
            kept.append(cand)
            continue
        if isinstance(evidence, dict):
            level = str(evidence.get("level", ""))
            if level in _EVIDENCE_LEVELS and level != "none":
                kept.append(cand)
                continue
            if level == "none" and in_scope and failed.get(repo):
                # 保守扩大：该仓库有文件没解析成，而本接口的文件/依赖确实在变更清单里
                # → 行级判定可能因为"没读到"而失真，不能据此丢掉。扩大必须带标记、可计数。
                cand["conservative_expansion"] = {
                    "reason": (
                        f"仓库 {repo} 有 {failed[repo]} 个源文件解析失败，"
                        "无法判定该接口是否受影响，保守纳入"
                    )
                }
                kept.append(cand)
            continue
        if in_scope:
            kept.append(cand)
    return kept


def _with_change_scope(repo: RepositoryInput, inventory: dict[str, Any]) -> RepositoryInput:
    """给仓库补上 target_sha 与 changed_lines（#50/#52）。

    找不到同名仓库时**不带**（java-parser 会如实标 degraded，由完整性门禁拦下）——
    绝不猜一个版本给下游，否则行号与 diff 不同版本会静默算错。
    """
    for entry in inventory.get("repositories") or []:
        if not isinstance(entry, dict) or str(entry.get("name", "")) != repo.name:
            continue
        changed_lines = entry.get("changed_lines")
        return replace(
            repo,
            target_sha=_text_or_none(entry.get("target_sha")),
            changed_lines=changed_lines if isinstance(changed_lines, dict) else {},
        )
    return repo


def _text_or_none(value: Any) -> str | None:
    """空串 / None 统一成 None（"没给"与"给了空值"是同一件事）。"""
    text = str(value).strip() if value is not None else ""
    return text or None


def _parse_failed_files(stats: dict[str, Any]) -> dict[str, int]:
    """仓库名 → 解析失败文件数（>0 说明该仓库"没看全"，需要保守扩大）。"""
    failed: dict[str, int] = {}
    for entry in stats.get("repositories") or []:
        if not isinstance(entry, dict):
            continue
        count = int(entry.get("files_failed") or 0)
        if count > 0:
            failed[str(entry.get("name", ""))] = count
    return failed


def _scan_integrity(
    inventory: dict[str, Any],
    stats: dict[str, Any],
    capabilities: dict[str, Any],
    candidate_count: int,
    expected_repo_names: list[str] | None = None,
) -> dict[str, Any]:
    """完整性闭环校验（P0①）：发现文件数 == 解析成功数 + 失败数，且与 code_inventory 一致。

    这是「零漏扫」里**唯一可判定**的部分：静态分析判不了运行时可达性，但"我到底读了几个
    文件、几个没读成"可以断言。任一项不闭合 → complete=False，下游不许据此说"未发现接口"。

    仓库级 error / degraded 单独列出（调用方据此硬拦）：那是"根本没扫成"，不是"扫了没命中"。
    """
    inventory_entries = [
        repo for repo in inventory.get("repositories") or [] if isinstance(repo, dict)
    ]
    inventory_counts = {
        str(repo.get("name", "")): repo.get("file_count")
        for repo in inventory_entries
    }
    expected_provided = expected_repo_names is not None
    expected = {str(name) for name in (expected_repo_names or []) if str(name)}
    entries = [entry for entry in stats.get("repositories") or [] if isinstance(entry, dict)]
    actual = [str(entry.get("name", "")) for entry in entries]
    actual_set = set(actual)
    reasons: list[str] = []
    error_repos: list[str] = []
    degraded_repos: list[str] = []
    snapshot_repos: list[str] = []
    missing_repos = sorted(expected - actual_set)
    unexpected_repos = sorted(actual_set - expected) if expected_provided else []
    duplicate_repos = sorted({name for name in actual if actual.count(name) > 1 and name})
    if missing_repos:
        reasons.append("java-parser 缺少仓库统计：" + "、".join(missing_repos))
    if unexpected_repos:
        reasons.append("java-parser 返回未请求的仓库统计：" + "、".join(unexpected_repos))
    if duplicate_repos:
        reasons.append("java-parser 返回重复仓库统计：" + "、".join(duplicate_repos))
    if expected_provided and expected and not entries:
        reasons.append("java-parser 未返回任何仓库统计，无法证明源码扫描完整")
    for entry in entries:
        name = str(entry.get("name", ""))
        status = str(entry.get("status", ""))
        if status == "error":
            error = entry.get("error") if isinstance(entry.get("error"), dict) else {}
            error_repos.append(name)
            reasons.append(
                f"仓库 {name} 候选提取失败（{error.get('code', '未知')}）："
                f"{error.get('message', '无详情')}"
            )
            continue
        if status == "degraded":
            degraded_repos.append(name)
            reasons.append(
                f"仓库 {name} 未按 target 提交解析候选"
                f"（{entry.get('degraded_reason', '未知原因')}）：行号与变更行区间可能不同版本"
            )
            continue
        if status != "ok":
            reasons.append(f"仓库 {name} 返回未知扫描状态：{status or '空值'}")
            continue
        snapshot_repos.append(name)
        discovered = int(entry.get("files_discovered") or 0)
        parsed = int(entry.get("files_parsed") or 0)
        failed = int(entry.get("files_failed") or 0)
        if parsed + failed != discovered:
            reasons.append(
                f"仓库 {name} 文件计数不守恒：发现 {discovered}，"
                f"解析成功 {parsed} + 失败 {failed}"
            )
        if failed > 0:
            reasons.append(
                f"仓库 {name} 有 {failed} 个源文件解析失败，接口集合可能不完整"
            )
        recorded = inventory_counts.get(name)
        if not isinstance(recorded, int) or isinstance(recorded, bool):
            reasons.append(f"仓库 {name} code_inventory 缺少有效 file_count，无法闭合扫描范围")
        elif recorded != discovered:
            reasons.append(
                f"仓库 {name} 提取阶段发现 {discovered} 个 .java 文件，"
                f"code_inventory 记录 {recorded} 个（扫描范围不一致，接口可能成批缺失）"
            )
    totals = stats.get("totals") if isinstance(stats.get("totals"), dict) else {}
    for field in ("files_discovered", "files_parsed", "files_failed"):
        total = int(totals.get(field) or 0)
        summed = sum(int(entry.get(field) or 0) for entry in entries)
        if total != summed:
            reasons.append(
                f"stats.totals.{field} 不守恒：总计 {total}，仓库明细合计 {summed}"
            )
    unsupported = [str(item) for item in (capabilities.get("unsupported") or [])]
    if unsupported:
        reasons.append(
            f"请求的协议未接入静态识别：{'、'.join(unsupported)}——这些协议的接口整体缺失"
        )
    if not stats and candidate_count and not expected:
        # 有候选却没有 stats：无法校验完整性（旧版 java-parser）。如实标不完整，不假装通过。
        reasons.append("java-parser 未返回 stats，无法校验扫描完整性（请升级该服务）")
    return {
        "complete": not reasons,
        "reasons": reasons,
        "snapshot_repos": snapshot_repos,
        "degraded_repos": degraded_repos,
        "error_repos": error_repos,
        "missing_repos": missing_repos,
        "unexpected_repos": unexpected_repos,
        "unsupported_protocols": unsupported,
        "files_discovered": int((stats.get("totals") or {}).get("files_discovered") or 0),
        "files_parsed": int((stats.get("totals") or {}).get("files_parsed") or 0),
        "files_failed": int((stats.get("totals") or {}).get("files_failed") or 0),
        "candidates": candidate_count,
    }


def _build_change_set(
    job_id: str,
    inventory: dict[str, Any],
    candidates: list[dict[str, Any]],
    stats: dict[str, Any],
    integrity: dict[str, Any],
) -> dict[str, Any]:
    """变动接口 + 变动代码的行级口径（#52）落盘：机器可读的"到底改了什么"。

    每个接口记录命中等级、命中跨度与重叠的变更行区间、实际变动代码片段；无法静态定位的
    调用单独列 unresolved。计数让"不漏"可被核对（有多少条是保守纳入的、多少条没定位到）。
    """
    level_counts: dict[str, int] = dict.fromkeys(_EVIDENCE_LEVELS, 0)
    conservative = 0
    entries: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for cand in candidates:
        location = cand.get("code_location") or {}
        raw = cand.get("change_evidence")
        evidence = raw if isinstance(raw, dict) else {}
        level = str(evidence.get("level") or "unknown")
        level_counts[level] = level_counts.get(level, 0) + 1
        entry: dict[str, Any] = {
            "candidate_id": str(cand.get("candidate_id", "")),
            "repo": str(location.get("repo", "")),
            "file": str(location.get("file", "")),
            "signature": str(cand.get("signature", "")),
            "level": level,
            "hits": evidence.get("hits") or [],
            "sources": evidence.get("sources") or [],
            "changed_snippets": evidence.get("changed_snippets") or [],
            "code_spans": cand.get("code_spans") or [],
        }
        if cand.get("conservative_expansion"):
            entry["conservative_expansion"] = cand["conservative_expansion"]
            conservative += 1
        entries.append(entry)
        for item in cand.get("unresolved") or []:
            if isinstance(item, dict):
                unresolved.append({"candidate_id": entry["candidate_id"], **item})
    repositories: list[dict[str, Any]] = []
    removed_files: list[dict[str, str]] = []
    for repo in inventory.get("repositories") or []:
        if not isinstance(repo, dict):
            continue
        changed = repo.get("changed_files") if isinstance(repo.get("changed_files"), dict) else {}
        for file in changed.get("removed") or []:
            removed_files.append({"repo": str(repo.get("name", "")), "file": str(file)})
        repositories.append(
            {
                "name": str(repo.get("name", "")),
                "base_ref": repo.get("base_ref"),
                "target_ref": repo.get("target_ref"),
                "base_sha": repo.get("base_sha"),
                "target_sha": repo.get("target_sha"),
                "changed_files": changed,
                "changed_file_count": sum(
                    len(changed.get(key) or []) for key in ("added", "modified", "removed")
                ),
                "changed_lines": repo.get("changed_lines") or {},
                "source_path": next(
                    (
                        item.get("snapshot_path") or item.get("workspace")
                        for item in stats.get("repositories") or []
                        if isinstance(item, dict)
                        and str(item.get("name", "")) == str(repo.get("name", ""))
                    ),
                    None,
                ),
            }
        )
    counts = {
        "candidates": len(candidates),
        **{level: level_counts.get(level, 0) for level in _EVIDENCE_LEVELS},
        "unknown": level_counts.get("unknown", 0),
        "conservative_expansion": conservative,
        "unresolved": len(unresolved),
        "files_failed": int((stats.get("totals") or {}).get("files_failed") or 0),
    }
    return {
        "schema_version": "1.0",
        "job_id": job_id,
        "repositories": repositories,
        "removed_files": removed_files,
        "counts": counts,
        "interfaces": entries,
        "unresolved": unresolved,
        "scan_integrity": integrity,
    }


def _touches_changed_file(candidate: dict[str, Any], scope: set[str]) -> bool:
    """该候选的关联文件是否落在变更清单里（R-REV-04② 的行为级判定）。

    只比文件名（`related_files[].file` 与 `changed_files` 同为仓库内相对路径）；
    `role` 不参与判定——Service/DTO/Entity/Enum 任一被改都算该接口变动。
    """
    related = candidate.get("related_files")
    if not isinstance(related, list):
        return False
    for item in related:
        if isinstance(item, dict) and str(item.get("file", "")) in scope:
            return True
    return False


def _touches_unresolved_file(candidate: dict[str, Any], scope: set[str]) -> bool:
    """未解析调用是否带有命中变更范围的目标文件。"""
    for item in candidate.get("unresolved") or []:
        if isinstance(item, dict) and str(item.get("file", "")) in scope:
            return True
    return False


def _normalize_candidates(response: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """ExtractJob 响应 → (规范化候选列表, 告警清单)。

    候选 ID 规范化：契约只约束 string，产物 Schema 要求 CAND-###；
    缺失/不合规时按顺序重编（数据由代码定，不经 LLM）。同一源码位置、方法和路径
    的重复展开只保留一条，避免类级路径数组或重复注解把候选数放大。
    """
    raw = response.get("candidates")
    if not isinstance(raw, list):
        raise PipelineError(
            code=X_ORCH_01,
            message="java-parser 响应缺 candidates 数组（契约不符，宁停不猜）",
            node_id=NODE_NAME,
        )
    warnings = [str(item) for item in response.get("warnings") or [] if isinstance(item, str)]

    candidates: list[dict[str, Any]] = []
    seen_locations: set[tuple[str, str, str, str, str, str]] = set()
    duplicate_count = 0
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue  # 契约条目级异常：跳过并记告警，不中断整体（X-REV-01 口径）
        entry = dict(item)
        if not _CAND_ID_RE.match(str(entry.get("candidate_id", ""))):
            entry["candidate_id"] = f"CAND-{index:03d}"
        location = entry.get("code_location")
        signature = entry.get("signature")
        if isinstance(location, dict):
            signature_key = json.dumps(signature, ensure_ascii=False, sort_keys=True)
            key = (
                str(entry.get("protocol", "")),
                str(location.get("repo", "")),
                str(location.get("file", "")),
                str(location.get("start_line", "")),
                str(location.get("end_line", "")),
                signature_key,
            )
            if key in seen_locations:
                duplicate_count += 1
                continue
            seen_locations.add(key)
        candidates.append(entry)
    if duplicate_count:
        warnings.append(f"X-REV-01：去除 {duplicate_count} 条重复接口候选（同源码位置/方法/路径）")
    return candidates, warnings


def _merge_llm_output(text: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """LLM 输出与候选合并：语义字段取 LLM，溯源字段代码回填。

    Raises:
        PipelineError: X-LLM-02——输出不可解析 / 引用虚构或重复候选。
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
    if not isinstance(data, dict) or not isinstance(data.get("interfaces"), list):
        raise PipelineError(
            code=X_LLM_02,
            message="LLM 输出缺少 interfaces 数组（或结构不符）",
            node_id=NODE_NAME,
        )

    by_id = {str(cand["candidate_id"]): cand for cand in candidates}
    used: set[str] = set()
    counter: dict[str, int] = {}
    interfaces: list[dict[str, Any]] = []
    for item in data["interfaces"]:
        if not isinstance(item, dict):
            raise PipelineError(
                code=X_LLM_02,
                message="LLM 输出的 interfaces 含非对象条目",
                node_id=NODE_NAME,
            )
        cand_id = str(item.get("candidate_id", ""))
        cand = by_id.get(cand_id)
        if cand is None:
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 引用了不存在的候选：{cand_id}（禁止虚构）",
                node_id=NODE_NAME,
            )
        if cand_id in used:
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 对同一候选输出多个条目：{cand_id}",
                node_id=NODE_NAME,
            )
        used.add(cand_id)

        protocol = str(cand.get("protocol", "rest"))
        seq = counter.get(protocol, 0) + 1
        counter[protocol] = seq
        prefix = _PROTOCOL_PREFIX.get(protocol, protocol.upper())

        entry: dict[str, Any] = {
            "id": f"IF-{prefix}-{seq:03d}",
            "candidate_id": cand_id,
            "protocol": protocol,
            "signature": item.get("signature") or {},
            "code_location": cand.get("code_location"),
            "is_fallback": bool(cand.get("is_fallback", False)),
        }
        enriched_params = _enrich_params_from_candidate(item.get("params"), cand)
        if enriched_params is not None:
            entry["params"] = enriched_params
        if item.get("returns") is not None:
            entry["returns"] = item["returns"]
        if cand.get("confidence") is not None:
            entry["confidence"] = cand["confidence"]
        # 溯源/判定字段一律由代码回填（评审 §5）：related_files / code_spans /
        # change_evidence / unresolved 必须一路带到 interfaces.json。否则下游
        # `_derive_change_scope` 只看 code_location.file，Service/DTO 改动会被记成
        # unmatched_changed_files，变更接口与需求的关联也就断了。
        for key in (
            "related_files",
            "code_spans",
            "change_evidence",
            "unresolved",
            "conservative_expansion",
            "parameter_evidence",
            "method_body_truncated",
            "method_body_char_count",
            "method_body_context",
            "call_graph",
            "call_graph_incomplete",
        ):
            if cand.get(key) is not None:
                entry[key] = cand[key]
        interfaces.append(entry)
    missing = sorted(set(by_id) - used)
    if missing:
        raise PipelineError(
            code=X_LLM_02,
            message=f"LLM 未覆盖当前批候选：{missing[:10]}",
            node_id=NODE_NAME,
        )
    if not interfaces:
        raise PipelineError(
            code=X_LLM_02,
            message="LLM 输出的 interfaces 为空（候选均未产出条目）",
            node_id=NODE_NAME,
        )
    return interfaces


def _enrich_params_from_candidate(
    params: Any, candidate: dict[str, Any]
) -> list[dict[str, Any]] | Any:
    """用静态候选证据补齐 LLM 漏掉的 body 字段，避免参数退化为笼统 param。"""
    if params is not None and not isinstance(params, list):
        return params
    result = [dict(item) for item in (params or []) if isinstance(item, dict)]
    evidence_by_name = {
        str(item.get("name", "")): item
        for item in candidate.get("parameter_evidence") or []
        if isinstance(item, dict) and item.get("name")
    }
    for param in result:
        evidence = evidence_by_name.get(str(param.get("name", "")))
        if evidence is None:
            continue
        if "required" in evidence:
            param["required"] = bool(evidence["required"])
        if evidence.get("required_evidence"):
            param["required_evidence"] = evidence["required_evidence"]
    body_evidence = [
        item
        for item in candidate.get("parameter_evidence") or []
        if isinstance(item, dict) and item.get("in") == "body"
    ]
    request_body = [
        item for item in candidate.get("request_body") or [] if isinstance(item, dict)
    ]
    if not body_evidence and not request_body:
        return params
    body = next(
        (
            item
            for item in result
            if item.get("in") == "body"
            or any(str(item.get("name", "")) == str(e.get("name", "")) for e in body_evidence)
        ),
        None,
    )
    if body is None and body_evidence:
        evidence = body_evidence[0]
        body = {
            "name": evidence.get("name", "param"),
            "type": evidence.get("type", "unknown"),
            "required": bool(evidence.get("required", True)),
            "in": "body",
        }
        if evidence.get("required_evidence"):
            body["required_evidence"] = evidence["required_evidence"]
        result.append(body)
    if body is None:
        return result
    body.setdefault("in", "body")
    if body_evidence:
        evidence = next(
            (item for item in body_evidence if str(item.get("name", "")) == str(body.get("name", ""))),
            body_evidence[0],
        )
        if "required" in evidence:
            body["required"] = bool(evidence["required"])
        if evidence.get("required_evidence"):
            body["required_evidence"] = evidence["required_evidence"]
    existing = {
        str(field.get("name")): field
        for field in body.get("fields") or []
        if isinstance(field, dict) and field.get("name")
    }
    for field in request_body:
        name = str(field.get("name", ""))
        if not name:
            continue
        if name not in existing:
            existing[name] = dict(field)
            continue
        for key in ("type", "description", "required", "required_evidence", "source_refs"):
            if key in field:
                existing[name][key] = field[key]
        if "required" not in field:
            # 静态侧未找到 required 证据时，清掉 LLM 擅自给出的布尔结论，回到未判定。
            existing[name].pop("required", None)
            existing[name].pop("required_evidence", None)
    evidence_by_field = {}
    for evidence in body_evidence:
        for access in evidence.get("body_accesses") or []:
            if isinstance(access, dict) and access.get("field"):
                evidence_by_field.setdefault(str(access["field"]), []).append(access)
        for condition in evidence.get("conditions") or []:
            if isinstance(condition, dict) and condition.get("condition"):
                text = str(condition["condition"])
                for field_name in existing:
                    if field_name in text:
                        evidence_by_field.setdefault(field_name, []).append(condition)
    source_file = str((candidate.get("code_location") or {}).get("file", ""))
    for field_name, field in existing.items():
        references = evidence_by_field.get(field_name) or []
        if not references:
            continue
        constraints = list(field.get("constraints") or [])
        observed = list(field.get("observed_values") or [])
        source_refs = list(field.get("source_refs") or [])
        for reference in references:
            if reference.get("condition"):
                condition = str(reference["condition"])
                if condition not in constraints:
                    constraints.append(condition)
                observed.extend(re.findall(r"['\"]([^'\"]+)['\"]", condition))
            if reference.get("default_expression") is not None:
                default_expression = str(reference["default_expression"])
                field.setdefault("default", default_expression)
                observed.extend(re.findall(r"['\"]([^'\"]+)['\"]", default_expression))
            if source_file and reference.get("line") is not None:
                source_refs.append({"file": source_file, "line": reference["line"]})
        if constraints:
            field["constraints"] = list(dict.fromkeys(constraints))
        if observed:
            field["observed_values"] = list(dict.fromkeys(observed))
        if source_refs:
            field["source_refs"] = list({(item.get("file"), item.get("line")): item for item in source_refs}.values())
    if existing:
        body["fields"] = list(existing.values())
    return result


def _renumber_interfaces(interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批次合并后统一重编 IF ID，保证跨批唯一且按协议顺序稳定。"""
    counters: dict[str, int] = {}
    for entry in interfaces:
        protocol = str(entry.get("protocol", "rest"))
        counters[protocol] = counters.get(protocol, 0) + 1
        prefix = _PROTOCOL_PREFIX.get(protocol, protocol.upper())
        entry["id"] = f"IF-{prefix}-{counters[protocol]:03d}"
    return interfaces
