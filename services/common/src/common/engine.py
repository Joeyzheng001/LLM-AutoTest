"""编排引擎：state.json 持久化、断点续跑、跳序防护、降级清单（docs/12 第一章）。

核心原则：
- **产物是唯一事实**：state.json 里的 completed 只是对产物状态的缓存；
  每次加载都对账（reconcile）——产物在而状态不在则补齐，状态在而产物失效则降级回 pending；
- 跳序防护：请求运行某节点时，其前序节点必须全部 completed（X-ORCH-02）；
- 降级节点按 manual_intervention 计入（D1：分母固定 9 不缩减），降级原因进 degraded 清单。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common.artifacts import ArtifactStore
from common.errors import X_ORCH_01, X_ORCH_02, X_ORCH_04, PipelineError
from common.pipeline import NODE_ORDER, NodeStatus

_STATE_VERSION = "1.0"

# docs/12 §1.1 节点完成判据（产物文件名）；interface_reconstruct 需两份产物同时合规
NODE_ARTIFACTS: dict[str, tuple[str, ...]] = {
    # req_parse 内含「需求质量评审 + 条目提取」两个受控子阶段；两份产物均合规才完成。
    "req_parse": ("requirement_review.json", "requirement_items.json"),
    "code_scan": ("code_inventory.json",),
    "interface_reconstruct": (
        "interface_candidates.json",
        "interfaces.json",
        "change_set.json",
    ),
    "code_review": ("review_mapping.json",),
    "testcase_generate": ("testcases.json",),
    "auto_execute": ("execution.json",),
    "test_report": ("test_report.md", "metrics.json"),
    "defect_list": ("defects.json",),
    "retest_regression": ("regression_index.json",),
}


def _now() -> str:
    """UTC ISO-8601 时间戳（横切约定）。"""
    return datetime.now(UTC).isoformat()


@dataclass
class NodeState:
    """单节点运行时状态（state.json 的 nodes 数组元素）。"""

    name: str
    status: NodeStatus = NodeStatus.PENDING
    updated_at: str = field(default_factory=_now)
    attempts: int = 0
    error: dict[str, Any] | None = None  # 最近一次失败（{code, message, details?}）

    def to_dict(self) -> dict[str, Any]:
        """序列化（error 仅在存在时出现）。"""
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status.value,
            "updated_at": self.updated_at,
            "attempts": self.attempts,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass
class JobState:
    """单条流水线的完整状态（state.json 根对象）。"""

    job_id: str
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    nodes: tuple[NodeState, ...] = field(default_factory=tuple)
    degraded: tuple[dict[str, Any], ...] = ()  # {node, reason, marked_at}（D5 报告分列依据）
    resumed: int = 0  # 断点续跑次数（对账发生次数）

    def __post_init__(self) -> None:
        """新 job 初始化九节点全 pending。"""
        if not self.nodes:
            self.nodes = tuple(NodeState(name=n) for n in NODE_ORDER)

    def node(self, name: str) -> NodeState:
        """按名取节点状态。"""
        return next(n for n in self.nodes if n.name == name)

    def to_dict(self) -> dict[str, Any]:
        """序列化 state.json。"""
        return {
            "schema_version": _STATE_VERSION,
            "job_id": self.job_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "nodes": [n.to_dict() for n in self.nodes],
            "degraded": list(self.degraded),
            "resumed": self.resumed,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> JobState:
        """反序列化；结构不符按 X-ORCH-01 拒绝（宁停不猜）。"""
        try:
            nodes = tuple(
                NodeState(
                    name=item["name"],
                    status=NodeStatus(item["status"]),
                    updated_at=item["updated_at"],
                    attempts=item.get("attempts", 0),
                    error=item.get("error"),
                )
                for item in payload["nodes"]
            )
            return cls(
                job_id=payload["job_id"],
                created_at=payload["created_at"],
                updated_at=payload["updated_at"],
                nodes=nodes,
                degraded=tuple(payload.get("degraded", [])),
                resumed=payload.get("resumed", 0),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise PipelineError(
                code=X_ORCH_01,
                message=f"state.json 结构不合规：{exc}",
            ) from exc

    @property
    def automation_rate(self) -> float | None:
        """自动化率 = completed / 9（F-MTR-03 程序化汇总；无实测返回 None）。"""
        completed = sum(1 for n in self.nodes if n.status is NodeStatus.COMPLETED)
        return completed / len(NODE_ORDER) if completed else None


class Engine:
    """流水线编排引擎（持有 ArtifactStore，state.json 读写经其目录约定）。"""

    def __init__(self, store: ArtifactStore) -> None:
        """store 决定产物卷位置与本服务身份。"""
        self._store = store

    def init_job(self, job_id: str) -> JobState:
        """新建 job：建目录并写入初始 state.json（已存在则报错，防误覆盖）。"""
        state_path = self._state_path(job_id)
        if state_path.is_file():
            raise PipelineError(
                code=X_ORCH_01,
                message=f"job 已存在：{job_id}（续跑请用 open_job）",
            )
        self._store.create_job(job_id)
        state = JobState(job_id=job_id)
        self._save(state)
        return state

    def open_job(self, job_id: str) -> JobState:
        """打开已有 job 并对账：产物为准修正状态（断点续跑入口）。"""
        state = self._load(job_id)
        self.reconcile(state)
        state.resumed += 1
        self._save(state)
        return state

    def reconcile(self, state: JobState) -> None:
        """以产物为准对账：产物合规 → completed；状态 completed 但产物失效 → 回 pending。"""
        for node in state.nodes:
            artifacts_ok = all(
                self._store.is_node_completed(state.job_id, filename)
                for filename in NODE_ARTIFACTS[node.name]
            )
            if artifacts_ok and node.status is not NodeStatus.COMPLETED:
                # 产物在而状态未记（崩溃于落盘后、写状态前）：补齐
                node.status = NodeStatus.COMPLETED
                node.updated_at = _now()
            elif not artifacts_ok and node.status is NodeStatus.COMPLETED:
                # 状态记完成但产物失效（被删/损坏/未过校验）：诚实降回待运行
                node.status = NodeStatus.PENDING
                node.updated_at = _now()
                node.error = {
                    "code": X_ORCH_01,
                    "message": "对账发现产物缺失或不合规，节点状态回退为待运行",
                }

    def next_node(self, state: JobState) -> str | None:
        """第一个非 completed 节点（按 NODE_ORDER 顺序）。

        manual_intervention 节点被跳过（D1：降级跳过计入人工介入，
        流程继续推进后续节点；人工修复后可显式重新触发该节点）。
        """
        return next(
            (
                n.name
                for n in state.nodes
                if n.status not in (NodeStatus.COMPLETED, NodeStatus.MANUAL_INTERVENTION)
            ),
            None,
        )

    def check_runnable(self, state: JobState, node_name: str) -> None:
        """跳序防护：前序节点须全部 completed（docs/12 §1.2）。

        Raises:
            PipelineError: X-ORCH-02——前序存在未完成节点。
        """
        if node_name not in NODE_ARTIFACTS:
            raise PipelineError(
                code=X_ORCH_02,
                message=f"未知节点名：{node_name}",
            )
        for name in NODE_ORDER:
            if name == node_name:
                break
            # 降级节点视同已处理：不阻塞后续（D1 口径，与 next_node 一致）
            if state.node(name).status not in (
                NodeStatus.COMPLETED,
                NodeStatus.MANUAL_INTERVENTION,
            ):
                raise PipelineError(
                    code=X_ORCH_02,
                    message=f"跳序请求：{node_name} 的前序节点 {name} 尚未完成",
                    details={"blocked_by": name},
                )

    def mark_running(self, state: JobState, node_name: str) -> None:
        """置运行中（attempts 自增——一次真实尝试计一次），并清除上一轮遗留错误。

        不清 error 会让前端把「上一轮失败原因」当成「当前正在失败」持续展示：
        重跑后节点已是 running，旧错误字符串却仍挂在状态里，直到本轮写下新错误或
        成功——中间这段时间的展示是错的。
        """
        node = state.node(node_name)
        node.status = NodeStatus.RUNNING
        node.attempts += 1
        node.error = None
        node.updated_at = _now()
        self._save(state)

    def mark_completed(self, state: JobState, node_name: str) -> None:
        """置完成：先验产物判据，未达标拒绝（不把「跑完了」当完成）。"""
        filenames = NODE_ARTIFACTS[node_name]
        invalid = [f for f in filenames if not self._store.is_node_completed(state.job_id, f)]
        if invalid:
            raise PipelineError(
                code=X_ORCH_01,
                message=f"节点 {node_name} 产物未达标：{invalid}",
            )
        node = state.node(node_name)
        node.status = NodeStatus.COMPLETED
        node.error = None
        node.updated_at = _now()
        self._save(state)

    def mark_failed(self, state: JobState, node_name: str, error: PipelineError) -> None:
        """失败回 pending 并记录错误（重试耗尽后由编排决定降级）。"""
        node = state.node(node_name)
        node.status = NodeStatus.PENDING
        node.error = error.to_dict()
        node.updated_at = _now()
        self._save(state)

    def stop_running_nodes(self, job_id: str, message: str | None = None) -> list[str]:
        """将中断时仍在运行的节点安全回退为待运行。

        已落盘且已通过校验的产物不会删除；下次重跑仍通过现有的对账与级联归档机制
        进入。必须在后台协程取消并收敛后调用，避免与节点自身的状态写入竞争。
        """
        state = self._load(job_id)
        stopped_message = message or "任务被用户停止；已完成产物保留，可从该节点继续重跑"
        stopped: list[str] = []
        for node in state.nodes:
            if node.status is not NodeStatus.RUNNING:
                continue
            node.status = NodeStatus.PENDING
            node.error = {
                "code": X_ORCH_04,
                "message": stopped_message,
            }
            node.updated_at = _now()
            stopped.append(node.name)
        if stopped:
            self._save(state)
        return stopped

    def mark_degraded(self, state: JobState, node_name: str, reason: str) -> None:
        """降级：置 manual_intervention + 降级清单（D1：分母不缩减，报告须分列）。"""
        node = state.node(node_name)
        node.status = NodeStatus.MANUAL_INTERVENTION
        node.updated_at = _now()
        state.degraded = (
            *state.degraded,
            {"node": node_name, "reason": reason, "marked_at": _now()},
        )
        self._save(state)

    def node_statuses(self, state: JobState) -> list[dict[str, str]]:
        """API 用的节点状态摘要。"""
        return [{"name": n.name, "status": n.status.value} for n in state.nodes]

    def _state_path(self, job_id: str) -> Path:
        """state.json 路径（docs/12 §二布局）。"""
        return self._store.job_dir(job_id) / "state.json"

    def _load(self, job_id: str) -> JobState:
        """读取 state.json；损坏时按 X-ORCH-01 拒绝（宁停不猜）。"""
        path = self._state_path(job_id)
        if not path.is_file():
            raise PipelineError(
                code=X_ORCH_01,
                message=f"job 不存在：{job_id}（新建请用 init_job）",
            )
        try:
            payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise PipelineError(
                code=X_ORCH_01,
                message=f"state.json 不可读或损坏：{exc}",
            ) from exc
        state = JobState.from_dict(payload)
        if state.job_id != job_id:
            raise PipelineError(
                code=X_ORCH_01,
                message=f"state.json 的 job_id 不匹配：{state.job_id} != {job_id}",
            )
        return state

    def _save(self, state: JobState) -> None:
        """原子落盘 state.json（tmp + replace）。"""
        state.updated_at = _now()
        path = self._state_path(state.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
