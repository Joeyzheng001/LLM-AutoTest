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
        }
    )
    assert settings.java_parser_url == "http://java-parser:8080"
    assert settings.llm_model_name == "glm-5.3"
    assert settings.llm_temperature == pytest.approx(0.3)
    assert settings.llm_seed == 42


def test_settings_from_env_missing_required() -> None:
    """缺失必填项时显式报错，禁止静默默认。"""
    with pytest.raises(RuntimeError, match="JAVA_PARSER_URL"):
        Settings.from_env({})


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
