# 对应 F-ORCH-01：流水线统一入口命令；F-ORCH-02：断点续跑。
"""orchestrator 服务入口：CLI 统一入口 + REST API。"""

from __future__ import annotations

import logging

from orchestrator.api import create_app
from orchestrator.config import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    """启动 REST API 服务（CLI 入口在 pipeline 模块定稿后接入）。"""
    import uvicorn

    settings = Settings.from_env()
    logger.info("orchestrator 启动：%s", settings.public_summary)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
