# 构建上下文 = 仓库根（compose 已指向 ../..）：
# prompts/ 与 schemas/ 为运行必需（prompt 加载与产物 Schema 校验），随镜像分发
ARG UV_VERSION=0.12.9
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:3.11-slim

# python_scan 基线 diff（C-09）在进程内 subprocess 调 git，容器须自带
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/services/orchestrator

COPY --from=uv /uv /usr/local/bin/uv

COPY services/orchestrator/pyproject.toml services/orchestrator/uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project

COPY services/orchestrator/src ./src
RUN uv sync --frozen --no-dev
# E2E 为任务级可选能力；镜像预置 Chromium，运行时不下载浏览器，也不影响未启用 E2E 的任务。
RUN uv run playwright install --with-deps chromium

# runner 的 prompts_dir 按 parents[4]/prompts 解析 → /app/prompts；
# schemas 经 SCHEMAS_DIR 环境变量显式指定（默认相对 CWD 不适用于本布局）
COPY prompts /app/prompts
COPY schemas /app/schemas
ENV SCHEMAS_DIR=/app/schemas

EXPOSE 8000

CMD ["uv", "run", "python", "-m", "orchestrator", "serve"]
