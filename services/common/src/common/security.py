"""共享安全边界：敏感字段脱敏和任务内部配置的私有写入。

任务配置仍由服务内部用于断点续跑；本模块确保凭据不会进入数据库事件、运行日志
或错误响应，并让 job_input 文件只对服务账户可读。运行期凭据由调用方在任务结束
后清理，跨重启复用必须使用部署级 Secret Store。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from common.artifacts import ensure_job_id

SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "set_cookie",
    }
)
REDACTED = "<redacted>"
CREDENTIAL_BLOB_NAME = ".job-credentials.enc"


def redact_secrets(value: Any, *, depth: int = 0) -> Any:
    """递归复制并掩盖常见敏感字段，不修改调用方对象。"""
    if depth > 8:
        return "…"
    if isinstance(value, dict):
        return {
            str(key): REDACTED
            if str(key).lower().replace("-", "_") in SENSITIVE_KEYS
            else redact_secrets(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, tuple):
        return [redact_secrets(item, depth=depth + 1) for item in value[:100]]
    return value


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写入任务内部配置，并设置 Unix 私有文件权限（Windows 尽力兼容）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        # Windows ACL 不由 chmod 完整表达，部署侧应使用服务账户/目录 ACL 控制。
        pass
    temporary.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class SecretStoreError(RuntimeError):
    """任务密钥不可用或无法解密。"""


@dataclass(frozen=True)
class AccessQuota:
    """用户/项目可用的任务配额；None 表示该项不限制。"""

    max_concurrent_jobs: int | None = None
    daily_job_limit: int | None = None


@dataclass(frozen=True)
class AccessPrincipal:
    """访问令牌解析后的服务端身份，不接受客户端自报 user_id。"""

    user_id: str
    projects: frozenset[str]
    max_concurrent_jobs: int | None = None
    daily_job_limit: int | None = None
    project_quotas: tuple[tuple[str, AccessQuota], ...] = ()

    def quota_for(self, project_id: str) -> AccessQuota:
        """读取项目覆盖配额；没有覆盖时回退令牌级默认值。"""
        for name, quota in self.project_quotas:
            if name == project_id:
                return quota
        for name, quota in self.project_quotas:
            if name == "*":
                return quota
        return AccessQuota(self.max_concurrent_jobs, self.daily_job_limit)


class AccessPolicy:
    """可选的部门访问令牌策略。

    环境变量格式为 ``{"token-value":{"user_id":"alice","projects":["fund"]}}``。
    未配置时返回本地兼容身份；配置后只能用 Bearer/X-Autotest-Token 令牌建立身份。
    """

    def __init__(self, tokens_json: str | None, *, single_user_mode: bool = False) -> None:
        self._tokens: dict[str, AccessPrincipal] = {}
        self._configured = bool(tokens_json)
        self._single_user_mode = single_user_mode
        if not tokens_json:
            return
        try:
            raw = json.loads(tokens_json)
        except json.JSONDecodeError as exc:
            raise SecretStoreError("AUTOTEST_ACCESS_TOKENS_JSON 不是合法 JSON") from exc
        if not isinstance(raw, dict):
            raise SecretStoreError("AUTOTEST_ACCESS_TOKENS_JSON 必须是对象")
        for token, entry in raw.items():
            if not isinstance(token, str) or not token or not isinstance(entry, dict):
                raise SecretStoreError("访问令牌映射格式非法")
            user_id = entry.get("user_id")
            projects = entry.get("projects", [])
            if not isinstance(user_id, str) or not user_id or not isinstance(projects, list):
                raise SecretStoreError("访问令牌必须声明 user_id 和 projects 数组")
            normalized_projects = frozenset(
                str(project) for project in projects if str(project).strip()
            )
            if not normalized_projects:
                raise SecretStoreError("访问令牌 projects 不能为空")
            max_concurrent = _optional_non_negative_int(
                entry.get("max_concurrent_jobs"), "max_concurrent_jobs"
            )
            daily_limit = _optional_non_negative_int(
                entry.get("daily_job_limit"), "daily_job_limit"
            )
            project_quotas = _parse_project_quotas(entry.get("project_quotas"), normalized_projects)
            self._tokens[token] = AccessPrincipal(
                user_id,
                normalized_projects,
                max_concurrent_jobs=max_concurrent,
                daily_job_limit=daily_limit,
                project_quotas=project_quotas,
            )

    @property
    def enabled(self) -> bool:
        """是否启用部门访问令牌校验。"""
        return self._configured and not self._single_user_mode

    def authenticate(self, headers: Mapping[str, str]) -> AccessPrincipal | None:
        """解析请求令牌；未启用策略时返回本地兼容身份。"""
        if self._single_user_mode:
            return AccessPrincipal("admin", frozenset({"*"}))
        if not self.enabled:
            return AccessPrincipal("local", frozenset({"*"}))
        authorization = headers.get("authorization", "")
        token = (
            authorization.removeprefix("Bearer ").strip()
            if authorization.startswith("Bearer ")
            else headers.get("x-autotest-token", "").strip()
        )
        return self._tokens.get(token)

    @staticmethod
    def allows_project(principal: AccessPrincipal, project_id: str) -> bool:
        """判断身份是否有项目访问权。"""
        return "*" in principal.projects or project_id in principal.projects


def _optional_non_negative_int(value: Any, name: str) -> int | None:
    """解析令牌级配额；缺省表示不启用该项限制。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SecretStoreError(f"访问令牌 {name} 必须是非负整数")
    return value


def _parse_project_quotas(
    value: Any,
    projects: frozenset[str],
) -> tuple[tuple[str, AccessQuota], ...]:
    """解析令牌中的项目级配额覆盖。"""
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise SecretStoreError("访问令牌 project_quotas 必须是对象")
    parsed: list[tuple[str, AccessQuota]] = []
    for project_id, raw_quota in value.items():
        if not isinstance(project_id, str) or not project_id.strip():
            raise SecretStoreError("访问令牌 project_quotas 包含非法项目名")
        if project_id != "*" and "*" not in projects and project_id not in projects:
            raise SecretStoreError(f"project_quotas 项目不在 projects 范围内：{project_id}")
        if not isinstance(raw_quota, dict):
            raise SecretStoreError(f"项目 {project_id} 的 quota 必须是对象")
        parsed.append(
            (
                project_id,
                AccessQuota(
                    max_concurrent_jobs=_optional_non_negative_int(
                        raw_quota.get("max_concurrent_jobs"),
                        f"project_quotas.{project_id}.max_concurrent_jobs",
                    ),
                    daily_job_limit=_optional_non_negative_int(
                        raw_quota.get("daily_job_limit"),
                        f"project_quotas.{project_id}.daily_job_limit",
                    ),
                ),
            )
        )
    return tuple(parsed)


class SecretStore:
    """按 job 隔离的 Fernet 加密密钥存储。

    ``encryption_key`` 由部署环境或 KMS 注入，不写入任务目录。未配置时
    ``enabled`` 为 False，由上层保留本地开发兼容路径；生产部署应强制配置。
    """

    def __init__(self, root: Path, encryption_key: str | None) -> None:
        self._root = root.resolve()
        self._fernet: Fernet | None = None
        if encryption_key:
            try:
                self._fernet = Fernet(encryption_key.encode("ascii"))
            except (ValueError, UnicodeEncodeError) as exc:
                raise SecretStoreError(
                    "SECRETS_ENCRYPTION_KEY 不是有效的 Fernet key；"
                    "请使用 Fernet.generate_key() 生成"
                ) from exc

    @property
    def enabled(self) -> bool:
        """是否已启用加密存储。"""
        return self._fernet is not None

    def _path(self, job_id: str) -> Path:
        ensure_job_id(job_id)
        path = (self._root / job_id / CREDENTIAL_BLOB_NAME).resolve()
        if self._root not in path.parents:
            raise SecretStoreError("任务密钥路径逸出产物根目录")
        return path

    def save(self, job_id: str, secrets: dict[str, Any]) -> None:
        """加密保存任务凭据；密文文件不进入用户导出白名单。"""
        if self._fernet is None:
            raise SecretStoreError("SecretStore 未启用")
        payload = json.dumps(
            {"version": 1, "job_id": job_id, "secrets": secrets},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        path = self._path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(self._fernet.encrypt(payload))
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def load(self, job_id: str) -> dict[str, Any]:
        """读取并解密任务凭据；不存在时返回空字典。"""
        if self._fernet is None:
            return {}
        path = self._path(job_id)
        if not path.is_file():
            return {}
        try:
            decoded = self._fernet.decrypt(path.read_bytes())
            payload = json.loads(decoded.decode("utf-8"))
        except (InvalidToken, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecretStoreError(f"任务密钥文件无法解密：{job_id}") from exc
        secrets = payload.get("secrets") if isinstance(payload, dict) else None
        return dict(secrets) if isinstance(secrets, dict) else {}

    def clear(self, job_id: str) -> None:
        """删除任务密钥文件；不存在时幂等。"""
        if self._fernet is None:
            return
        path = self._path(job_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise SecretStoreError(f"任务密钥文件无法删除：{job_id}") from exc
