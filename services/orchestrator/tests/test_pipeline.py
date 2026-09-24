"""9 节点口径与 API 健康检查测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from common.config import Settings
from common.pipeline import NODE_ORDER, PipelineState
from fastapi.testclient import TestClient

from orchestrator.api import create_app
from orchestrator.persistence import Database


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


def test_readiness_and_prometheus_metrics_endpoints() -> None:
    client = TestClient(create_app(_settings()))
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "autotest_queue_active_jobs" in metrics.text
    assert "autotest_database_up 1" in metrics.text
    assert 'route="/health"' in client.get("/health").text or 'route="/ready"' in metrics.text


def test_database_readiness_requires_schema_migration(tmp_path: Path) -> None:
    """数据库能连接但未执行迁移时，服务不得误报 ready。"""
    db_path = tmp_path / "unmigrated.db"
    Database(f"sqlite:///{db_path}").create_schema()
    settings = replace(
        _settings(),
        database_url=f"sqlite:///{db_path}",
        database_auto_create=False,
    )
    client = TestClient(create_app(settings))
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["database"] == "ok"
    assert response.json()["schema"]["status"] == "migration_required"


def test_database_readiness_accepts_migrated_schema(tmp_path: Path) -> None:
    """显式迁移完成后，就绪检查允许接收任务。"""
    db_path = tmp_path / "migrated.db"
    Database(f"sqlite:///{db_path}").migrate_schema()
    settings = replace(
        _settings(),
        database_url=f"sqlite:///{db_path}",
        database_auto_create=False,
    )
    client = TestClient(create_app(settings))
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["schema"]["status"] == "ok"


def test_unmigrated_database_does_not_abort_application_startup(tmp_path: Path) -> None:
    """数据库尚未建表时，应用仍可启动并通过 /ready 暴露迁移状态。"""
    settings = replace(
        _settings(),
        database_url=f"sqlite:///{tmp_path / 'missing-schema.db'}",
        database_auto_create=False,
    )
    client = TestClient(create_app(settings))
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"
    assert response.json()["schema"]["status"] == "unavailable"


def test_object_storage_readiness_requires_healthcheck() -> None:
    """启用对象存储但探活失败时，服务不得接收任务。"""
    settings = _settings()
    app = create_app(settings)

    class BrokenStorage:
        def healthcheck(self) -> bool:
            raise RuntimeError("object storage unavailable")

    app.state.object_storage = BrokenStorage()
    client = TestClient(app)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["object_storage"] == "unavailable"


def test_nodes_endpoint_returns_nine() -> None:
    """/api/v1/pipeline/nodes 返回 9 个节点。"""
    client = TestClient(create_app(_settings()))
    response = client.get("/api/v1/pipeline/nodes")
    assert response.status_code == 200
    assert len(response.json()["nodes"]) == 9
