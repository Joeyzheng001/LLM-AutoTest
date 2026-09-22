#!/usr/bin/env sh
set -eu

umask 077

# 备份只写到调用方指定目录，不自动删除旧备份。
backup_dir="${BACKUP_DIR:-./backups}"
# 秒级时间戳加 PID，避免同一秒内两次演练互相覆盖最终文件。
timestamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
compose_env_file="${COMPOSE_ENV_FILE:-deploy/.env}"
mkdir -p "$backup_dir"
output_file="$backup_dir/autotest-${timestamp}.dump"
temporary_file="$(mktemp "$backup_dir/.autotest-${timestamp}.XXXXXX.dump")"
cleanup() {
  rm -f "$temporary_file"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
docker compose --env-file "$compose_env_file" -f deploy/docker-compose.department.yml exec -T postgres \
  pg_dump --format=custom --no-owner --no-acl \
  -U "${POSTGRES_USER:-autotest}" -d "${POSTGRES_DB:-autotest}" \
  > "$temporary_file"
test -s "$temporary_file"
mv -f "$temporary_file" "$output_file"
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$(dirname "$output_file")" && sha256sum "$(basename "$output_file")" > "$(basename "$output_file").sha256")
elif command -v shasum >/dev/null 2>&1; then
  (cd "$(dirname "$output_file")" && shasum -a 256 "$(basename "$output_file")" > "$(basename "$output_file").sha256")
else
  echo "warning: sha256sum/shasum 不可用，未生成校验文件" >&2
fi
echo "backup=$output_file"
