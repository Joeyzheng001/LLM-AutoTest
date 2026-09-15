"""REST API 层：只做协议转换与校验，业务下沉 service（.rules/python-code-style）。"""

from __future__ import annotations

from fastapi import FastAPI

from orchestrator.config import Settings
from orchestrator.pipeline import NODE_ORDER, PipelineState


def create_app(settings: Settings) -> FastAPI:
    """构建 FastAPI 应用实例。"""
    app = FastAPI(title="LLM-AutoTest orchestrator", version="0.1.0")
    app.state.settings = settings

    @app.get("/health")
    async def health() -> dict[str, str]:
        """健康检查（docker-compose 编排依赖）。"""
        return {"status": "ok"}

    @app.get("/api/v1/pipeline/nodes")
    async def list_nodes() -> dict[str, object]:
        """9 节点清单与当前状态（R-MTR-01 九节点固定口径）。"""
        state = PipelineState.initial()
        return {"nodes": [state.node_summary(name) for name in NODE_ORDER]}

    return app
