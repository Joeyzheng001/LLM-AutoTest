#!/usr/bin/env sh
set -eu

umask 077

# 在独立恢复库中演练，不触碰生产数据库；RESTORE_DB_NAME 必须使用专用名称。
dump_file="${1:?usage: restore_rehearsal.sh path/to/autotest.dump}"
restore_db="${RESTORE_DB_NAME:-autotest_restore_check}"
compose_env_file="${COMPOSE_ENV_FILE:-deploy/.env}"
if [ ! -s "$dump_file" ]; then
  echo "恢复文件不存在或为空：$dump_file" >&2
  exit 2
fi
checksum_file="$dump_file.sha256"
if command -v sha256sum >/dev/null 2>&1 && [ -f "$checksum_file" ]; then
  (cd "$(dirname "$dump_file")" && sha256sum -c "$(basename "$checksum_file")")
elif command -v shasum >/dev/null 2>&1 && [ -f "$checksum_file" ]; then
  expected="$(awk '{print $1}' "$checksum_file")"
  actual="$(shasum -a 256 "$dump_file" | awk '{print $1}')"
  [ "$expected" = "$actual" ] || { echo "恢复文件校验失败：$dump_file" >&2; exit 2; }
fi
# 在删除隔离恢复库前先确认 dump 可被 pg_restore 解析，避免把灾备演练错误归因成数据库问题。
cat "$dump_file" | docker compose --env-file "$compose_env_file" -f deploy/docker-compose.department.yml exec -T postgres \
  pg_restore --list > /dev/null
case "$restore_db" in
  autotest|postgres|template0|template1) echo "拒绝使用生产/系统数据库名" >&2; exit 2 ;;
esac
docker compose --env-file "$compose_env_file" -f deploy/docker-compose.department.yml exec -T postgres \
  dropdb --if-exists -U "${POSTGRES_USER:-autotest}" "$restore_db"
docker compose --env-file "$compose_env_file" -f deploy/docker-compose.department.yml exec -T postgres \
  createdb -U "${POSTGRES_USER:-autotest}" "$restore_db"
cat "$dump_file" | docker compose --env-file "$compose_env_file" -f deploy/docker-compose.department.yml exec -T postgres \
  pg_restore --clean --if-exists --no-owner --no-acl \
  -U "${POSTGRES_USER:-autotest}" -d "${restore_db}"
echo "restore rehearsal completed: database=$restore_db"
