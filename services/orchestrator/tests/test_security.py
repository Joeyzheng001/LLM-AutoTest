"""P0-06 第一批安全边界测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from common.security import (
    REDACTED,
    AccessPolicy,
    SecretStore,
    SecretStoreError,
    redact_secrets,
    write_private_json,
)
from cryptography.fernet import Fernet


def test_redact_secrets_copies_nested_sensitive_fields() -> None:
    source = {
        "token": "secret-token",
        "nested": {"Authorization": "Bearer secret", "username": "admin"},
        "items": [{"password": "secret-password"}],
    }

    redacted = redact_secrets(source)

    assert redacted == {
        "token": "<redacted>",
        "nested": {"Authorization": "<redacted>", "username": "admin"},
        "items": [{"password": "<redacted>"}],
    }
    assert source["token"] != REDACTED


def test_write_private_json_is_atomic_and_private(tmp_path: Path) -> None:
    path = tmp_path / "job" / "job_input.json"
    write_private_json(path, {"job_id": "job-security-0001", "password": "secret"})

    assert json.loads(path.read_text(encoding="utf-8"))["password"] != REDACTED
    assert not path.with_suffix(".json.tmp").exists()
    # Unix runner 应为服务账户私有；Windows 只保证文件写入成功，ACL 由部署配置负责。
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_secret_store_encrypts_per_job_and_survives_new_instance(tmp_path: Path) -> None:
    key = Fernet.generate_key().decode("ascii")
    first = SecretStore(tmp_path, key)
    first.save("job-security-0002", {"auth": {"password": "fixture-value"}})

    blob = tmp_path / "job-security-0002" / ".job-credentials.enc"
    assert blob.is_file()
    assert b"fixture-value" not in blob.read_bytes()
    assert SecretStore(tmp_path, key).load("job-security-0002") == {
        "auth": {"password": "fixture-value"}
    }


def test_secret_store_rejects_wrong_key(tmp_path: Path) -> None:
    first = SecretStore(tmp_path, Fernet.generate_key().decode("ascii"))
    first.save("job-security-0003", {"llm_api_key": "fixture-value"})

    from pytest import raises

    with raises(RuntimeError, match="无法解密"):
        SecretStore(tmp_path, Fernet.generate_key().decode("ascii")).load("job-security-0003")


def test_access_policy_parses_optional_department_quotas() -> None:
    policy = AccessPolicy(
        json.dumps(
            {
                "token": {
                    "user_id": "alice",
                    "projects": ["fund"],
                    "max_concurrent_jobs": 2,
                    "daily_job_limit": 10,
                }
            }
        )
    )
    principal = policy.authenticate({"authorization": "Bearer token"})
    assert principal is not None
    assert principal.max_concurrent_jobs == 2
    assert principal.daily_job_limit == 10


def test_access_policy_project_quota_overrides_token_default() -> None:
    policy = AccessPolicy(
        json.dumps(
            {
                "token": {
                    "user_id": "alice",
                    "projects": ["fund", "bond"],
                    "max_concurrent_jobs": 4,
                    "project_quotas": {
                        "fund": {"max_concurrent_jobs": 1, "daily_job_limit": 3}
                    },
                }
            }
        )
    )
    principal = policy.authenticate({"x-autotest-token": "token"})
    assert principal is not None
    assert principal.quota_for("fund").max_concurrent_jobs == 1
    assert principal.quota_for("fund").daily_job_limit == 3
    assert principal.quota_for("bond").max_concurrent_jobs == 4


def test_access_policy_single_user_mode_does_not_require_browser_token() -> None:
    policy = AccessPolicy(
        json.dumps({"token": {"user_id": "qa", "projects": ["fund"]}}),
        single_user_mode=True,
    )
    principal = policy.authenticate({})
    assert policy.enabled is False
    assert principal is not None
    assert principal.user_id == "admin"
    assert policy.allows_project(principal, "any-project")


def test_access_policy_rejects_invalid_quota() -> None:
    with pytest.raises(SecretStoreError, match="max_concurrent_jobs"):
        AccessPolicy(
            json.dumps(
                {
                    "token": {
                        "user_id": "alice",
                        "projects": ["fund"],
                        "max_concurrent_jobs": -1,
                    }
                }
            )
        )
