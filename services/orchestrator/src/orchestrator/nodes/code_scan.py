"""code_scan 节点：扫描多仓库 → code_inventory.json 落盘（F-CODE-01~03）。

C-13 语言分流：Java 仓库走 java-parser；Python 仓库走内置 AST 扫描器
（python_scan，同口径产出）。仓库四态口径（X-CODE-01~03）不变：
- error（X-CODE-01）→ 节点失败（客户端已透传契约错误码）；
- empty（X-CODE-02）→ 正常态，流程继续，如实记入产物；
- truncated（X-CODE-03）→ 正常态，截断范围记入 truncated_scope；
- ok → 正常。
全 error 兕底：java-parser 侧失败由 X-ORCH-03 表达。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from common.artifacts import ArtifactStore
from common.engine import Engine, JobState
from common.errors import X_ORCH_01, PipelineError

from orchestrator.javaparser_client import JavaParserClient, RepositoryInput
from orchestrator.python_scan import scan_repository
from orchestrator.repository_resolver import workspace_scope_warnings

logger = logging.getLogger(__name__)

NODE_NAME = "code_scan"

# code_scan 前置产物无（req_parse 之后第二节点，产物判据由 Engine 把关）


async def run_code_scan(
    engine: Engine,
    state: JobState,
    store: ArtifactStore,
    client: JavaParserClient,
    repositories: list[RepositoryInput],
) -> Path:
    """执行 code_scan 全流程：跳序检查 → 分语言扫描 → 校验落盘 → 置完成。

    Raises:
        PipelineError: X-CODE-01（仓库硬错误，透传乙段码）或 X-ORCH-03（通信失败）。
    """
    engine.check_runnable(state, NODE_NAME)
    engine.mark_running(state, NODE_NAME)
    try:
        java_repos = [r for r in repositories if r.language != "python"]
        python_repos = [r for r in repositories if r.language == "python"]
        repository_entries: list[dict[str, Any]] = []
        if java_repos:
            response = await client.scan(state.job_id, java_repos)
            inventory = _extract_inventory(response, state.job_id)
            repository_entries.extend(inventory.get("repositories", []))
        for repo in python_repos:  # C-13：Python 内置 AST 扫描（同四态口径）
            repository_entries.append(scan_repository(repo).inventory_entry)
        inventory = {
            "schema_version": "1.0",
            "job_id": state.job_id,
            "repositories": repository_entries,
        }
        _check_repo_errors(inventory)
        # 工作区与配置分支脱节时如实告警：接口候选是扫工作区得到的，扫错分支会静默少接口。
        scope_warnings = workspace_scope_warnings(repositories)
        if scope_warnings:
            inventory["warnings"] = scope_warnings
            for message in scope_warnings:
                logger.warning("%s", message)
        path = store.write(state.job_id, "code_inventory.json", inventory)
        engine.mark_completed(state, NODE_NAME)
        return path
    except PipelineError as exc:
        engine.mark_failed(state, NODE_NAME, exc)
        raise


def _extract_inventory(response: dict[str, Any], job_id: str) -> dict[str, Any]:
    """从 ScanJob 响应提取 code_inventory，并钉死 job_id 归属（防串档）。

    Raises:
        PipelineError: X-ORCH-01——响应缺 code_inventory（契约不符，宁停不猜）。
    """
    inventory = response.get("code_inventory")
    if not isinstance(inventory, dict):
        raise PipelineError(
            code=X_ORCH_01,
            message="java-parser 响应缺 code_inventory 产物",
            node_id=NODE_NAME,
        )
    # 归属校验：产物 job_id 必须与本流水线一致（防串档/串任务）
    if inventory.get("job_id") not in (None, job_id):
        raise PipelineError(
            code=X_ORCH_01,
            message=f"code_inventory 的 job_id 不匹配：{inventory.get('job_id')} != {job_id}",
            node_id=NODE_NAME,
        )
    result = dict(inventory)
    result["job_id"] = job_id
    result.setdefault("schema_version", "1.0")
    return result


def _check_repo_errors(inventory: dict[str, Any]) -> None:
    """仓库级硬错误检查：任一 status=error（X-CODE-01）即节点失败。

    empty / truncated 为正常态（如实落盘，流程继续）。
    """
    failed_repos = [
        repo
        for repo in inventory.get("repositories", [])
        if repo.get("status") == "error"
    ]
    if failed_repos:
        error_details: list[dict[str, Any]] = []
        summaries: list[str] = []
        for repo in failed_repos:
            name = str(repo.get("name", "<未命名>"))
            error = repo.get("error") if isinstance(repo.get("error"), dict) else {}
            code = str(error.get("code", ""))
            message = " ".join(str(error.get("message", "")).split())
            entry: dict[str, Any] = {"name": name}
            if code:
                entry["code"] = code
            if message:
                entry["message"] = message
            error_details.append(entry)
            summary = name
            if code:
                summary += f" [{code}]"
            if message:
                summary += f"：{message[:600]}"
            summaries.append(summary)
        raise PipelineError(
            code="X-CODE-01",
            message=f"仓库扫描硬错误（X-CODE-01）：{'; '.join(summaries)}",
            node_id=NODE_NAME,
            details={"failed_repositories": error_details},
        )
