#!/usr/bin/env sh
set -eu

# 在无公网出站的部门服务器上，为“仅源码/Prompt/Schema 变化”构建编排服务覆盖镜像。
# 不能替代常规完整构建：依赖锁文件、Dockerfile 或 OS 依赖发生变化时必须先导入完整镜像。

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
base_image=${ORCHESTRATOR_BASE_IMAGE:-llm-autotest/orchestrator:0.1.0}
target_image=${ORCHESTRATOR_IMAGE:-}

if [ -z "$target_image" ]; then
  echo "离线覆盖构建需要显式设置 ORCHESTRATOR_IMAGE（例如 llm-autotest/orchestrator:release-20260923）" >&2
  exit 2
fi

if ! docker image inspect "$base_image" >/dev/null 2>&1; then
  echo "未找到基础镜像：$base_image；请先通过受控镜像仓库或镜像导入流程准备它" >&2
  exit 2
fi

cd "$repo_root"
docker build --pull=false \
  --build-arg "BASE_IMAGE=$base_image" \
  -f deploy/Dockerfile.orchestrator.overlay \
  -t "$target_image" \
  .

echo "离线覆盖镜像已构建：$target_image（基础镜像：$base_image）"
