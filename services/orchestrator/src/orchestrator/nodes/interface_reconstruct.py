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
- 零候选 → X-REV-04（候选数低于可运行底线）；<5 候选 → warning 如实记录
  （X-REV-04 弱口径）；
- LLM 输出不可解析/引用虚构候选/未过 Schema → X-LLM-02；
- java-parser 通信失败 → X-ORCH-03；契约错误 → 透传。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

from orchestrator.artifacts import ArtifactStore
from orchestrator.engine import Engine, JobState
from orchestrator.errors import X_LLM_02, X_ORCH_01, PipelineError
from orchestrator.javaparser_client import JavaParserClient, RepositoryInput
from orchestrator.llm import LlmResult
from orchestrator.nodes.req_parse import _strip_code_fence
from orchestrator.prompts_loader import PromptSpec, load_prompt

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
        response = await client.extract(state.job_id, repositories, protocols)
        candidates, warnings = _normalize_candidates(response)

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

        # 第②步：LLM 语义解析（候选含 snippet 上下文）
        prompt = load_prompt(prompts_dir, NODE_NAME)
        result = await llm.complete(
            NODE_NAME, prompt, json.dumps(candidates, ensure_ascii=False)
        )
        interfaces = _merge_llm_output(result.text, candidates)

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
    cleaned = _strip_code_fence(text)
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
    if not interfaces:
        raise PipelineError(
            code=X_LLM_02,
            message="LLM 输出的 interfaces 为空（候选均未产出条目）",
            node_id=NODE_NAME,
        )
    return interfaces
