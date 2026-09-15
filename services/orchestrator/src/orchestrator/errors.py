"""统一错误结构：{code, message, node_id?, details}（docs/12 第四章，禁止表外错误码）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 本模块定义的错误码（docs/12 §四 甲段；X-CODE-* 由 java-parser 契约定义，不在本表）
X_LLM_01 = "X-LLM-01"  # LLM 调用超时/网络失败（可重试）
X_LLM_03 = "X-LLM-03"  # Prompt 文件缺失/元信息不合规（配置错误，人工介入）
X_ORCH_01 = "X-ORCH-01"  # 产物校验失败
X_ORCH_02 = "X-ORCH-02"  # 断点续跑跳序请求


@dataclass
class PipelineError(Exception):
    """流水线统一错误：code 必须来自 docs/12 §四 总表。"""

    code: str
    message: str
    node_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        """日志输出格式：[code] message（node）。"""
        node = f"（{self.node_id}）" if self.node_id else ""
        return f"[{self.code}] {self.message}{node}"

    def to_dict(self) -> dict[str, Any]:
        """API 返回结构（契约冻结口径：node_id/details 仅在存在时出现）。"""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.node_id is not None:
            payload["node_id"] = self.node_id
        if self.details:
            payload["details"] = self.details
        return payload
