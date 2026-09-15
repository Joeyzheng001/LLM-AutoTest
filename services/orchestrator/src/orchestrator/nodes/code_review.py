"""code_review 节点：三视图产出（F-REVW-01~05）——一次 LLM 调用产映射表，
风险清单与遗漏功能清单程序化派生（R-REVW-02：与映射表 100% 一致）。

产物：review_mapping.json（Day1 冻结口径：三视图合一单产物；
R-REVW-03 的三文件名是规格草案表述，以 schemas/ 冻结文件名为准）。

程序化派生（数值/清单不经 LLM）：
- risks：status=partial 自动进入（R-REVW-04：实现不完整即风险）；
- missing_features：status=missing 条目提取，title 从需求条目回填；
- evidence.code_location：从 interface_id 对应接口回填（LLM 给的忽略）。

校验（R-REVW-01 覆盖率 100% + 虚构防护）：
- 每条输入 REQ 必须有且仅一个映射条目，缺/多/虚构 REQ → X-LLM-02；
- interface_id 必须存在于 interfaces.json → 虚构即 X-LLM-02。

失败口径（docs/12）：
- LLM 输出不可解析/未过 Schema → X-LLM-02；前置产物缺失/不合规 → X-ORCH-01。
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

NODE_NAME = "code_review"

_VALID_STATUSES = ("implemented", "partial", "missing", "undetermined")


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

        prompt = load_prompt(prompts_dir, NODE_NAME)
        user_content = json.dumps(
            {"requirements": requirements, "interfaces": interfaces},
            ensure_ascii=False,
        )
        result = await llm.complete(NODE_NAME, prompt, user_content)
        mappings = _merge_llm_mappings(result.text, requirements, interfaces)

        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "job_id": state.job_id,
            "mappings": mappings,
            "risks": _derive_risks(mappings),
            "missing_features": _derive_missing(mappings, requirements),
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


def _derive_risks(mappings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """视图二（R-REVW-04）：partial 自动进入风险清单（程序派生）。"""
    risks: list[dict[str, Any]] = []
    for entry in mappings:
        if entry["status"] != "partial":
            continue
        risk: dict[str, Any] = {
            "requirement_id": entry["requirement_id"],
            "description": "部分实现（R-REVW-04）：实现不完整即风险，须复核覆盖缺口",
        }
        if entry.get("interface_id"):
            risk["interface_id"] = entry["interface_id"]
        note = (entry.get("evidence") or {}).get("note")
        if note:
            risk["description"] += f"；LLM 判定依据：{note}"
        risks.append(risk)
    return risks


def _derive_missing(
    mappings: list[dict[str, Any]], requirements: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """视图三（R-REVW-02）：提取 missing 条目，title 从需求回填（程序派生）。"""
    title_by_id: dict[str, str] = {
        str(item["id"]): str(item.get("title", ""))
        for item in requirements
        if item.get("id")
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
