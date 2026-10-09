#!/usr/bin/env bash
# 本地开发启动 orchestrator（不依赖 Docker）。
#
# 从 deploy/.env 提取两个密钥，其余键显式指定：
#   - SECRETS_ENCRYPTION_KEY：任务凭据（LLM Key / Git Token / 鉴权）的 Fernet 加密主密钥，
#     启用后任务 key 加密落盘到 <job>/.job-credentials.enc，后端重启不丢。
#   - LLM_API_KEY：服务级中转站 Key，作为任务级 Key 的兜底（任务未填 key 时回退）。
#
# 注意：这里刻意【不】source 整个 deploy/.env，因为其中含 DATABASE_URL / POSTGRES_*
# 等 Docker 部署专用键，本地未起 PostgreSQL 时会拖垮启动。
# 每个键都可用同名环境变量覆盖（已导出的优先）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/deploy/.env"

if [[ -f "$ENV_FILE" ]]; then
  export SECRETS_ENCRYPTION_KEY="${SECRETS_ENCRYPTION_KEY:-$(grep '^SECRETS_ENCRYPTION_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")}"
  export LLM_API_KEY="${LLM_API_KEY:-$(grep '^LLM_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")}"
fi

export JAVA_PARSER_URL="${JAVA_PARSER_URL:-http://localhost:8081}"
export LLM_BASE_URL="${LLM_BASE_URL:-https://llm.htffund.com/v1}"
export LLM_MODEL_NAME="${LLM_MODEL_NAME:-deepseek-reasoner-code}"
export SCHEMAS_DIR="${SCHEMAS_DIR:-$ROOT/schemas}"
export ARTIFACTS_ROOT="${ARTIFACTS_ROOT:-/tmp/llm-autotest}"
export REPOSITORIES_ROOT="${REPOSITORIES_ROOT:-$HOME/code}"
export AUTOTEST_SINGLE_USER_MODE="${AUTOTEST_SINGLE_USER_MODE:-true}"
# Windows 上 Python 默认按系统 ANSI 代码页（cp936）写 stdout/stderr，中文日志落到终端
# 或重定向文件后即乱码（容器内默认 UTF-8，不受影响）。统一为 UTF-8，使本地与生产
# 口径一致，并让未显式指定 encoding 的文件读写也走 UTF-8。
export PYTHONUTF8="${PYTHONUTF8:-1}"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

cd "$ROOT/services/orchestrator"
exec uv run python -m orchestrator serve "$@"
