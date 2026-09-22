"""生产部署 CLI 初始化入口测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from cryptography.fernet import Fernet

from orchestrator.__main__ import _cmd_init_db, _cmd_migrate_db, _cmd_validate_deployment
from orchestrator.config import Settings
from orchestrator.persistence import Database


def test_init_db_creates_schema_explicitly(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "JAVA_PARSER_URL": "http://java-parser:8080",
            "LLM_BASE_URL": "https://api.example.com",
        }
    )
    configured = replace(settings, database_url=f"sqlite:///{tmp_path / 'cli.db'}")

    assert _cmd_init_db(configured) == 0
    database = Database(configured.database_url or "")
    assert database.healthcheck() is True
    assert database.schema_version() == "001_initial"
    assert _cmd_migrate_db(configured) == 0
    assert database.schema_version() == "001_initial"


def test_validate_deployment_requires_shared_service_security() -> None:
    settings = Settings.from_env(
        {
            "JAVA_PARSER_URL": "http://java-parser:8080",
            "LLM_BASE_URL": "https://api.minimaxi.com/v1",
            "DATABASE_URL": "postgresql+psycopg://autotest:secret@db:5432/autotest",
            "SECRETS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "AUTOTEST_ACCESS_TOKENS_JSON": '{"token":{"user_id":"qa","projects":["fund"]}}',
        }
    )
    assert _cmd_validate_deployment(settings) == 0


def test_validate_deployment_rejects_local_database_and_missing_tokens() -> None:
    settings = Settings.from_env(
        {
            "JAVA_PARSER_URL": "http://java-parser:8080",
            "LLM_BASE_URL": "https://api.minimaxi.com/v1",
            "DATABASE_URL": "sqlite:///local.db",
        }
    )
    assert _cmd_validate_deployment(settings) == 2
