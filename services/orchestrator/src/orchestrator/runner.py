"""流水线全流程串联（F-ORCH-01 单命令跑完 / F-ORCH-02 断点续跑）。

职责：按 9 节点固定顺序驱动；逐节点失败治理（重试耗尽降级 + 继续或终止）；
产物为唯一事实——状态经产物判据在 open_job 时自动补齐。

驱动纪律（docs/12 §1）：
- 重试耗尽（默认 2 次）→ mark_degraded（manual_intervention + D1 降级清单，
  分母不缩减），不中断全流程（D1：降级节点视同已处理不阻塞下游）；
- X-ORCH-02 跳序防护仍在（顺序表驱动天然满足，人为乱序仍会被拦）；
- 失败/降级逐节点汇总到 PipelineOutcome（CLI 打印 + 报告数据源）。

单体过渡期说明：丙泳道节点（GEN/EXEC/DEF/REG）当前由编排进程内驱动，
经 te_store（test-executor 身份）写产物；服务拆分后随迁 test-executor。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.artifacts import ArtifactStore
from orchestrator.config import Settings
from orchestrator.engine import NODE_ARTIFACTS, Engine, JobState
from orchestrator.errors import PipelineError
from orchestrator.javaparser_client import JavaParserClient, RepositoryInput
from orchestrator.llm.client import LlmClient
from orchestrator.nodes import (
    run_auto_execute,
    run_code_review,
    run_code_scan,
    run_defect_list,
    run_interface_reconstruct,
    run_req_parse,
    run_retest_regression,
    run_test_report,
    run_testcase_generate,
)
from orchestrator.nodes.test_report import build_final_metrics
from orchestrator.pipeline import NODE_ORDER, NodeStatus

logger = logging.getLogger(__name__)

MAX_NODE_ATTEMPTS = 2  # 1 次首发 + 1 次重试（LLM 调用内部另有网络级重试）


@dataclass
class PipelineOutcome:
    """一次全流程运行的结果汇总（CLI 输出与报告数据源）。"""

    job_id: str
    completed: list[str] = field(default_factory=list)
    degraded: list[tuple[str, str]] = field(default_factory=list)  # （节点名, 原因）
    failed: list[tuple[str, str]] = field(default_factory=list)  # （节点名, 错误码+原因）
    artifacts_dir: Path | None = None

    @property
    def ok(self) -> bool:
        """无 failed 且有产物（降级不阻断验收演示口径 D1）。"""
        return not self.failed and self.artifacts_dir is not None


class PipelineRunner:
    """9 节点串联驱动器（CLI 与 API 共用）。"""

    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        orch_store: ArtifactStore,
        te_store: ArtifactStore,
        java_client: JavaParserClient,
    ) -> None:
        """双身份产物卷：甲/乙产物 orchestrator 写，丙产物 test-executor 写。"""
        self._settings = settings
        self._engine = engine
        self._orch_store = orch_store
        self._te_store = te_store
        self._java_client = java_client

    async def run(
        self,
        job_id: str,
        doc_path: Path,
        repositories: list[RepositoryInput],
        rest_base_url: str,
        force_node: str | None = None,
    ) -> PipelineOutcome:
        """按固定顺序驱动 9 节点（F-ORCH-01）。

        state.json 已存在时经产物判据续跑（F-ORCH-02）；
        force_node：重跑已完成节点（旧产物先移入 archive/，禁止原地覆盖——留痕红线）。
        """
        state = self._open_or_init(job_id)
        if force_node is not None:
            self._archive_node_artifacts(state, force_node)
        llm = LlmClient(
            settings=self._settings,
            job_id=job_id,
            trace_path=self._orch_store.job_dir(job_id) / "llm_calls.jsonl",
        )
        prompts_dir = Path(__file__).resolve().parents[4] / "prompts"
        outcome = PipelineOutcome(job_id=job_id)

        for node_name in NODE_ORDER:
            status = state.node(node_name).status
            if status is NodeStatus.COMPLETED:
                outcome.completed.append(node_name)  # 断点续跑：跳过已完成
                logger.info("节点 %s 已完成（续跑跳过）", node_name)
                continue
            if status is NodeStatus.MANUAL_INTERVENTION:
                outcome.degraded.append((node_name, "前次运行降级，等待人工介入"))
                continue

            await self._run_node_with_retry(
                node_name, state, llm, prompts_dir, doc_path, repositories, rest_base_url, outcome
            )

        self._refresh_final_metrics(state)
        outcome.artifacts_dir = self._orch_store.job_dir(job_id)
        return outcome

    def _refresh_final_metrics(self, state: JobState) -> None:
        """收尾刷新 metrics.json 为九节点终态版（docs/13 P-10）。

        仅当流水线已到达末尾节点（无 pending）时刷新——避免早期中断的 job
        凭 metrics.json 被产物判据误判 test_report 已完成（X-ORCH 判据口径）。
        """
        if any(state.node(name).status is NodeStatus.PENDING for name in NODE_ORDER):
            return
        self._orch_store.write(state.job_id, "metrics.json", build_final_metrics(state))
        logger.info("metrics.json 已刷新为终态版（job %s）", state.job_id)

    async def _run_node_with_retry(
        self,
        node_name: str,
        state: JobState,
        llm: LlmClient,
        prompts_dir: Path,
        doc_path: Path,
        repositories: list[RepositoryInput],
        rest_base_url: str,
        outcome: PipelineOutcome,
    ) -> None:
        """单节点治理：LLM 类失败重试一次，再失败或非 LLM 类失败 → 降级并继续（D1）。"""
        attempts = state.node(node_name).attempts
        while attempts < MAX_NODE_ATTEMPTS:
            try:
                await self._invoke_node(
                    node_name, state, llm, prompts_dir, doc_path, repositories, rest_base_url
                )
                outcome.completed.append(node_name)
                return
            except PipelineError as exc:
                attempts += 1
                logger.warning(
                    "节点 %s 第 %d 次尝试失败：%s", node_name, attempts, exc.message
                )
                # 设计 §1.3：节点级自动重试仅 LLM 调用类失败（网络级重试在 LlmClient 内）
                if not exc.code.startswith("X-LLM-"):
                    attempts = MAX_NODE_ATTEMPTS
                if attempts >= MAX_NODE_ATTEMPTS:
                    # 重试耗尽 → 降级（manual_intervention + 降级清单，D1）
                    reason = f"{exc.code}：{exc.message}"
                    self._engine.mark_degraded(state, node_name, reason)
                    outcome.degraded.append((node_name, reason))
                    return

    async def _invoke_node(
        self,
        node_name: str,
        state: JobState,
        llm: LlmClient,
        prompts_dir: Path,
        doc_path: Path,
        repositories: list[RepositoryInput],
        rest_base_url: str,
    ) -> None:
        """分派到对应节点实现（甲/乙产物走 orch_store，丙产物走 te_store）。"""
        if node_name == "req_parse":
            await run_req_parse(
                self._engine, state, self._orch_store, llm, prompts_dir, doc_path
            )
        elif node_name == "code_scan":
            await run_code_scan(
                self._engine, state, self._orch_store, self._java_client, repositories
            )
        elif node_name == "interface_reconstruct":
            await run_interface_reconstruct(
                self._engine,
                state,
                self._orch_store,
                self._java_client,
                llm,
                prompts_dir,
                repositories,
            )
        elif node_name == "code_review":
            await run_code_review(self._engine, state, self._orch_store, llm, prompts_dir)
        elif node_name == "testcase_generate":
            await run_testcase_generate(
                self._engine, state, self._te_store, llm, prompts_dir
            )
        elif node_name == "auto_execute":
            await run_auto_execute(
                self._engine, state, self._te_store, rest_base_url
            )
        elif node_name == "test_report":
            await run_test_report(self._engine, state, self._orch_store)
        elif node_name == "defect_list":
            await run_defect_list(self._engine, state, self._te_store)
        elif node_name == "retest_regression":
            await run_retest_regression(
                self._engine, state, self._te_store, rest_base_url
            )
        else:  # pragma: no cover - NODE_ORDER 固定 9 项，防御性兜底
            raise PipelineError(
                code="X-ORCH-01", message=f"未知节点：{node_name}", node_id=node_name
            )

    def _open_or_init(self, job_id: str) -> JobState:
        """打开既有 job（断点续跑）或新建。"""
        state_path = self._orch_store.job_dir(job_id) / "state.json"
        if state_path.is_file():
            return self._engine.open_job(job_id)
        return self._engine.init_job(job_id)

    def _archive_node_artifacts(self, state: JobState, node_name: str) -> None:
        """force-node 前置：旧产物移入 archive/<node>/，节点状态回 pending。"""
        if node_name not in NODE_ARTIFACTS:
            raise PipelineError(
                code="X-ORCH-01", message=f"未知节点：{node_name}", node_id=node_name
            )
        job_dir = self._orch_store.job_dir(state.job_id)
        archive_dir = job_dir / "archive" / node_name
        archive_dir.mkdir(parents=True, exist_ok=True)
        for filename in NODE_ARTIFACTS[node_name]:
            source = job_dir / filename
            if source.is_file():
                shutil.move(str(source), str(archive_dir / filename))
        node = state.node(node_name)
        node.status = NodeStatus.PENDING
        self._engine._save(state)  # 归档后立即持久化


def summarize_outcome(outcome: PipelineOutcome) -> str:
    """CLI 汇总输出（人读，数值程序拼接）。"""
    lines = [
        f"流水线运行完成：{outcome.job_id}",
        f"产物目录：{outcome.artifacts_dir}",
        f"完成节点：{len(outcome.completed)}/{len(NODE_ORDER)}",
    ]
    if outcome.degraded:
        lines.append("降级节点（D1：分母不缩减，报告分列）：")
        lines += [f"  - {name}：{reason}" for name, reason in outcome.degraded]
    if outcome.failed:
        lines.append("失败节点：")
        lines += [f"  - {name}：{reason}" for name, reason in outcome.failed]
    return "\n".join(lines)


def repository_inputs_from_config(
    repositories: list[dict[str, Any]],
) -> list[RepositoryInput]:
    """任务配置（JSON/CLI 参数）→ RepositoryInput 列表。

    相对路径按当前工作目录解析为绝对路径（java-parser 侧按绝对路径定位仓库）。
    """
    inputs: list[RepositoryInput] = []
    for repo in repositories:
        path = Path(str(repo["path"]))
        if not path.is_absolute():
            path = Path.cwd() / path
        inputs.append(
            RepositoryInput(
                name=str(repo["name"]),
                path=str(path),
                branch=str(repo.get("branch", "main")),
                is_fallback=bool(repo.get("is_fallback", False)),
                include_paths=tuple(repo.get("include_paths", ())),
            )
        )
    return inputs
