"""服务配置：显式环境变量读取（docs/09 第五章：采样参数禁止硬编码）。"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """orchestrator 运行配置，全部来自环境变量。"""

    port: int
    java_parser_url: str
    artifacts_root: str
    # MiniMax 调用参数（OpenAI 兼容协议；页面候选模型为 M3/M2.7/M2.5）
    llm_base_url: str
    llm_model_name: str
    llm_api_key: str  # 可为空（本地 Mock/内网网关免鉴权场景），入参不进日志
    llm_temperature: float
    llm_top_p: float
    llm_seed: int | None
    llm_timeout_s: float  # docs/12 §三：单次调用超时
    schemas_dir: str  # 产物 Schema 目录（校验用）
    # 宿主机源码目录在容器内的统一挂载点；Compose 的宿主机路径单独由
    # REPOSITORIES_HOST_ROOT 注入，避免 Windows C:/... 路径传进容器。
    repositories_root: str = "/opt/repos"
    # 数据库连接；为空时保持文件型兼容模式，生产部署应配置 PostgreSQL。
    database_url: str | None = None
    # 仅用于首次本地初始化；生产环境建议通过迁移命令建表后保持 false。
    database_auto_create: bool = False
    # 单进程后台队列的并发上限；多实例部署时由数据库租约进一步约束。
    job_max_concurrency: int = 2
    # 任务级租约时长；节点级租约 API 复用该部署基线。
    job_lease_seconds: int = 120
    # 单次模型最大输出，长结果由节点分批生成而非无限提高该值。
    llm_max_tokens: int = 16384
    # 任务级凭据加密主密钥；生产部署必须配置，开发环境可留空兼容旧任务。
    secrets_encryption_key: str | None = None
    # 部门访问令牌映射 JSON；未配置时保持本地单用户兼容模式。
    access_tokens_json: str | None = None
    # 单用户部署时由服务端固定使用管理员身份，浏览器无需保存或填写技术令牌。
    # 保留令牌映射，未来切换企业登录/多用户模式只需关闭此开关。
    single_user_mode: bool = False
    # 产物生命周期：0 表示不自动清理；启用后先进入 .trash 回收区。
    artifact_retention_days: int = 0
    artifact_trash_grace_days: int = 7
    artifact_cleanup_interval_s: int = 3600
    # 审计事件保留：0 表示不自动清理；清理只删除数据库事件，不影响产物。
    audit_retention_days: int = 0
    audit_cleanup_interval_s: int = 3600
    # 可选 S3/MinIO 镜像；local 保持本地产物卷主读源。
    artifact_storage_backend: str = "local"
    artifact_storage_bucket: str | None = None
    artifact_storage_endpoint: str | None = None
    artifact_storage_region: str = "us-east-1"
    artifact_storage_access_key: str | None = None
    artifact_storage_secret_key: str | None = None
    artifact_storage_prefix: str = "llm-autotest"
    artifact_storage_verify_tls: bool = True

    def __post_init__(self) -> None:
        """在启动前拒绝会导致队列/LLM 客户端异常的无效部署参数。"""
        if not 1 <= self.port <= 65535:
            raise ValueError("PORT 必须在 1 到 65535 之间")
        if not self.java_parser_url.strip().startswith(("http://", "https://")):
            raise ValueError("JAVA_PARSER_URL 必须是 http(s) 地址")
        if not self.repositories_root.strip() or not Path(self.repositories_root).is_absolute():
            raise ValueError("REPOSITORIES_ROOT 必须是绝对目录")
        if not self.llm_base_url.strip().startswith(("http://", "https://")):
            raise ValueError("LLM_BASE_URL 必须是 http(s) 地址")
        if not self.llm_model_name.strip():
            raise ValueError("LLM_MODEL_NAME 不能为空")
        if not math.isfinite(self.llm_timeout_s) or self.llm_timeout_s <= 0:
            raise ValueError("LLM_TIMEOUT_S 必须是正数")
        if not math.isfinite(self.llm_temperature) or self.llm_temperature < 0:
            raise ValueError("LLM_TEMPERATURE 必须是非负数")
        if not math.isfinite(self.llm_top_p) or not 0 < self.llm_top_p <= 1:
            raise ValueError("LLM_TOP_P 必须在 (0, 1] 范围内")
        if self.llm_max_tokens < 1024:
            raise ValueError("LLM_MAX_TOKENS 不能小于 1024")
        if self.job_max_concurrency < 1:
            raise ValueError("JOB_MAX_CONCURRENCY 必须大于 0")
        if self.job_lease_seconds < 1:
            raise ValueError("JOB_LEASE_SECONDS 必须大于 0")
        if self.artifact_retention_days < 0 or self.artifact_trash_grace_days < 0:
            raise ValueError("产物保留天数不能为负数")
        if self.audit_retention_days < 0:
            raise ValueError("AUDIT_RETENTION_DAYS 不能为负数")

    @property
    def public_summary(self) -> str:
        """不含密钥的配置摘要（日志安全口径）。"""
        return (
            f"port={self.port} java_parser_url={self.java_parser_url} "
            f"artifacts_root={self.artifacts_root} model={self.llm_model_name} "
            f"temperature={self.llm_temperature} top_p={self.llm_top_p} seed={self.llm_seed} "
            f"job_max_concurrency={self.job_max_concurrency} "
            f"job_lease_seconds={self.job_lease_seconds}"
            f" secrets_store={'enabled' if self.secrets_encryption_key else 'disabled'}"
            f" access_control={'single_user' if self.single_user_mode else 'enabled' if self.access_tokens_json else 'disabled'}"
            f" artifact_retention_days={self.artifact_retention_days}"
            f" audit_retention_days={self.audit_retention_days}"
            f" artifact_storage={self.artifact_storage_backend}"
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        """从环境变量构建配置；缺失必填项时显式报错。"""
        source = env if env is not None else dict(os.environ)
        try:
            return cls(
                port=int(source.get("PORT", "8000")),
                java_parser_url=source["JAVA_PARSER_URL"],
                artifacts_root=source.get("ARTIFACTS_ROOT", "/data/llm-autotest"),
                repositories_root=source.get("REPOSITORIES_ROOT", "/opt/repos"),
                llm_base_url=source["LLM_BASE_URL"],
                llm_model_name=source.get("LLM_MODEL_NAME", "MiniMax-M3"),
                llm_api_key=source.get("LLM_API_KEY", ""),
                llm_temperature=float(source.get("LLM_TEMPERATURE", "0.2")),
                llm_top_p=float(source.get("LLM_TOP_P", "0.8")),
                llm_seed=int(source["LLM_SEED"]) if source.get("LLM_SEED") else None,
                # MiniMax 推理批次可能超过 120s；服务级默认与部署基线统一为 300s，
                # 仍允许按网关 SLA 通过环境变量调小或调大。
                llm_timeout_s=float(source.get("LLM_TIMEOUT_S", "300")),
                schemas_dir=source.get("SCHEMAS_DIR", "schemas"),
                llm_max_tokens=int(source.get("LLM_MAX_TOKENS", "16384")),
                database_url=source.get("DATABASE_URL") or None,
                database_auto_create=source.get("DATABASE_AUTO_CREATE", "false").lower()
                in {"1", "true", "yes", "on"},
                job_max_concurrency=int(source.get("JOB_MAX_CONCURRENCY", "2")),
                job_lease_seconds=int(source.get("JOB_LEASE_SECONDS", "120")),
                secrets_encryption_key=source.get("SECRETS_ENCRYPTION_KEY") or None,
                access_tokens_json=source.get("AUTOTEST_ACCESS_TOKENS_JSON") or None,
                single_user_mode=source.get("AUTOTEST_SINGLE_USER_MODE", "false").lower()
                in {"1", "true", "yes", "on"},
                artifact_retention_days=int(source.get("ARTIFACT_RETENTION_DAYS", "0")),
                artifact_trash_grace_days=int(source.get("ARTIFACT_TRASH_GRACE_DAYS", "7")),
                artifact_cleanup_interval_s=int(
                    source.get("ARTIFACT_CLEANUP_INTERVAL_S", "3600")
                ),
                audit_retention_days=int(source.get("AUDIT_RETENTION_DAYS", "0")),
                audit_cleanup_interval_s=int(
                    source.get("AUDIT_CLEANUP_INTERVAL_S", "3600")
                ),
                artifact_storage_backend=source.get("ARTIFACT_STORAGE_BACKEND", "local"),
                artifact_storage_bucket=source.get("ARTIFACT_STORAGE_BUCKET") or None,
                artifact_storage_endpoint=source.get("ARTIFACT_STORAGE_ENDPOINT") or None,
                artifact_storage_region=source.get("ARTIFACT_STORAGE_REGION", "us-east-1"),
                artifact_storage_access_key=source.get("ARTIFACT_STORAGE_ACCESS_KEY") or None,
                artifact_storage_secret_key=source.get("ARTIFACT_STORAGE_SECRET_KEY") or None,
                artifact_storage_prefix=source.get(
                    "ARTIFACT_STORAGE_PREFIX", "llm-autotest"
                ),
                artifact_storage_verify_tls=source.get(
                    "ARTIFACT_STORAGE_VERIFY_TLS", "true"
                ).lower()
                in {"1", "true", "yes", "on"},
            )
        except KeyError as exc:
            msg = f"缺少必填环境变量：{exc}"
            raise RuntimeError(msg) from exc
