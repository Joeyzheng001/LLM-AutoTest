#!/usr/bin/env sh
set -eu

# 部门共享部署的一键启动编排。
#
# 该脚本只读取 deploy/.env，不打印其内容，也不接收密码参数：
#   COMPOSE_ENV_FILE=deploy/.env APP_URL=http://127.0.0.1:8080 \
#     sh deploy/department_bootstrap.sh
#
# 需要回滚或演练时请先停止服务并使用 docs/17 的备份/恢复流程；脚本不会删除卷、
# 数据库或旧任务产物。默认启动两个 orchestrator 实例，避免 docker compose 的
# deploy.replicas 在非 Swarm 模式下被静默忽略。

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compose_env_file=${COMPOSE_ENV_FILE:-deploy/.env}
compose_file=${COMPOSE_FILE:-deploy/docker-compose.department.yml}
app_url=${APP_URL:-http://127.0.0.1:8080}
scale=${ORCHESTRATOR_REPLICAS:-2}
postgres_wait_s=${POSTGRES_WAIT_S:-120}
acceptance_timeout=${ACCEPTANCE_TIMEOUT_S:-5}
acceptance_retries=${ACCEPTANCE_RETRIES:-2}
acceptance_insecure_tls=${ACCEPTANCE_INSECURE_TLS:-0}
dry_run=${DRY_RUN:-0}

case "$compose_env_file" in
  /*) compose_env_path=$compose_env_file ;;
  *) compose_env_path=$repo_root/$compose_env_file ;;
esac

case "$scale" in
  ''|*[!0-9]*) echo "ORCHESTRATOR_REPLICAS 必须是正整数" >&2; exit 2 ;;
esac
if [ "$scale" -lt 1 ]; then
  echo "ORCHESTRATOR_REPLICAS 必须大于 0" >&2
  exit 2
fi
case "$acceptance_insecure_tls" in
  0|1) ;;
  *) echo "ACCEPTANCE_INSECURE_TLS 必须是 0 或 1" >&2; exit 2 ;;
esac
case "$postgres_wait_s" in
  ''|*[!0-9]*) echo "POSTGRES_WAIT_S 必须是非负整数" >&2; exit 2 ;;
esac

if [ ! -f "$compose_env_path" ]; then
  echo "找不到 Compose 环境文件：$compose_env_path" >&2
  exit 2
fi

compose() {
  if [ "$dry_run" = "1" ]; then
    printf '+ docker compose --env-file %s -f %s' "$compose_env_path" "$compose_file"
    for arg in "$@"; do printf ' %s' "$arg"; done
    printf '\n'
    return 0
  fi
  docker compose --env-file "$compose_env_path" -f "$compose_file" "$@"
}

wait_for_postgres() {
  if [ "$dry_run" = "1" ]; then
    printf '+ wait for postgres health\n'
    return 0
  fi
  deadline=$(( $(date +%s) + postgres_wait_s ))
  while :; do
    if compose exec -T postgres pg_isready \
      -U "${POSTGRES_USER:-autotest}" -d "${POSTGRES_DB:-autotest}" >/dev/null 2>&1; then
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "PostgreSQL 在 ${postgres_wait_s}s 内未就绪" >&2
      return 1
    fi
    sleep 2
  done
}

cd "$repo_root"

# config 会触发 Compose 的 required 变量检查，但不启动容器、不输出 .env 内容。
compose config --quiet
compose build orchestrator java-parser frontend
compose run --rm --no-deps orchestrator \
  uv run python -m orchestrator validate-deployment

compose up -d postgres
wait_for_postgres
compose run --rm --no-deps orchestrator \
  uv run python -m orchestrator migrate-db

# 先启动解析器，随后按显式副本数启动编排服务和统一入口。
compose up -d java-parser
compose up -d --scale "orchestrator=$scale" orchestrator frontend prometheus

if [ "$dry_run" = "1" ]; then
  printf '+ python deploy/acceptance_smoke.py --url %s --timeout %s --retries %s' \
    "$app_url" "$acceptance_timeout" "$acceptance_retries"
  if [ "$acceptance_insecure_tls" = "1" ]; then printf ' --insecure-tls'; fi
  printf '\n'
  exit 0
fi

if [ "$acceptance_insecure_tls" = "1" ]; then
  python3 deploy/acceptance_smoke.py \
    --url "$app_url" \
    --timeout "$acceptance_timeout" \
    --retries "$acceptance_retries" \
    --insecure-tls
else
  python3 deploy/acceptance_smoke.py \
    --url "$app_url" \
    --timeout "$acceptance_timeout" \
    --retries "$acceptance_retries"
fi

echo "部门共享服务已启动并通过只读入口验收：$app_url"
