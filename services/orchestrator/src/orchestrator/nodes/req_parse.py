"""req_parse 节点：需求文档 → LLM 解析 → requirement_items.json 落盘（F-REQ-01~03）。

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
from orchestrator.prompts_loader import PromptSpec, load_prompt

NODE_NAME = "req_parse"


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
    doc_path: Path,
) -> Path:
    """执行 req_parse 全流程：跳序检查 → 运行 → 解析 → 校验落盘 → 置完成。

    Returns:
        产物文件路径。

    Raises:
        PipelineError: 各失败口径见模块 docstring。
    """
    engine.check_runnable(state, NODE_NAME)
    engine.mark_running(state, NODE_NAME)
    try:
        prompt = load_prompt(prompts_dir, NODE_NAME)
        doc_text = load_document_text(doc_path)
        result = await llm.complete(NODE_NAME, prompt, doc_text)
        items = _extract_items(result.text)
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "job_id": state.job_id,
            "source_file": str(doc_path),
            "items": items,
        }
        try:
            path = store.write(state.job_id, "requirement_items.json", payload)
        except PipelineError as exc:
            # 源头是 LLM 输出不合规：归 X-LLM-02（错误码语义归位，非表外新增）
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
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise PipelineError(
            code=X_LLM_02,
            message="LLM 输出缺少 items 数组（或结构不符）",
            node_id=NODE_NAME,
        )
    items = data["items"]
    if not all(isinstance(item, dict) for item in items):
        raise PipelineError(
            code=X_LLM_02,
            message="LLM 输出的 items 含非对象条目",
            node_id=NODE_NAME,
        )
    return [dict(item) for item in items]


def _strip_code_fence(text: str) -> str:
    """剥掉 ```json 围栏（仅容错围栏本身，围栏内内容必须完整）。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        # 去首行围栏（``` 或 ```json）
        first_newline = stripped.find("\n")
        if first_newline == -1:
            return stripped.removeprefix("```").strip()
        body = stripped[first_newline + 1 :]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        return body.strip()
    return stripped
