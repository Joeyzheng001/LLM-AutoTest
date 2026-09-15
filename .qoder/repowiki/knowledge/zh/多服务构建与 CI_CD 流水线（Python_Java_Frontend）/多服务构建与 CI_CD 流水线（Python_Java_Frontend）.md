---
kind: build_system
name: 多服务构建与 CI/CD 流水线（Python/Java/Frontend）
category: build_system
scope:
    - '**'
source_files:
    - .github/workflows/ci.yml
    - deploy/docker-compose.yml
    - deploy/.env.example
    - services/orchestrator/Dockerfile
    - services/orchestrator/pyproject.toml
    - services/test-executor/pyproject.toml
    - services/java-parser/Dockerfile
    - services/java-parser/pom.xml
    - services/frontend/Dockerfile
    - services/frontend/package.json
---

## 1. 构建系统总览

仓库采用**按服务拆分、统一 CI 编排**的多语言构建体系：
- Python 服务（`services/orchestrator`、`services/test-executor`）使用 **uv + hatchling**，依赖锁定通过 `uv.lock`。
- Java 服务（`services/java-parser`）使用 **Maven 3.9 + Spring Boot 3.4**，JDK 21 LTS。
- 前端（`services/frontend`）使用 **pnpm@9 + Vite + Vue 3 + TypeScript**。
- 容器化由每个服务的独立 `Dockerfile` 完成，本地/CI 统一通过 `deploy/docker-compose.yml` 编排。
- GitHub Actions（`.github/workflows/ci.yml`）在 push/PR 时并行执行 Python lint+test、Java verify、Frontend build、以及 Docker 镜像构建。

## 2. 关键文件与职责

| 文件 | 作用 |
|---|---|
| `.github/workflows/ci.yml` | 单一入口 CI：矩阵化运行 Python/Java/Frontend/Docker 构建任务 |
| `deploy/docker-compose.yml` | 开发/测试环境编排唯一事实来源，声明服务依赖、端口、健康检查、共享卷 `artifacts` |
| `deploy/.env.example` | LLM 调用参数模板（GLM-5.3 基线），禁止硬编码 |
| `services/*/Dockerfile` | 各服务容器镜像定义 |
| `services/orchestrator/pyproject.toml`、`services/test-executor/pyproject.toml` | Python 包元数据、依赖、ruff/mypy/pytest 配置 |
| `services/java-parser/pom.xml` | Maven 构建、checkstyle 质量门禁 |
| `services/frontend/package.json` | pnpm 脚本（dev/build/lint/test） |

## 3. 架构与约定

### 3.1 Python 服务构建
- 双阶段 uv 安装：先 `uv sync --frozen --no-dev --no-install-project` 安装依赖缓存，再 `uv sync --frozen --no-dev` 安装项目代码，实现镜像层复用。
- 运行时通过 `uv run python -m orchestrator` 启动，暴露 8000 端口。
- 依赖版本锁定在 `uv.lock`，CI 中 `uv sync` 严格冻结。
- 代码质量：`ruff check/format` + `mypy strict` + `pytest`，lint 规则在 `pyproject.toml` 的 `[tool.ruff]` 中集中声明，行宽 100，目标 py311。
- 中文注释全角标点通过忽略 RUF001/002/003 保留（见两个 pyproject.toml 注释引用 docs/09 第二章）。

### 3.2 Java 服务构建
- 多阶段 Docker 构建：第一阶段 `maven:3.9-eclipse-temurin-21` 执行 `mvn -B -ntp package`，第二阶段 `eclipse-temurin:21-jre` 仅拷贝产物 `java-parser-0.1.0.jar`。
- `pom.xml` 继承 `spring-boot-starter-parent:3.4.0`，固定 `java.version=21`。
- Checkstyle 在 `verify` 阶段执行且 `failOnViolation=true`，作为质量门禁。
- 注释明确“禁止 SNAPSHOT 依赖进入主干”。

### 3.3 前端构建
- 当前 Dockerfile 为占位镜像（nginx:alpine + 静态 HTML），正式 `vue-tsc + vite build → nginx` 流程待 V2.1 裁决后启用（Dockerfile 顶部注释）。
- CI 中执行 `pnpm install --no-frozen-lockfile`、`pnpm lint`（eslint + prettier --check）、`pnpm build`（vue-tsc -b && vite build）。
- 包管理器锁定在 `packageManager: "pnpm@9.15.0"`，CI 使用 pnpm/action-setup@v4。

### 3.4 编排与部署
- `docker-compose.yml` 是“编排唯一事实来源”（注释引用 docs/09 第十一章），声明 `java-parser` → `orchestrator` → `frontend` 的依赖链，`test-executor` 和 `mock-services` 通过 profiles (`exec`、`mock`) 按需启用。
- 共享卷 `artifacts` 挂载到 `/data/llm-autotest`，所有服务产物路径统一于此（注释引用 docs/09 第十一章）。
- `java-parser` 提供 `/health` 端点，compose 用 wget 做 healthcheck；`orchestrator` 通过 `JAVA_PARSER_URL` 环境变量发现下游。
- 环境变量通过 `--env-file deploy/.env` 注入，LLM 密钥等敏感信息不入库。

### 3.5 CI 策略
- 触发条件：push 到 `main` 或任意 PR。
- 并行矩阵：Python 对 `orchestrator`、`test-executor` 分别执行；Java、Frontend、Docker 构建各自独立 job。
- Docker 构建 job 仅在 `push` 事件触发（`if: github.event_name == 'push'`），镜像命名 `llm-autotest/<service>:ci`。
- Python 覆盖率门禁（≥70%）已预留注释，自 M1 起逐步启用。

## 4. 规范与约束

- **Python 版本基线**：所有 Python 服务要求 `requires-python = ">=3.11"`，mypy/ruff target-version 均为 `py311`。
- **Java 版本基线**：JDK 21 LTS + Maven 3.9，Spring Boot 3.4.0，禁止 SNAPSHOT 依赖进入主干。
- **前端工具链**：pnpm@9、Node 20、Vue 3 + TypeScript + Vite 6。
- **镜像标签**：compose 中镜像统一为 `<image>:0.1.0`，CI 构建使用 `:ci` 标签。
- **健康检查**：java-parser 必须暴露 `/health`，compose 以 wget 探测。
- **环境变量**：LLM 相关参数（API Key、Base URL、Model、Temperature、Top-P）必须通过 `.env` 注入，禁止硬编码（`.env.example` 注释引用 docs/09 第五章红线）。
- **产物目录**：所有服务共享 `/data/llm-autotest` 卷用于持久化产物（compose 注释引用 docs/09 第十一章）。
- **质量门禁**：Python 走 ruff + mypy + pytest；Java 走 maven-checkstyle（verify 阶段 failOnViolation）；Frontend 走 eslint + prettier + vue-tsc。