"""P0-01 持久化基础层测试。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from orchestrator.persistence import Database, JobRecord


def test_database_creates_schema_and_job_nodes(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'state.db'}")
    db.create_schema()

    db.ensure_job("job-persistence-0001", created_by="test")
    db.record_node("job-persistence-0001", "req_parse", status="completed", attempts=1)
    db.append_event(
        "job-persistence-0001",
        "completed",
        node_name="req_parse",
        payload={"duration_ms": 12},
    )

    jobs = db.list_jobs()
    assert [job.job_id for job in jobs] == ["job-persistence-0001"]

    with db.session() as session:
        job = session.get(type(jobs[0]), "job-persistence-0001")
        assert job is not None
        assert job.nodes[0].node_name == "req_parse"
        assert job.nodes[0].status == "completed"


def test_database_schema_migration_is_idempotent_and_versioned(tmp_path: Path) -> None:
    """数据库迁移有明确版本，重复执行不产生重复记录。"""
    db = Database(f"sqlite:///{tmp_path / 'schema-version.db'}")
    assert db.migrate_schema() == ["001_initial"]
    assert db.schema_version() == "001_initial"
    assert db.migrate_schema() == []
    assert db.schema_version() == "001_initial"


def test_database_engine_uses_connection_liveness_settings(tmp_path: Path) -> None:
    """数据库引擎启用连接预检和回收，降低共享部署的瞬态断链影响。"""
    db = Database(f"sqlite:///{tmp_path / 'pool.db'}")
    assert db.engine.pool._pre_ping is True  # type: ignore[attr-defined]
    assert db.engine.pool._recycle == 1800  # type: ignore[attr-defined]


def test_database_event_payload_is_redacted(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'redaction.db'}")
    db.create_schema()
    db.ensure_job("job-persistence-redact")
    db.append_event(
        "job-persistence-redact",
        "auth_checked",
        payload={"token": "fixture-value", "username": "admin"},
    )

    from orchestrator.persistence import JobEventRecord

    with db.session() as session:
        event = session.query(JobEventRecord).one()
        assert '"token": "<redacted>"' in event.payload_json
        assert '"username": "admin"' in event.payload_json


def test_database_registers_artifact_metadata_without_reading_into_db(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'state.db'}")
    db.create_schema()
    db.ensure_job("job-persistence-0002")
    artifact = tmp_path / "requirement_items.json"
    artifact.write_text('{"job_id":"job-persistence-0002"}', encoding="utf-8")

    db.register_artifact("job-persistence-0002", artifact.name, artifact)

    with db.session() as session:
        from orchestrator.persistence import ArtifactRecord

        record = session.query(ArtifactRecord).one()
        assert record.storage_uri == str(artifact)
        assert record.size_bytes == artifact.stat().st_size
        assert record.sha256


def test_database_node_lease_prevents_duplicate_workers(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'lease.db'}"
    first = Database(db_url)
    second = Database(db_url)
    first.create_schema()
    first.ensure_job("job-lease-0001")

    assert first.claim_node("job-lease-0001", "req_parse", worker_id="worker-a") is True
    assert second.claim_node("job-lease-0001", "req_parse", worker_id="worker-b") is False
    assert first.heartbeat_node("job-lease-0001", "req_parse", worker_id="worker-a") is True
    assert second.release_node("job-lease-0001", "req_parse", worker_id="worker-b") is False
    assert (
        first.release_node(
            "job-lease-0001", "req_parse", worker_id="worker-a", status="pending"
        )
        is True
    )
    assert second.claim_node("job-lease-0001", "req_parse", worker_id="worker-b") is True


def test_database_node_snapshot_rejects_stale_job_worker(tmp_path: Path) -> None:
    """任务租约切换后，旧 Worker 不得覆盖新实例的节点状态。"""
    db = Database(f"sqlite:///{tmp_path / 'stale-node.db'}")
    db.create_schema()
    db.ensure_job("job-stale-node")
    assert db.claim_job("job-stale-node", worker_id="worker-a", lease_seconds=60) is True
    assert (
        db.record_node(
            "job-stale-node",
            "req_parse",
            status="running",
            worker_id="worker-a",
        )
        is True
    )
    assert db.release_job("job-stale-node", worker_id="worker-a", status="stopped") is True
    assert db.claim_job("job-stale-node", worker_id="worker-b", lease_seconds=60) is True
    assert (
        db.record_node(
            "job-stale-node",
            "req_parse",
            status="failed",
            worker_id="worker-a",
        )
        is False
    )
    assert (
        db.record_node(
            "job-stale-node",
            "req_parse",
            status="running",
            worker_id="worker-b",
        )
        is True
    )
    node = db.list_nodes("job-stale-node")[0]
    assert node.status == "running"


def test_database_job_lease_prevents_duplicate_pipeline_workers(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'job-lease.db'}"
    first = Database(db_url)
    second = Database(db_url)
    first.create_schema()

    assert first.claim_job("job-lease-0002", worker_id="worker-a") is True
    assert second.claim_job("job-lease-0002", worker_id="worker-b") is False
    assert first.heartbeat_job("job-lease-0002", worker_id="worker-a") is True
    assert first.release_job("job-lease-0002", worker_id="worker-a", status="pending") is True
    assert second.claim_job("job-lease-0002", worker_id="worker-b") is True


def test_database_lists_queued_and_expired_jobs_for_recovery(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'recover.db'}")
    db.create_schema()
    db.ensure_job("job-recover-queued")
    db.ensure_job("job-recover-running")
    assert db.claim_job("job-recover-running", worker_id="worker-a", lease_seconds=1) is True
    with db.session() as session:
        running = session.get(JobRecord, "job-recover-running")
        assert running is not None
        running.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    assert db.list_recoverable_jobs() == ["job-recover-queued", "job-recover-running"]


def test_database_counts_scoped_jobs_and_lists_audit_events(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'quota.db'}")
    db.create_schema()
    db.ensure_job("job-quota-1", created_by="alice", project_id="fund")
    db.ensure_job("job-quota-2", created_by="alice", project_id="fund")
    db.ensure_job("job-quota-other", created_by="bob", project_id="fund")
    with db.session() as session:
        from orchestrator.persistence import JobRecord

        first = session.get(JobRecord, "job-quota-1")
        second = session.get(JobRecord, "job-quota-2")
        assert first is not None and second is not None
        first.status = "running"
        second.status = "completed"
    db.append_event(
        "job-quota-1",
        "job_submitted",
        payload={"project_id": "fund", "token": "should-redact"},
        created_by="alice",
    )
    db.append_event(
        "job-quota-1",
        "job_rerun",
        payload={"from_node": "req_parse"},
        created_by="alice",
    )

    assert (
        db.count_jobs(
            created_by="alice",
            project_id="fund",
            statuses={"queued", "running"},
        )
        == 1
    )
    events = db.list_events("job-quota-1", limit=1)
    assert len(events) == 1
    assert events[0].created_by == "alice"
    assert db.list_events("job-quota-1", before_event_id=events[0].event_id)[0].event_type == (
        "job_submitted"
    )
    assert "should-redact" not in db.list_events("job-quota-1")[1].payload_json


def test_database_job_index_filters_and_node_snapshot(tmp_path: Path) -> None:
    """任务列表查询不依赖产物目录时，归属过滤和节点快照仍然可用。"""
    db = Database(f"sqlite:///{tmp_path / 'job-index.db'}")
    db.create_schema()
    db.ensure_job("job-index-a", created_by="alice", project_id="fund")
    db.ensure_job("job-index-b", created_by="bob", project_id="fund")
    db.record_node(
        "job-index-a",
        "req_parse",
        status="manual_intervention",
        attempts=2,
        error={"code": "X-LLM-01", "message": "需要人工介入"},
    )

    assert [job.job_id for job in db.list_jobs(created_by="alice", project_id="fund")] == [
        "job-index-a"
    ]
    assert db.get_job("job-index-a") is not None
    assert db.get_job("job-index-missing") is None
    nodes = db.list_nodes("job-index-a")
    assert len(nodes) == 1
    assert nodes[0].status == "manual_intervention"
    assert nodes[0].finished_at is not None


def test_database_job_config_snapshots_are_versioned_and_redacted(tmp_path: Path) -> None:
    """共享卷丢失时可恢复最新非敏感配置，但密钥不进入配置表。"""
    db = Database(f"sqlite:///{tmp_path / 'job-config.db'}")
    db.create_schema()
    db.ensure_job("job-config-1", created_by="alice", project_id="fund")
    first = db.save_job_config(
        "job-config-1",
        {"rest_url": "http://service", "llm": {"api_key": "secret-key", "model": "M3"}},
        created_by="alice",
    )
    second = db.save_job_config(
        "job-config-1",
        {"rest_url": "http://service-v2", "llm": {"model": "M3"}},
        created_by="alice",
    )

    assert (first, second) == (1, 2)
    payload = db.latest_job_config("job-config-1")
    assert payload == {"rest_url": "http://service-v2", "llm": {"model": "M3"}}
    with db.session() as session:
        from orchestrator.persistence import JobConfigRecord

        rows = session.query(JobConfigRecord).all()
        assert all("secret-key" not in row.config_json for row in rows)


def test_database_purges_only_old_events(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'audit-retention.db'}")
    db.create_schema()
    db.ensure_job("job-audit-retention")
    db.append_event("job-audit-retention", "old", payload={})
    db.append_event("job-audit-retention", "new", payload={})
    with db.session() as session:
        from orchestrator.persistence import JobEventRecord

        old = session.query(JobEventRecord).filter_by(event_type="old").one()
        old.created_at = datetime.now(UTC) - timedelta(days=30)

    removed = db.purge_events_before(datetime.now(UTC) - timedelta(days=7))
    assert removed == 1
    assert [event.event_type for event in db.list_events("job-audit-retention")] == ["new"]


def test_database_healthcheck_is_read_only(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'health.db'}")
    db.create_schema()
    assert db.healthcheck() is True
