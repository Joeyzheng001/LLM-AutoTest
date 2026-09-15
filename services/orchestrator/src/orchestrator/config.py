"""服务配置：显式环境变量读取（docs/09 第五章：采样参数禁止硬编码）。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """orchestrator 运行配置，全部来自环境变量。"""

    port: int
    java_parser_url: str
    artifacts_root: str
    # GLM-5.3 调用参数（仅允许 GLM-5.3，冻结基线）
    llm_base_url: str
    llm_model_name: str
    llm_api_key: str  # 可为空（本地 Mock/内网网关免鉴权场景），入参不进日志
    llm_temperature: float
    llm_top_p: float
    llm_seed: int | None
    llm_timeout_s: float  # docs/12 §三：单次调用超时
    schemas_dir: str  # 产物 Schema 目录（校验用）

    @property
    def public_summary(self) -> str:
        """不含密钥的配置摘要（日志安全口径）。"""
        return (
            f"port={self.port} java_parser_url={self.java_parser_url} "
            f"artifacts_root={self.artifacts_root} model={self.llm_model_name} "
            f"temperature={self.llm_temperature} top_p={self.llm_top_p} seed={self.llm_seed}"
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
                llm_base_url=source["LLM_BASE_URL"],
                llm_model_name=source.get("LLM_MODEL_NAME", "glm-5.3"),
                llm_api_key=source.get("LLM_API_KEY", ""),
                llm_temperature=float(source.get("LLM_TEMPERATURE", "0.2")),
                llm_top_p=float(source.get("LLM_TOP_P", "0.8")),
                llm_seed=int(source["LLM_SEED"]) if source.get("LLM_SEED") else None,
                llm_timeout_s=float(source.get("LLM_TIMEOUT_S", "120")),
                schemas_dir=source.get("SCHEMAS_DIR", "schemas"),
            )
        except KeyError as exc:
            msg = f"缺少必填环境变量：{exc}"
            raise RuntimeError(msg) from exc
