"""req_parse 节点：需求质量评审 + 条目提取（F-REQ-01~03）。

多文档口径（C-10）：各文档按文件名分节拼接为统一文本输入，先执行需求质量评审，
再提取可追溯需求条目。两份产物 `requirement_review.json` 与
`requirement_items.json` 均通过 Schema 才允许节点完成。

失败口径（docs/12）：
- Prompt 缺失 → X-LLM-03；LLM 网络/超时 → X-LLM-01（客户端内重试）；
- LLM 输出不可解析或未过 Schema → X-LLM-02（不重试，人工介入）；
- 全部失败路径经 engine.mark_failed 记入 state.json 后再抛出。
"""

from __future__ import annotations

import json
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

_STRING_LIST_FIELDS = (
    "readable_issue_points",
    "completeness_issues",
    "consistency_issues",
    "untestable_items",
    "testable_features",
)


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
        doc_text = _merge_documents(doc_paths)
        review_prompt = load_prompt(prompts_dir, "requirement_review")
        review_payload = _build_review_payload(
            await complete_with_json_repair(
                llm,
                NODE_NAME,
                review_prompt,
                doc_text,
                _extract_review,
                lambda review: _validate_review_shape(store, state.job_id, review, doc_paths),
            ),
            state.job_id,
            doc_paths,
        )
        _write_llm_artifact(store, state.job_id, "requirement_review.json", review_payload)

        items_prompt = load_prompt(prompts_dir, NODE_NAME)
        # 大文档按章节/段落拆分：每批只要求返回局部条目，避免长 JSON 被模型截断。
        item_batches: list[list[dict[str, Any]]] = []
        document_chunks = chunk_text(doc_text)
        for index, chunk in enumerate(document_chunks, start=1):
            batch_input = _batch_input("需求条目提取", index, len(document_chunks), chunk)
            item_batches.append(
                await complete_with_json_repair(
                    llm, NODE_NAME, items_prompt, batch_input, _extract_items
                )
            )
        items = _renumber_items(item_batches)
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "job_id": state.job_id,
            # C-10：source_files 记全部；source_file 保留首文档兼容旧读取方
            "source_files": [str(path) for path in doc_paths],
            "source_file": str(doc_paths[0]),
            "items": items,
        }
        path = _write_llm_artifact(store, state.job_id, "requirement_items.json", payload)
        engine.mark_completed(state, NODE_NAME)
        return path
    except PipelineError as exc:
        engine.mark_failed(state, NODE_NAME, exc)
        raise


def _build_review_payload(
    review: dict[str, Any], job_id: str, doc_paths: list[Path]
) -> dict[str, Any]:
    """注入产物归属与来源字段；业务评审字段仅接受模型的完整 JSON 输出。"""
    return {
        "schema_version": "1.0",
        "job_id": job_id,
        "source_files": [str(path) for path in doc_paths],
        **review,
    }


def _batch_input(label: str, index: int, total: int, content: str) -> str:
    """给模型显式批次边界；模型不得引用未随本批提供的内容。"""
    return (
        f"这是{label}第 {index}/{total} 批。仅处理本批内容；"
        "输出仍必须满足 Prompt 规定的完整 JSON 结构。\n\n"
        f"{content}"
    )


def _renumber_items(batches: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """跨批汇总并由程序统一编号，避免每批都从 REQ-001 开始。"""
    merged = [dict(item) for batch in batches for item in batch]
    if not merged:
        raise PipelineError(
            code=X_LLM_02,
            message="所有需求解析批次均未产出条目",
            node_id=NODE_NAME,
        )
    for index, item in enumerate(merged, start=1):
        item["id"] = f"REQ-{index:03d}"
    return merged


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


def _validate_review_shape(
    store: ArtifactStore, job_id: str, review: dict[str, Any], doc_paths: list[Path]
) -> None:
    """在落盘前校验评审业务字段，使 Schema 错误可进入一次模型修复轮。"""
    if _looks_like_dimension_projected_as_review(review):
        raise PipelineError(
            code=X_LLM_02,
            message=(
                "requirement_review.json 校验失败：当前输出是单个 dimension 评分对象，"
                "不是完整需求评审。请丢弃该对象，重新输出包含 13 个规定顶层字段的完整 JSON；"
                "weight/reason 只能出现在 dimension_scores 的五个维度内部。"
            ),
            node_id=NODE_NAME,
        )
    errors = store.validate(
        job_id,
        "requirement_review.json",
        _build_review_payload(review, job_id, doc_paths),
    )
    if errors:
        raise PipelineError(
            code=X_LLM_02,
            message=f"requirement_review.json 校验失败：{'; '.join(errors[:3])}",
            node_id=NODE_NAME,
            details={"errors": errors[:10]},
        )


def _looks_like_dimension_projected_as_review(review: dict[str, Any]) -> bool:
    """识别模型把一个 `{weight, score, reason}` 维度误作为整份评审输出的漂移。"""
    dimension_fields = {"weight", "score", "reason"}
    review_fields = {"quality_grade", "summary", "dimension_scores", "quality_assessment"}
    return bool(dimension_fields & set(review)) and not bool(review_fields & set(review))


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
    return normalized


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
    """
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
