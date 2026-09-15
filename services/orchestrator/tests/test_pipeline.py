"""9 节点口径与 API 健康检查测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestrator.api import create_app
from orchestrator.config import Settings
from orchestrator.pipeline import NODE_ORDER, PipelineState


def _settings() -> Settings:
    return Settings.from_env(
        {
            "JAVA_PARSER_URL": "http://java-parser:8080",
            "LLM_BASE_URL": "https://api.example.com",
        }
    )


def test_node_order_frozen_at_nine() -> None:
    """R-MTR-01：分母固定为 9，节点清单禁止增删。"""
    assert len(NODE_ORDER) == 9


def test_initial_state_all_pending_and_no_rate() -> None:
    """初始态全部待运行；自动化率未实测时返回 None 而非 0（诚实口径）。"""
    state = PipelineState.initial()
    assert all(r.status.value == "pending" for r in state.records)
    assert state.automation_rate is None


def test_health_endpoint() -> None:
    """/health 返回 ok（docker-compose 健康检查依赖）。"""
    client = TestClient(create_app(_settings()))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_nodes_endpoint_returns_nine() -> None:
    """/api/v1/pipeline/nodes 返回 9 个节点。"""
    client = TestClient(create_app(_settings()))
    response = client.get("/api/v1/pipeline/nodes")
    assert response.status_code == 200
    assert len(response.json()["nodes"]) == 9
