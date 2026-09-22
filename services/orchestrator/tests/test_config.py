"""Settings 环境变量读取与日志安全口径测试。"""

from __future__ import annotations

import pytest

from orchestrator.config import Settings


def test_settings_from_env_complete() -> None:
    """必填变量齐全时正确构建，采样参数显式读取。"""
    settings = Settings.from_env(
        {
            "JAVA_PARSER_URL": "http://java-parser:8080",
            "LLM_BASE_URL": "https://api.example.com",
            "LLM_TEMPERATURE": "0.3",
            "LLM_SEED": "42",
            "DATABASE_URL": "postgresql+psycopg://autotest:secret@db:5432/autotest",
        }
    )
    assert settings.java_parser_url == "http://java-parser:8080"
    assert settings.llm_model_name == "MiniMax-M3"
    assert settings.repositories_root == "/opt/repos"
    assert settings.llm_timeout_s == pytest.approx(300.0)
    assert settings.llm_temperature == pytest.approx(0.3)
    assert settings.llm_seed == 42
    assert settings.database_url == "postgresql+psycopg://autotest:secret@db:5432/autotest"


def test_settings_supports_explicit_single_user_mode() -> None:
    settings = Settings.from_env(
        {
            "JAVA_PARSER_URL": "http://java-parser:8080",
            "LLM_BASE_URL": "https://api.example.com",
            "AUTOTEST_SINGLE_USER_MODE": "true",
        }
    )
    assert settings.single_user_mode is True
    assert "access_control=single_user" in settings.public_summary


def test_settings_from_env_missing_required() -> None:
    """缺失必填项时显式报错，禁止静默默认。"""
    with pytest.raises(RuntimeError, match="JAVA_PARSER_URL"):
        Settings.from_env({})


def test_settings_allows_llm_timeout_override() -> None:
    settings = Settings.from_env(
        {
            "JAVA_PARSER_URL": "http://java-parser:8080",
            "LLM_BASE_URL": "https://api.example.com",
            "LLM_TIMEOUT_S": "45",
        }
    )
    assert settings.llm_timeout_s == pytest.approx(45.0)


def test_settings_rejects_invalid_runtime_limits() -> None:
    with pytest.raises(ValueError, match="LLM_TIMEOUT_S"):
        Settings.from_env(
            {
                "JAVA_PARSER_URL": "http://java-parser:8080",
                "LLM_BASE_URL": "https://api.example.com",
                "LLM_TIMEOUT_S": "0",
            }
        )
    with pytest.raises(ValueError, match="JOB_MAX_CONCURRENCY"):
        Settings.from_env(
            {
                "JAVA_PARSER_URL": "http://java-parser:8080",
                "LLM_BASE_URL": "https://api.example.com",
                "JOB_MAX_CONCURRENCY": "0",
            }
        )


def test_public_summary_excludes_secrets() -> None:
    """配置摘要不得包含 API Key 等敏感字段。"""
    settings = Settings.from_env(
        {
            "JAVA_PARSER_URL": "http://java-parser:8080",
            "LLM_BASE_URL": "https://api.example.com",
            "LLM_API_KEY": "sk-secret-value",
        }
    )
    summary = settings.public_summary
    assert "sk-secret-value" not in summary
    assert "autotest:secret" not in summary


def test_artifact_storage_settings_are_explicit_and_secret_free() -> None:
    settings = Settings.from_env(
        {
            "JAVA_PARSER_URL": "http://java-parser:8080",
            "LLM_BASE_URL": "https://api.example.com",
            "ARTIFACT_STORAGE_BACKEND": "s3",
            "ARTIFACT_STORAGE_BUCKET": "autotest-evidence",
            "ARTIFACT_STORAGE_ENDPOINT": "http://minio:9000",
            "ARTIFACT_STORAGE_ACCESS_KEY": "access-secret",
            "ARTIFACT_STORAGE_SECRET_KEY": "secret-secret",
        }
    )
    assert settings.artifact_storage_backend == "s3"
    assert settings.artifact_storage_bucket == "autotest-evidence"
    assert "access-secret" not in settings.public_summary
    assert "secret-secret" not in settings.public_summary


def test_audit_retention_settings_are_explicit() -> None:
    settings = Settings.from_env(
        {
            "JAVA_PARSER_URL": "http://java-parser:8080",
            "LLM_BASE_URL": "https://api.example.com",
            "AUDIT_RETENTION_DAYS": "90",
            "AUDIT_CLEANUP_INTERVAL_S": "120",
        }
    )
    assert settings.audit_retention_days == 90
    assert settings.audit_cleanup_interval_s == 120
    assert "audit_retention_days=90" in settings.public_summary
