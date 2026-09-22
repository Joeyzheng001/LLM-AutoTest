"""req_parse 节点：来源锚定、需求条目提取和需求质量评审（F-REQ-01~03）。

多文档口径（C-10）：各文档建立段落级来源目录，先提取带来源引用的原子声明，再执行
需求质量评审。双版本文档的来源继续回链到基线/被测原文。两份产物 `requirement_review.json` 与
`requirement_items.json` 均通过 Schema 才允许节点完成。

失败口径（docs/12）：
- Prompt 缺失 → X-LLM-03；LLM 网络/超时 → X-LLM-01（客户端内重试）；
- LLM 输出不可解析或未过 Schema → X-LLM-02（不重试，人工介入）；
- 全部失败路径经 engine.mark_failed 记入 state.json 后再抛出。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Protocol

from orchestrator.artifacts import ArtifactStore
from orchestrator.doc_loader import load_document_text
from orchestrator.engine import Engine, JobState
from orchestrator.errors import X_LLM_02, X_ORCH_01, PipelineError
from orchestrator.llm import LlmResult
from orchestrator.llm_batch import chunk_text
from orchestrator.llm_repair import complete_with_json_repair
from orchestrator.prompts_loader import PromptSpec, load_prompt

NODE_NAME = "req_parse"

_DIMENSION_ALIASES = {
    "一致性": "一致性与逻辑性",
    "清晰度": "清晰度与规范性",
    "风险与依赖": "风险与依赖描述",
}
_DIMENSION_WEIGHTS = {
    "需求完整性": 0.35,
    "一致性与逻辑性": 0.20,
    "可测试性": 0.25,
    "清晰度与规范性": 0.10,
    "风险与依赖描述": 0.10,
}

# Legacy offline callers may still use the historical review normalizer; the live path
# emits compact findings and derives the presentation fields in program code.
_STRING_LIST_FIELDS = (
    "readable_issue_points",
    "completeness_issues",
    "consistency_issues",
    "untestable_items",
    "testable_features",
)

_SOURCE_REF_RE = re.compile(r"\bSRC-[A-Z0-9-]+\b")
_CLAIM_KINDS = {
    "normal", "validation", "boundary", "exception", "authorization",
    "state_transition", "data_consistency", "non_functional", "dependency",
}


class SupportsComplete(Protocol):
    """LLM 客户端协议（生产为 LlmClient，测试可注入桩）。"""

    async def complete(self, node: str, prompt: PromptSpec, user_content: str) -> LlmResult:
        """按协议执行一次补全。"""
        ...


async def run_req_parse(
    engine: Engine,
    state: JobState,
    store: ArtifactStore,
    llm: SupportsComplete,
    prompts_dir: Path,
    doc_paths: list[Path],
    document_pairs: list[dict[str, str | None]] | None = None,
) -> Path:
    """执行 req_parse 全流程：跳序检查 → 运行 → 解析 → 校验落盘 → 置完成。

    Args:
        doc_paths: 需求文档路径列表（C-10 多文档；至少 1 份，调用方保证非空）。

    Returns:
        产物文件路径。

    Raises:
        PipelineError: 各失败口径见模块 docstring。
    """
    engine.check_runnable(state, NODE_NAME)
    engine.mark_running(state, NODE_NAME)
    try:
        # 有基线时先形成差异产物；后续所有解析只看差异范围。
        doc_text = ""
        diff_payload: dict[str, Any] | None = None
        if document_pairs and any(pair.get("baseline_path") for pair in document_pairs):
            diff_payload = await _build_requirement_diff(
                store, state.job_id, llm, prompts_dir, document_pairs
            )
            diff_artifact = {
                **diff_payload,
                "source_catalog": _source_catalog_previews(diff_payload["source_catalog"]),
            }
            _write_llm_artifact(store, state.job_id, "requirement_diff.json", diff_artifact)
            doc_text = _diff_as_requirement_input(diff_payload)

        source_catalog = (
            diff_payload["source_catalog"]
            if diff_payload is not None
            else _build_source_catalog(doc_paths, doc_text)
        )
        # 差异模式的输入已包含带 SRC 编号的基线/被测原文片段；普通解析在此处加编号。
        source_text = doc_text if diff_payload is not None else _annotate_source_text(source_catalog)

        items_prompt = load_prompt(prompts_dir, NODE_NAME)
        item_batches: list[tuple[list[dict[str, Any]], list[str]]] = []
        document_chunks = chunk_text(source_text)
        for index, chunk in enumerate(document_chunks, start=1):
            batch_input = _batch_input("需求条目提取", index, len(document_chunks), chunk)
            allowed_refs = sorted(set(_SOURCE_REF_RE.findall(chunk)))
            batch_items = await complete_with_json_repair(
                llm,
                NODE_NAME,
                items_prompt,
                batch_input,
                _extract_items,
                lambda items, refs=set(allowed_refs): _validate_item_batch(items, refs),
            )
            item_batches.append((batch_items, allowed_refs))
        items = _renumber_items(item_batches, source_catalog)
        payload: dict[str, Any] = {
            "schema_version": "2.0",
            "job_id": state.job_id,
            # C-10：source_files 记全部；source_file 保留首文档兼容旧读取方
            "source_files": [str(path) for path in doc_paths],
            "source_file": str(doc_paths[0]),
            "source_catalog": _source_catalog_previews(source_catalog),
            "items": items,
        }
        items_path = _write_llm_artifact(store, state.job_id, "requirement_items.json", payload)

        # 需求质量评审基于稳定编号后的原子声明分批执行，模型只提交问题发现。
        review_findings = await _review_requirement_batches(
            llm, prompts_dir, items, source_catalog
        )
        review_payload = _build_review_payload(
            review_findings, items, state.job_id, doc_paths
        )
        _write_llm_artifact(store, state.job_id, "requirement_review.json", review_payload)
        engine.mark_completed(state, NODE_NAME)
        return items_path
    except PipelineError as exc:
        engine.mark_failed(state, NODE_NAME, exc)
        raise


def _build_review_payload(
    findings: list[dict[str, Any]],
    items: list[dict[str, Any]],
    job_id: str,
    doc_paths: list[Path],
) -> dict[str, Any]:
    """由少量模型发现构造兼容页面的完整评审视图，并确定性计算评分。"""
    dimension_names = tuple(_DIMENSION_WEIGHTS)
    item_by_claim = {
        str(claim["claim_id"]): item
        for item in items
        for claim in item.get("claims", [])
    }
    claim_by_id = {
        str(claim["claim_id"]): claim
        for item in items
        for claim in item.get("claims", [])
    }
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    priority_rank = {"高": 0, "中": 1, "低": 2}
    type_to_dimension = {
        "缺失信息": "需求完整性",
        "歧义": "清晰度与规范性",
        "冲突": "一致性与逻辑性",
        "不可测": "可测试性",
        "其它": "清晰度与规范性",
        "建议": "风险与依赖描述",
    }
    for raw in findings:
        issue_type = str(raw.get("type", "其它"))
        if issue_type not in {"缺失信息", "歧义", "冲突", "不可测", "其它", "建议"}:
            issue_type = "其它"
        priority = str(raw.get("priority", "中"))
        if priority not in priority_rank:
            priority = "中"
        claim_ids = [
            str(value) for value in (raw.get("claim_ids") or [])
            if str(value) in claim_by_id
        ]
        source_refs = [
            str(value) for value in (raw.get("source_refs") or [])
            if isinstance(value, str)
        ]
        if not source_refs:
            for claim_id in claim_ids:
                source_refs.extend(claim_by_id[claim_id].get("source_refs", []))
        source_refs = list(dict.fromkeys(source_refs))
        location = str(raw.get("location", "")).strip()
        if not location and claim_ids:
            parent = item_by_claim.get(claim_ids[0], {})
            location = str(parent.get("title", claim_ids[0]))
        question = str(raw.get("question", raw.get("description", ""))).strip()
        reason = str(raw.get("reason", raw.get("impact", ""))).strip()
        impact = str(raw.get("impact", reason)).strip()
        if not question:
            continue
        key = (issue_type, question)
        if key in seen:
            continue
        seen.add(key)
        dimension = raw.get("dimension")
        if dimension not in dimension_names:
            dimension = type_to_dimension[issue_type]
        normalized.append(
            {
                "type": issue_type,
                "priority": priority,
                "location": location or "需求文档",
                "question": question,
                "reason": reason or "评审发现该项信息不足，建议结合原文确认。",
                "impact": impact or "可能导致实现、测试或验收口径不一致。",
                "dimension": dimension,
                "claim_ids": claim_ids,
                "source_refs": source_refs,
            }
        )
    normalized.sort(key=lambda issue: (priority_rank[issue["priority"]], issue["dimension"], issue["location"]))
    normalized = normalized[:20]

    dimension_scores: dict[str, dict[str, Any]] = {}
    for name, weight in _DIMENSION_WEIGHTS.items():
        related = [issue for issue in normalized if issue["dimension"] == name]
        penalty = sum({"高": 12, "中": 7, "低": 3}[issue["priority"]] for issue in related)
        score = max(0, 100 - penalty)
        reason = (
            f"本维度记录 {len(related)} 项问题；分值按优先级扣分，详情见问题清单。"
            if related
            else "本轮未识别到明确问题；结论仅覆盖本次提交的需求内容。"
        )
        dimension_scores[name] = {"weight": weight, "score": score, "reason": reason}
    score = round(sum(weight * dimension_scores[name]["score"] for name, weight in _DIMENSION_WEIGHTS.items()))
    has_core_gap = any(
        issue["priority"] == "高" and issue["dimension"] in {"需求完整性", "可测试性"}
        for issue in normalized
    )
    grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 50 else "D"
    if has_core_gap and grade in {"A", "B"}:
        grade = "C"
    grade_name = {"A": "优秀", "B": "良好", "C": "需补充", "D": "较差"}[grade]
    high_count = sum(issue["priority"] == "高" for issue in normalized)
    summary = (
        f"识别到 {len(normalized)} 项需求质量问题，其中高优先级 {high_count} 项；请优先处理高优先级澄清。"
        if normalized
        else "本轮未识别到明确需求质量问题；评审范围以已提供的文档片段为准。"
    )
    readable = [issue["question"] for issue in normalized if issue["priority"] == "高"][:10]
    completeness = [issue["question"] for issue in normalized if issue["dimension"] == "需求完整性"]
    consistency = [issue["question"] for issue in normalized if issue["dimension"] == "一致性与逻辑性"]
    untestable = [issue["question"] for issue in normalized if issue["dimension"] == "可测试性"]
    risks = [
        {"type": issue["dimension"], "desc": issue["impact"]}
        for issue in normalized
        if issue["dimension"] == "风险与依赖描述" or issue["priority"] == "高"
    ][:10]
    untestable_claim_ids = {
        claim_id
        for issue in normalized
        if issue["dimension"] == "可测试性" or issue["type"] == "不可测"
        for claim_id in issue["claim_ids"]
    }
    testable_features = [
        str(claim["statement"])
        for item in items
        for claim in item.get("claims", [])
        if claim.get("claim_id") not in untestable_claim_ids
    ][:30]
    mindmap = {
        "主题": "需求测试范围",
        "子主题": [
            {
                "主题": str(item["title"]),
                "子主题": [
                    {"主题": str(claim["statement"]), "子主题": []}
                    for claim in item.get("claims", [])[:12]
                ],
            }
            for item in items[:12]
        ],
    }
    return {
        "schema_version": "2.0",
        "job_id": job_id,
        "source_files": [str(path) for path in doc_paths],
        "score": score,
        "quality_grade": grade,
        "summary": summary,
        "dimension_scores": dimension_scores,
        "quality_assessment": {"grade": grade, "grade_name": grade_name, "summary_reason": summary},
        "issues": normalized,
        "readable_issue_points": readable,
        "completeness_issues": completeness[:20],
        "consistency_issues": consistency[:20],
        "untestable_items": untestable[:20],
        "risk_flags": risks,
        "testable_features": testable_features,
        "mindmap": mindmap,
    }


async def _build_requirement_diff(
    store: ArtifactStore,
    job_id: str,
    llm: SupportsComplete,
    prompts_dir: Path,
    document_pairs: list[dict[str, str | None]],
) -> dict[str, Any]:
    """逐对分析需求版本差异并由程序统一编号，避免长 JSON 与跨对串扰。"""
    prompt = load_prompt(prompts_dir, "requirement_diff")
    merged_changes: list[dict[str, Any]] = []
    summaries: list[str] = []
    source_catalog: list[dict[str, Any]] = []
    for pair_index, pair in enumerate(document_pairs, start=1):
        target_path = Path(str(pair["target_path"]))
        if not pair.get("baseline_path"):
            target_sources = _catalog_document_sources(
                target_path.name,
                load_document_text(target_path),
                source_prefix=f"SRC-P{pair_index:02d}-T",
                pair_index=pair_index,
                version="target",
            )
            source_catalog.extend(target_sources)
            summaries.append(f"文档对 {pair_index} 未提供基线，已按被测版本原文纳入需求范围")
            for source in target_sources:
                locator = str(source.get("section") or target_path.name)
                locator += f" · 文本片段 {source['paragraph_index']}.{source['fragment_index']}"
                merged_changes.append(
                    {
                        "pair_index": pair_index,
                        "change_type": "added",
                        "title": str(source.get("section") or target_path.stem),
                        "target_description": str(source["excerpt"])[:600],
                        "baseline_description": "",
                        "change_summary": "该行未提供基线版本，按被测版本原始需求处理。",
                        "source_location": locator,
                        "target_source_refs": [str(source["source_id"])],
                        "baseline_source_refs": [],
                        "source_refs": [str(source["source_id"])],
                    }
                )
            continue
        baseline_path = Path(str(pair["baseline_path"]))
        baseline_sources = _catalog_document_sources(
            baseline_path.name,
            load_document_text(baseline_path),
            source_prefix=f"SRC-P{pair_index:02d}-B",
            pair_index=pair_index,
            version="baseline",
        )
        target_sources = _catalog_document_sources(
            target_path.name,
            load_document_text(target_path),
            source_prefix=f"SRC-P{pair_index:02d}-T",
            pair_index=pair_index,
            version="target",
        )
        pair_sources = baseline_sources + target_sources
        source_catalog.extend(pair_sources)
        content = (
            f"文档对 {pair_index}/{len(document_pairs)}\n\n"
            f"## 基线版本：{baseline_path.name}\n\n"
            f"{_annotate_source_text(baseline_sources)}\n\n"
            f"## 被测版本：{target_path.name}\n\n"
            f"{_annotate_source_text(target_sources)}"
        )
        allowed_refs = {str(source["source_id"]) for source in pair_sources}
        raw = await complete_with_json_repair(
            llm,
            NODE_NAME,
            prompt,
            content,
            _extract_diff,
            lambda payload, refs=allowed_refs: _validate_diff_shape(payload, refs),
        )
        summaries.append(str(raw["summary"]))
        for change in raw["changes"]:
            target_refs = list(dict.fromkeys(change["target_source_refs"]))
            baseline_refs = list(dict.fromkeys(change["baseline_source_refs"]))
            refs = list(dict.fromkeys(target_refs + baseline_refs))
            merged_changes.append(
                {
                    "pair_index": pair_index,
                    **change,
                    "target_source_refs": target_refs,
                    "baseline_source_refs": baseline_refs,
                    "source_refs": refs,
                }
            )
    for index, change in enumerate(merged_changes, start=1):
        change["id"] = f"DIF-{index:03d}"
    return {
        "schema_version": "2.0",
        "job_id": job_id,
        "has_baseline": True,
        "summary": "；".join(summaries) or "未识别到可验证的需求差异",
        "changes": merged_changes,
        "source_catalog": source_catalog,
    }


def _source_catalog_previews(source_catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给持久化产物保留受限预览，内部解析/评审仍使用完整的 2400 字原文片段。"""
    return [
        {
            **source,
            "excerpt": str(source["excerpt"])[:600],
            "excerpt_truncated": len(str(source["excerpt"])) > 600,
        }
        for source in source_catalog
    ]


def _extract_diff(text: str) -> dict[str, Any]:
    """解析差异 Prompt 输出；字段和值域由 Schema 前的形状检查约束。"""
    cleaned = _strip_code_fence(text)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise PipelineError(
            code=X_LLM_02,
            message=f"需求差异分析 LLM 输出不是合法 JSON：{exc}",
            node_id=NODE_NAME,
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("changes"), list):
        raise PipelineError(
            code=X_LLM_02,
            message="需求差异分析 LLM 输出缺少 changes 数组（或结构不符）",
            node_id=NODE_NAME,
        )
    payload.pop("schema_version", None)
    payload.pop("job_id", None)
    return payload


def _validate_diff_shape(payload: dict[str, Any], allowed_refs: set[str] | None = None) -> None:
    """让缺 summary/changes 的典型漂移进入一次 JSON 修复轮。"""
    if not isinstance(payload.get("summary"), str) or not isinstance(payload.get("changes"), list):
        raise PipelineError(
            code=X_LLM_02,
            message="需求差异输出必须是包含 summary 字符串与 changes 数组的 JSON 对象",
            node_id=NODE_NAME,
        )
    required = {
        "change_type",
        "title",
        "target_description",
        "baseline_description",
        "change_summary",
        "source_location",
        "target_source_refs",
        "baseline_source_refs",
    }
    malformed = any(
        not isinstance(change, dict) or not required <= set(change)
        for change in payload["changes"]
    )
    if malformed:
        raise PipelineError(
            code=X_LLM_02,
            message="需求差异 changes 每项必须包含变化类型、标题、两侧描述、差异说明和来源定位",
            node_id=NODE_NAME,
        )
    for index, change in enumerate(payload["changes"], start=1):
        change_type = change.get("change_type")
        target_refs = change.get("target_source_refs")
        baseline_refs = change.get("baseline_source_refs")
        if (
            change_type not in {"added", "modified", "removed"}
            or not isinstance(target_refs, list)
            or not isinstance(baseline_refs, list)
            or any(not isinstance(ref, str) for ref in target_refs + baseline_refs)
        ):
            raise PipelineError(
                code=X_LLM_02,
                message=f"需求差异第 {index} 项的版本来源引用结构不正确",
                node_id=NODE_NAME,
            )
        if change_type in {"added", "modified"} and not target_refs:
            raise PipelineError(code=X_LLM_02, message=f"需求差异第 {index} 项缺少被测版本来源", node_id=NODE_NAME)
        if change_type in {"removed", "modified"} and not baseline_refs:
            raise PipelineError(code=X_LLM_02, message=f"需求差异第 {index} 项缺少基线版本来源", node_id=NODE_NAME)
        if change_type == "added" and baseline_refs:
            raise PipelineError(code=X_LLM_02, message=f"新增需求第 {index} 项不得引用基线来源", node_id=NODE_NAME)
        if change_type == "removed" and target_refs:
            raise PipelineError(code=X_LLM_02, message=f"删除需求第 {index} 项不得引用被测版本来源", node_id=NODE_NAME)
        refs = target_refs + baseline_refs
        if allowed_refs is not None and any(ref not in allowed_refs for ref in refs):
            raise PipelineError(
                code=X_LLM_02,
                message=f"需求差异第 {index} 项引用了不存在或不属于当前文档对的来源编号",
                node_id=NODE_NAME,
            )


def _diff_as_requirement_input(payload: dict[str, Any]) -> str:
    """将差异产物变为后续节点唯一需求范围；删除项只保留影响核查，不造成功用例。"""
    source_by_id = {
        str(source["source_id"]): source
        for source in payload.get("source_catalog", [])
        if isinstance(source, dict)
    }
    lines = ["# 需求版本差异分析（后续处理范围）", str(payload["summary"])]
    for change in payload["changes"]:
        change_type = str(change["change_type"])
        refs = list(change.get("source_refs") or [])
        lines.extend(
            [
                f"## {change['id']} · {change_type} · {change['title']}",
                f"差异：{change['change_summary']}",
                f"来源：{change['source_location']}",
                f"来源编号：{'、'.join(refs)}",
            ]
        )
        if change_type in {"added", "modified"}:
            lines.append(f"被测需求：{change['target_description']}")
        else:
            lines.append(f"删除需求（仅做实现影响与回归风险核查）：{change['baseline_description']}")
        for source_id in refs:
            source = source_by_id.get(str(source_id))
            if not source:
                continue
            locator = str(source.get("document", "原文"))
            if source.get("section"):
                locator += f" · {source['section']}"
            locator += f" · 文本片段 {source.get('paragraph_index', '?')}.{source.get('fragment_index', '?')}"
            version = source.get("version")
            if version:
                locator = f"{version}版本 · {locator}"
            lines.append(f"原文 [{source_id}] {locator}\n{source['excerpt']}")
    return "\n\n".join(lines)


def _batch_input(label: str, index: int, total: int, content: str) -> str:
    """给模型显式批次边界；模型不得引用未随本批提供的内容。"""
    return (
        f"这是{label}第 {index}/{total} 批。仅处理本批内容；"
        "输出仍必须满足 Prompt 规定的完整 JSON 结构。\n\n"
        f"{content}"
    )


def _build_source_catalog(doc_paths: list[Path], text: str) -> list[dict[str, Any]]:
    """建立由程序控制的段落级来源目录；LLM 仅引用 source_id。"""
    source_docs: list[tuple[str, str]] = []
    if text.lstrip().startswith("# 需求版本差异分析"):
        source_docs.append(("需求版本差异分析", text))
    else:
        for path in doc_paths:
            source_docs.append((path.name, load_document_text(path)))

    catalog: list[dict[str, Any]] = []
    for doc_index, (name, content) in enumerate(source_docs, start=1):
        catalog.extend(
            _catalog_document_sources(
                name,
                content,
                source_prefix=f"SRC-D{doc_index:02d}",
            )
        )
    return catalog


def _catalog_document_sources(
    name: str,
    content: str,
    source_prefix: str,
    pair_index: int | None = None,
    version: str | None = None,
) -> list[dict[str, Any]]:
    """把一个实际文档切为有稳定段落/片段坐标的来源目录。"""
    catalog: list[dict[str, Any]] = []
    lines = content.splitlines()
    section = ""
    paragraph: list[str] = []
    paragraph_index = 0

    def flush() -> None:
        nonlocal paragraph_index
        if not paragraph:
            return
        raw = "\n".join(paragraph).strip()
        if raw:
            paragraph_index += 1
            fragments = [raw[pos : pos + 2400] for pos in range(0, len(raw), 2400)]
            for fragment_index, excerpt in enumerate(fragments, start=1):
                source_id = f"{source_prefix}-P{paragraph_index:04d}"
                if len(fragments) > 1:
                    source_id += f"-S{fragment_index:02d}"
                source: dict[str, Any] = {
                    "source_id": source_id,
                    "document": name,
                    "section": section,
                    "paragraph_index": paragraph_index,
                    "fragment_index": fragment_index,
                    "excerpt": excerpt,
                    "content_hash": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                }
                if pair_index is not None:
                    source["pair_index"] = pair_index
                if version is not None:
                    source["version"] = version
                catalog.append(source)
        paragraph.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            section = stripped.lstrip("# ").strip()
        elif not stripped:
            flush()
        else:
            paragraph.append(line)
    flush()
    return catalog


def _annotate_source_text(source_catalog: list[dict[str, Any]]) -> str:
    """把来源 ID 与准确原文一起送入模型，便于逐条引用。"""
    blocks = []
    for source in source_catalog:
        locator = f"{source['document']}"
        if source.get("section"):
            locator += f" · {source['section']}"
        locator += f" · 文本片段 {source['paragraph_index']}.{source['fragment_index']}"
        blocks.append(f"[{source['source_id']}] {locator}\n{source['excerpt']}")
    return "\n\n---\n\n".join(blocks)


def _validate_item_batch(items: list[dict[str, Any]], allowed_refs: set[str]) -> None:
    """检查模型来源引用；不要求模型生成程序掌管的 ID。"""
    for index, item in enumerate(items, start=1):
        title = item.get("title") or item.get("description")
        if not isinstance(title, str) or not title.strip():
            raise PipelineError(code=X_LLM_02, message=f"需求条目 {index} 缺少 title", node_id=NODE_NAME)
        refs = item.get("source_refs")
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) or ref not in allowed_refs for ref in refs)
        ):
            raise PipelineError(
                code=X_LLM_02,
                message=f"需求条目 {index} 引用了不存在或不属于当前批次的 source_id",
                node_id=NODE_NAME,
            )
        claims = item.get("claims")
        if claims is not None and (
            not isinstance(claims, list)
            or any(not isinstance(claim, dict) for claim in claims)
        ):
            raise PipelineError(code=X_LLM_02, message=f"需求条目 {index} 的 claims 结构不正确", node_id=NODE_NAME)
        for claim in claims or []:
            statement = claim.get("statement")
            if not isinstance(statement, str) or not statement.strip():
                raise PipelineError(code=X_LLM_02, message=f"需求条目 {index} 存在空 claim", node_id=NODE_NAME)
            kind = claim.get("kind", "normal")
            if kind not in _CLAIM_KINDS:
                raise PipelineError(code=X_LLM_02, message=f"需求条目 {index} 的 claim kind 不合法", node_id=NODE_NAME)
            claim_refs = claim.get("source_refs")
            if (
                not isinstance(claim_refs, list)
                or not claim_refs
                or any(not isinstance(ref, str) or ref not in allowed_refs for ref in claim_refs)
            ):
                raise PipelineError(code=X_LLM_02, message=f"需求条目 {index} 的 claim 引用了非法 source_id", node_id=NODE_NAME)
            if not set(claim_refs) <= set(refs):
                raise PipelineError(code=X_LLM_02, message=f"需求条目 {index} 的 claim 来源不在 item.source_refs 内", node_id=NODE_NAME)


def _renumber_items(
    batches: list[tuple[list[dict[str, Any]], list[str]]],
    source_catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """程序分配稳定 REQ/CLM ID，补齐精确来源和旧消费者仍使用的描述字段。"""
    source_by_id = {str(source["source_id"]): source for source in source_catalog}
    merged: list[dict[str, Any]] = []
    for batch_items, batch_refs in batches:
        allowed = set(batch_refs)
        _validate_item_batch(batch_items, allowed)
        for raw in batch_items:
            item = dict(raw)
            refs = item.get("source_refs") or batch_refs
            refs = list(dict.fromkeys(ref for ref in refs if ref in allowed))
            if not refs:
                raise PipelineError(
                    code=X_LLM_02,
                    message="需求条目无法关联到任何来源片段",
                    node_id=NODE_NAME,
                )
            raw_claims = item.get("claims")
            if not isinstance(raw_claims, list) or not raw_claims:
                description = str(item.get("description", "")).strip()
                if not description:
                    raise PipelineError(code=X_LLM_02, message="需求条目缺少可验证描述", node_id=NODE_NAME)
                raw_claims = [{"kind": "normal", "statement": description}]
            claims: list[dict[str, Any]] = []
            for claim in raw_claims:
                claim_refs = claim.get("source_refs") or refs
                claim_refs = list(dict.fromkeys(ref for ref in claim_refs if ref in refs))
                if not claim_refs:
                    claim_refs = refs
                criteria = claim.get("acceptance_criteria") or []
                if not isinstance(criteria, list):
                    criteria = [str(criteria)]
                claims.append(
                    {
                        "claim_id": "",
                        "kind": claim.get("kind", "normal"),
                        "statement": str(claim["statement"]).strip(),
                        "acceptance_criteria": [str(value).strip() for value in criteria if str(value).strip()],
                        "source_refs": claim_refs,
                    }
                )
            first_source = source_by_id.get(refs[0], {})
            description = "；".join(
                "；".join([claim["statement"], *claim["acceptance_criteria"]]).strip("；")
                for claim in claims
            )
            normalized: dict[str, Any] = {
                "id": "",
                "title": str(item.get("title") or claims[0]["statement"]).strip(),
                "description": description,
                "source_refs": refs,
                "claims": claims,
            }
            if item.get("priority") in {"P0", "P1", "P2"}:
                normalized["priority"] = item["priority"]
            if first_source:
                location: dict[str, Any] = {}
                if first_source.get("section"):
                    location["section"] = first_source["section"]
                if location:
                    normalized["source_location"] = location
            merged.append(normalized)
    if not merged:
        raise PipelineError(
            code=X_LLM_02,
            message="所有需求解析批次均未产出条目",
            node_id=NODE_NAME,
        )
    claim_index = 1
    for index, item in enumerate(merged, start=1):
        item["id"] = f"REQ-{index:03d}"
        for claim in item["claims"]:
            claim["claim_id"] = f"CLM-{claim_index:04d}"
            claim_index += 1
    return merged


def _normalize_item_source_location(item: dict[str, Any]) -> None:
    """归一 requirement_items 的可选来源坐标，避免类型漂移阻断整批结果。"""
    location = item.get("source_location")
    if not isinstance(location, dict):
        return
    normalized = dict(location)
    line = normalized.get("line")
    if isinstance(line, str):
        value = line.strip()
        if value.isdigit() and int(value) >= 1:
            normalized["line"] = int(value)
        else:
            normalized.pop("line", None)
            # 章节/功能编号不是行号；仅在 section 缺失时无损迁移。
            if value and not normalized.get("section"):
                normalized["section"] = value
    elif isinstance(line, bool) or (isinstance(line, int) and line < 1):
        normalized.pop("line", None)
    item["source_location"] = normalized


def _write_llm_artifact(
    store: ArtifactStore, job_id: str, filename: str, payload: dict[str, Any]
) -> Path:
    """写 LLM 产物；Schema 不合规统一归 X-LLM-02（不保存半成品）。"""
    try:
        return store.write(job_id, filename, payload)
    except PipelineError as exc:
        if exc.code == X_ORCH_01:
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 输出未过 Schema 校验：{filename}：{exc.message}",
                node_id=NODE_NAME,
                details=exc.details,
            ) from exc
        raise


def _merge_documents(doc_paths: list[Path]) -> str:
    """多文档分节拼接（C-10）：各文档提取纯文本后按文件名分节，单次送 LLM。

    任一文档缺失/不可读 → load_document_text 抛 X-ORCH-01（宁停不猜，
    不跳过任何一份——跳过会造成需求覆盖缺口而不被案觉）。
    """
    sections = [
        f"## 需求文档 {index}：{path.name}\n\n{load_document_text(path)}"
        for index, path in enumerate(doc_paths, start=1)
    ]
    return "\n\n---\n\n".join(sections)


def _extract_items(text: str) -> list[dict[str, Any]]:
    """从 LLM 输出提取 items 数组（容忍 Markdown 围栏，不截取可用部分）。

    Raises:
        PipelineError: X-LLM-02——输出不可解析为 JSON 或缺 items。
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
    # 模型常把 items 命名为 requirements/requirement_items，或包进 data/result；
    # 都是无歧义的容器差异，统一为契约 items，字段本身仍交给后续 Schema 校验。
    items = _normalize_items_container(data)
    if not isinstance(items, list):
        raise PipelineError(
            code=X_LLM_02,
            message="LLM 输出缺少 items 数组（或结构不符）",
            node_id=NODE_NAME,
        )
    if not all(isinstance(item, dict) for item in items):
        raise PipelineError(
            code=X_LLM_02,
            message="LLM 输出的 items 含非对象条目",
            node_id=NODE_NAME,
        )
    return [dict(item) for item in items]


def _normalize_items_container(data: Any) -> list[Any] | None:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    for key in ("items", "requirements", "requirement_items", "需求条目", "需求列表"):
        if isinstance(data.get(key), list):
            return [item for item in data[key]]
    for key in ("data", "result", "output"):
        nested = data.get(key)
        if isinstance(nested, dict):
            items = _normalize_items_container(nested)
            if items is not None:
                return items
    return None


async def _review_requirement_batches(
    llm: SupportsComplete,
    prompts_dir: Path,
    items: list[dict[str, Any]],
    source_catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """对已编号需求分批评审；跨批结果只合并发现，不重新生成大对象。"""
    prompt = load_prompt(prompts_dir, "requirement_review")
    sources_by_id = {str(source["source_id"]): source for source in source_catalog}
    batches = chunk_json_items(items, max_chars=6_000)
    findings: list[dict[str, Any]] = []
    for index, batch in enumerate(batches, start=1):
        claim_ids = {
            str(claim["claim_id"])
            for item in batch
            for claim in item.get("claims", [])
        }
        source_ids = {
            str(ref)
            for item in batch
            for ref in item.get("source_refs", [])
        }
        for item in batch:
            for claim in item.get("claims", []):
                source_ids.update(str(ref) for ref in claim.get("source_refs", []))
        batch_sources = [sources_by_id[source_id] for source_id in sorted(source_ids) if source_id in sources_by_id]
        user_content = json.dumps(
            {
                "batch": {"index": index, "total": len(batches)},
                "requirements": batch,
                "source_catalog": batch_sources,
            },
            ensure_ascii=False,
        )
        batch_findings = await complete_with_json_repair(
            llm,
            "requirement_review",
            prompt,
            user_content,
            _extract_review_findings,
            lambda values, ids=claim_ids, refs=source_ids: _validate_review_findings(values, ids, refs),
        )
        findings.extend(batch_findings)
    return findings


def _extract_review_findings(text: str) -> list[dict[str, Any]]:
    """解析新 finding 契约，并兼容旧评审响应中的 issues 数组。"""
    cleaned = _strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise PipelineError(
            code=X_LLM_02,
            message=f"需求评审 LLM 输出不是合法 JSON：{exc}",
            node_id="requirement_review",
        ) from exc
    if isinstance(data, dict):
        findings = data.get("findings")
        if findings is None:
            findings = data.get("issues")
    else:
        findings = None
    if not isinstance(findings, list) or any(not isinstance(item, dict) for item in findings):
        raise PipelineError(
            code=X_LLM_02,
            message="需求评审 LLM 输出缺少 findings 数组（或结构不符）",
            node_id="requirement_review",
        )
    return [dict(item) for item in findings]


def _validate_review_findings(
    findings: list[dict[str, Any]], claim_ids: set[str], source_ids: set[str]
) -> None:
    """检查 finding 的问题字段和引用均来自当前评审批次。"""
    allowed_types = {"缺失信息", "歧义", "冲突", "不可测", "其它", "建议"}
    allowed_priorities = {"高", "中", "低"}
    allowed_dimensions = set(_DIMENSION_WEIGHTS)
    for index, finding in enumerate(findings, start=1):
        if finding.get("type") not in allowed_types or finding.get("priority") not in allowed_priorities:
            raise PipelineError(
                code=X_LLM_02,
                message=f"需求评审 finding {index} 的 type/priority 不合法",
                node_id="requirement_review",
            )
        refs = finding.get("claim_ids", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in claim_ids for ref in refs):
            raise PipelineError(
                code=X_LLM_02,
                message=f"需求评审 finding {index} 引用了不存在或不属于当前批次的 claim_id",
                node_id="requirement_review",
            )
        sources = finding.get("source_refs", [])
        if not isinstance(sources, list) or any(not isinstance(ref, str) or ref not in source_ids for ref in sources):
            raise PipelineError(
                code=X_LLM_02,
                message=f"需求评审 finding {index} 引用了不存在或不属于当前批次的 source_id",
                node_id="requirement_review",
            )
        if not refs and not sources:
            raise PipelineError(
                code=X_LLM_02,
                message=f"需求评审 finding {index} 缺少可追溯的 claim_id/source_ref",
                node_id="requirement_review",
            )
        if finding.get("dimension") is not None and finding.get("dimension") not in allowed_dimensions:
            raise PipelineError(
                code=X_LLM_02,
                message=f"需求评审 finding {index} 的 dimension 不合法",
                node_id="requirement_review",
            )
        if not isinstance(finding.get("question", finding.get("description")), str) or not str(
            finding.get("question", finding.get("description", ""))
        ).strip():
            raise PipelineError(
                code=X_LLM_02,
                message=f"需求评审 finding {index} 缺少可回答的问题描述",
                node_id="requirement_review",
            )
        if (
            not isinstance(finding.get("reason"), str)
            or not finding["reason"].strip()
            or not isinstance(finding.get("impact"), str)
            or not finding["impact"].strip()
        ):
            raise PipelineError(
                code=X_LLM_02,
                message=f"需求评审 finding {index} 必须说明 reason 和 impact",
                node_id="requirement_review",
            )


def _extract_review(text: str) -> dict[str, Any]:
    """解析需求评审 JSON；字段完整性由 requirement_review Schema 最终裁决。

    MiniMax 偶尔会把两个无歧义的展示形式输出成更适合阅读的结构：
    ``dimension_scores`` 的维度数组，以及思维导图叶子字符串。这里仅将这两种
    等价表示归一为产物契约；业务字段、枚举和值域仍完全交由 Schema 拒绝。
    """
    cleaned = _strip_code_fence(text)
    try:
        review = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise PipelineError(
            code=X_LLM_02,
            message=f"需求评审 LLM 输出不是合法 JSON：{exc}",
            node_id=NODE_NAME,
        ) from exc
    if not isinstance(review, dict):
        raise PipelineError(
            code=X_LLM_02,
            message="需求评审 LLM 输出不是 JSON 对象",
            node_id=NODE_NAME,
        )
    review = _normalize_review_shape(review)
    # 产物归属字段只能由系统写入，防止模型伪造跨任务归属。
    review.pop("schema_version", None)
    review.pop("job_id", None)
    review.pop("source_files", None)
    return review


def _normalize_review_shape(review: dict[str, Any]) -> dict[str, Any]:
    """归一需求评审中两种无歧义、但不符合持久化 Schema 的等价格式。

    不补默认业务值、不修正评分或等级，也不会移除未知字段；因此最终 Schema
    仍是唯一的产物质量闸门。
    """
    normalized = dict(review)

    # 部分模型把五个维度直接置于根对象。该结构信息完整，只是少一层容器；
    # 迁入 dimension_scores 并删除根级字段，避免 additionalProperties 拒绝产物。
    root_dimensions = {
        name: normalized.pop(name) for name in list(normalized) if name in _DIMENSION_WEIGHTS
    }
    if root_dimensions and not isinstance(normalized.get("dimension_scores"), (dict, list)):
        normalized["dimension_scores"] = root_dimensions

    # 模型常输出 [{"dimension": "需求完整性", ...}]，而契约采用以维度名为键的对象。
    dimensions = normalized.get("dimension_scores")
    if isinstance(dimensions, list):
        by_dimension: dict[str, Any] = {}
        for item in dimensions:
            if not isinstance(item, dict):
                return normalized
            dimension = item.get("dimension")
            if not isinstance(dimension, str) or dimension in by_dimension:
                return normalized
            by_dimension[dimension] = {
                key: value for key, value in item.items() if key != "dimension"
            }
        normalized["dimension_scores"] = by_dimension
    if isinstance(normalized.get("dimension_scores"), dict):
        # 模型偶尔把根级质量结论错嵌到评分对象。字段名与目标位置均唯一，
        # 因而可无损上提；必须先移除，避免评分对象 additionalProperties 拒绝。
        nested_assessment = normalized["dimension_scores"].pop("quality_assessment", None)
        if not isinstance(normalized.get("quality_assessment"), dict) and isinstance(
            nested_assessment, dict
        ):
            normalized["quality_assessment"] = nested_assessment
        # “质量评级”与 quality_assessment 是同一展示概念。模型偶尔把前者
        # 放进 dimension_scores；根级 quality_assessment 已存在时它完全冗余，
        # 必须移除，避免固定维度对象被 additionalProperties 拒绝。
        nested_grade = normalized["dimension_scores"].pop("质量评级", None)
        if not isinstance(normalized.get("quality_assessment"), dict) and isinstance(
            nested_grade, dict
        ):
            normalized["quality_assessment"] = _normalize_quality_assessment(nested_grade)
        normalized["dimension_scores"] = _normalize_dimension_names(normalized["dimension_scores"])
        _complete_review_score_and_grade(normalized)

    for alias in ("总体评价", "总体结论", "评审摘要", "总结"):
        if not isinstance(normalized.get("summary"), str) and isinstance(
            normalized.get(alias), str
        ):
            normalized["summary"] = normalized[alias]
        normalized.pop(alias, None)

    # 这些字段的持久化契约均为字符串数组。模型有时会返回 issue 对象或主题树；
    # 将其压平为保留层级语义的可读文本，不补造任何业务内容。
    for field in _STRING_LIST_FIELDS:
        values = normalized.get(field)
        if isinstance(values, list):
            normalized[field] = [_normalize_review_text_item(item) for item in values]

    # 思维导图叶子被简写为字符串时，恢复为同义的叶节点对象。
    if "mindmap" in normalized:
        normalized["mindmap"] = _normalize_mindmap_node(normalized["mindmap"])
    # MiniMax 有时把 issue.reason 省略为“说明/依据”，或只给出问题本身。
    # 前者无损映射；后者明确标注待人工补充，避免 schema 拒绝整个评审产物。
    issues = normalized.get("issues")
    if isinstance(issues, list):
        normalized["issues"] = [_normalize_issue(item) for item in issues]
    _cap_review_collections(normalized)
    return normalized


def _normalize_quality_assessment(value: dict[str, Any]) -> dict[str, Any]:
    """归一“质量评级”这一明确别名；只转换字段名，不猜测业务评级。"""
    assessment = dict(value)
    aliases = {"评级": "grade", "等级": "grade", "等级名称": "grade_name", "说明": "summary_reason"}
    for source, target in aliases.items():
        if target not in assessment and source in assessment:
            assessment[target] = assessment.pop(source)
    return assessment


def _cap_review_collections(review: dict[str, Any]) -> None:
    """持久化 Schema 有硬上限时程序化截断，杜绝长文档因一条超限整体失败。"""
    limits = {
        "issues": 20,
        "readable_issue_points": 10,
        "completeness_issues": 20,
        "consistency_issues": 20,
        "untestable_items": 20,
        "risk_flags": 10,
        "testable_features": 30,
    }
    for field, limit in limits.items():
        values = review.get(field)
        if not isinstance(values, list):
            continue
        # 按模型给出的既有优先级保留前 N 条；同文本去重，避免重复项挤占配额。
        compact: list[Any] = []
        seen: set[str] = set()
        for value in values:
            marker = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if marker not in seen:
                compact.append(value)
                seen.add(marker)
            if len(compact) == limit:
                break
        review[field] = compact


def _complete_review_score_and_grade(review: dict[str, Any]) -> None:
    """维度齐全且含数值分时，程序化补回模型遗漏的总分与等级。"""
    dimensions = review.get("dimension_scores")
    if not isinstance(dimensions, dict):
        return
    scores: list[tuple[float, int]] = []
    for name, default_weight in _DIMENSION_WEIGHTS.items():
        item = dimensions.get(name)
        if not isinstance(item, dict) or not isinstance(item.get("score"), int):
            return
        item.setdefault("weight", default_weight)
        scores.append((float(item["weight"]), item["score"]))
    score = round(sum(weight * value for weight, value in scores))
    review.setdefault("score", score)
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D"
    review.setdefault("quality_grade", grade)
    assessment = review.get("quality_assessment")
    if isinstance(assessment, dict):
        assessment.setdefault("grade", review["quality_grade"])


def _normalize_issue(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    issue = dict(item)
    if not isinstance(issue.get("reason"), str) or not issue["reason"].strip():
        for alias in ("原因", "说明", "依据", "描述", "analysis"):
            if isinstance(issue.get(alias), str) and issue[alias].strip():
                issue["reason"] = issue.pop(alias)
                break
        else:
            issue["reason"] = "模型未提供原因，需人工补充"
    return issue


def _normalize_review_text_item(item: Any) -> Any:
    """把评审字符串数组中的问题对象/主题树无损压平为单条可读文本。"""
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return item

    topic = item.get("主题")
    children = item.get("子主题")
    if isinstance(topic, str):
        if isinstance(children, list):
            child_texts = [
                text
                for child in children
                if isinstance((text := _normalize_review_text_item(child)), str) and text
            ]
            if child_texts:
                return f"{topic}：{'；'.join(child_texts)}"
        return topic

    # issues/readable_issue_points 常见对象形态，按面向用户的字段优先级取值。
    for key in ("question", "desc", "description", "reason", "title", "name"):
        if isinstance(item.get(key), str) and item[key]:
            return item[key]
    return item


def _normalize_dimension_names(dimensions: dict[str, Any]) -> dict[str, Any]:
    """归一维度名，并隔离模型附带的 ``*_error`` 诊断字段。

    MiniMax 偶尔在成功给出正式维度后，额外给出同名 ``*_error`` 字段，作为它
    自己的解析提示；这不是业务评分的一部分，若直接落盘会触发契约的
    ``additionalProperties``。仅丢弃这类明确的诊断副本，其他未知维度仍交由
    Schema 拒绝，避免静默吞掉真实结构错误。
    """
    normalized: dict[str, Any] = {}
    for name, value in dimensions.items():
        if name.endswith("_error"):
            candidate = name.removesuffix("_error")
            canonical_candidate = _DIMENSION_ALIASES.get(candidate, candidate)
            if canonical_candidate in _DIMENSION_WEIGHTS:
                continue
        canonical = _DIMENSION_ALIASES.get(name, name)
        if canonical in normalized:
            return dimensions  # 别名与全名并存时不猜，交给 Schema 拒绝。
        if isinstance(value, dict) and "reason" not in value:
            entry = dict(value)
            for alias in ("理由", "说明", "评价", "评估理由"):
                if isinstance(entry.get(alias), str):
                    entry["reason"] = entry[alias]
                    entry.pop(alias, None)
                    break
            normalized[canonical] = entry
        else:
            normalized[canonical] = value
    return normalized


def _normalize_mindmap_node(node: Any) -> Any:
    """将叶子字符串转为 Schema 所需的 ``{主题, 子主题: []}``。"""
    if isinstance(node, str):
        return {"主题": node, "子主题": []}
    if not isinstance(node, dict):
        return node
    normalized = dict(node)
    children = normalized.get("子主题")
    if isinstance(children, list):
        normalized["子主题"] = [_normalize_mindmap_node(child) for child in children]
    return normalized


def _strip_code_fence(text: str) -> str:
    """剥掉围栏，并从模型附带的思考/解释中提取一个完整 JSON 对象。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        # 去首行围栏（``` 或 ```json）
        first_newline = stripped.find("\n")
        if first_newline == -1:
            return stripped.removeprefix("```").strip()
        body = stripped[first_newline + 1 :]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        stripped = body.strip()
    return _extract_complete_json_object(stripped)


def _extract_complete_json_object(text: str) -> str:
    """返回文本中的首个完整 JSON 对象；找不到时保留原文交由 json.loads 报错。

    仅接受花括号、字符串转义和嵌套结构都闭合的对象，不拼接或猜测残缺 JSON。
    整体就是完整 JSON 数组时原样返回（裸数组是已登记容器形态，
    截取首个元素会丢失其余条目）。
    """
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            whole = json.loads(stripped)
        except json.JSONDecodeError:
            whole = None
        if isinstance(whole, list):
            return stripped
    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        quoted = False
        escaped = False
        for end in range(start, len(text)):
            current = text[end]
            if quoted:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    quoted = False
                continue
            if current == '"':
                quoted = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : end + 1]
                    try:
                        data = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(data, dict):
                        return candidate
                    break
    return text
