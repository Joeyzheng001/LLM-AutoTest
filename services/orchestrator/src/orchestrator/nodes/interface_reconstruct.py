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
        # C-13 语言分流：Java 候选走 java-parser Extract；Python 走内置 AST 扫描
        java_repos = [r for r in repositories if r.language != "python"]
        python_repos = [r for r in repositories if r.language == "python"]
        raw_candidates: list[Any] = []
        raw_warnings: list[str] = []
        if java_repos:
            response = await client.extract(state.job_id, java_repos, protocols)
            raw_candidates.extend(response.get("candidates") or [])
            raw_warnings.extend(
                str(item) for item in response.get("warnings") or [] if isinstance(item, str)
            )
        for repo in python_repos:
            python_result = scan_repository(repo)
            raw_candidates.extend(python_result.candidates)
            raw_warnings.extend(python_result.warnings)
        candidates, warnings = _normalize_candidates(
            {"candidates": raw_candidates, "warnings": raw_warnings}
        )

        # C-09 扩展：接口候选 = **本次代码变更涉及的接口**（此阶段还不到需求映射）。
        # 变更范围是所有仓库的必填项：缺了就只能按全量还原，那不是本次任务的范围。
        # 所以这里直接拦下并指名仓库，而不是静默退化成全量。
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
        scoped = _filter_candidates_by_scope(candidates, changed_files)
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

        # 第①步产物：静态候选如实入库（D1 载体）
        store.write(
            state.job_id,
            "interface_candidates.json",
            {
                "schema_version": "1.0",
                "job_id": state.job_id,
                "candidates": candidates,
                "warnings": warnings,
            },
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
    candidates: list[dict[str, Any]], changed_files: dict[str, set[str]]
) -> list[dict[str, Any]]:
    """按变更范围过滤候选：保留「接口所在文件被改」或「关联文件被改」的候选。

    接口候选 = **本次代码变更涉及的接口**，口径是**行为级**：变更清单是文件级的，只看
    Controller 文件会漏掉「依赖（Service/DTO/Entity/Enum）改了、Controller 文件没改」的
    接口——而这类接口的行为确实随依赖一起变了。候选的 `related_files` 由 java-parser
    静态解析产出（Controller 自身 + 注入字段类型 + 端点方法参数/返回类型，含
    XxxService → XxxServiceImpl 一跳），这里用它做依赖传播。

    未配变更范围的仓库不会走到这里——上游 `_repos_without_change_scope` 已直接拦下，
    所以这里不再保留「未配则全量」的退路。removed 文件里的接口已删除，也不在还原范围
    （`_collect_changed_files` 只收 added + modified）。
    """
    kept: list[dict[str, Any]] = []
    for cand in candidates:
        loc = cand.get("code_location") or {}
        repo = str(loc.get("repo", ""))
        scope = changed_files.get(repo)
        if not scope:
            continue
        if str(loc.get("file", "")) in scope or _touches_changed_file(cand, scope):
            kept.append(cand)
    return kept


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


def _normalize_candidates(response: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """ExtractJob 响应 → (规范化候选列表, 告警清单)。

    候选 ID 规范化：契约只约束 string，产物 Schema 要求 CAND-###；
    缺失/不合规时按顺序重编（数据由代码定，不经 LLM）。
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
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue  # 契约条目级异常：跳过并记告警，不中断整体（X-REV-01 口径）
        entry = dict(item)
        if not _CAND_ID_RE.match(str(entry.get("candidate_id", ""))):
            entry["candidate_id"] = f"CAND-{index:03d}"
        candidates.append(entry)
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
        if item.get("params") is not None:
            entry["params"] = item["params"]
        if item.get("returns") is not None:
            entry["returns"] = item["returns"]
        if cand.get("confidence") is not None:
            entry["confidence"] = cand["confidence"]
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


def _renumber_interfaces(interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批次合并后统一重编 IF ID，保证跨批唯一且按协议顺序稳定。"""
    counters: dict[str, int] = {}
    for entry in interfaces:
        protocol = str(entry.get("protocol", "rest"))
        counters[protocol] = counters.get(protocol, 0) + 1
        prefix = _PROTOCOL_PREFIX.get(protocol, protocol.upper())
        entry["id"] = f"IF-{prefix}-{counters[protocol]:03d}"
    return interfaces
