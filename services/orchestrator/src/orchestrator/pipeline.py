# 对应 R-MTR-01：9 节点固定口径（分母固定为 9，禁止增删）。
"""流水线节点定义与状态：9 节点口径与四态标注的骨架实现。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class NodeStatus(StrEnum):
    """节点四态（对应 07 §3.2）。

    已完成 / 人工介入 / 运行中 / 待运行；降级节点按人工介入计入（D1）。
    """

    COMPLETED = "completed"
    MANUAL_INTERVENTION = "manual_intervention"
    RUNNING = "running"
    PENDING = "pending"


# 9 节点固定顺序（R-MTR-01 冻结口径，分母 = 9，禁止改动）
NODE_ORDER: tuple[str, ...] = (
    "req_parse",
    "code_scan",
    "interface_reconstruct",
    "code_review",
    "testcase_generate",
    "auto_execute",
    "test_report",
    "defect_list",
    "retest_regression",
)


@dataclass(frozen=True)
class NodeRecord:
    """单个节点的当前状态记录。"""

    name: str
    status: NodeStatus


@dataclass(frozen=True)
class PipelineState:
    """整条流水线的节点状态集合（骨架：真实状态持久化于产物卷）。"""

    records: tuple[NodeRecord, ...]

    @classmethod
    def initial(cls) -> PipelineState:
        """全部节点处于待运行状态的初始态。"""
        return cls(records=tuple(NodeRecord(n, NodeStatus.PENDING) for n in NODE_ORDER))

    def node_summary(self, name: str) -> dict[str, str]:
        """单节点摘要（API 返回结构）。"""
        record = next(r for r in self.records if r.name == name)
        return {"name": record.name, "status": record.status.value}

    @property
    def automation_rate(self) -> float | None:
        """自动化率 = 已完成节点数 / 9（F-MTR-03：程序化汇总，不经 LLM）。

        骨架阶段全部为待运行，返回 None 表示尚未实测；正式实现须从产物卷读取。
        """
        completed = sum(1 for r in self.records if r.status is NodeStatus.COMPLETED)
        if completed == 0:
            return None
        return completed / len(NODE_ORDER)


NodeName = Literal[
    "req_parse",
    "code_scan",
    "interface_reconstruct",
    "code_review",
    "testcase_generate",
    "auto_execute",
    "test_report",
    "defect_list",
    "retest_regression",
]
