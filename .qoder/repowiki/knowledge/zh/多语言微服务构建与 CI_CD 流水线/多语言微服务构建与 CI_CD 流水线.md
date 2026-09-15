---
kind: build_system
name: 多语言微服务构建与 CI/CD 流水线
category: build_system
scope:
    - '**'
source_files:
    - .github/workflows/ci.yml
    - deploy/docker-compose.yml
    - services/orchestrator/pyproject.toml
    - services/test-executor/pyproject.toml
    - services/java-parser/pom.xml
    - services/frontend/package.json
    - services/orchestrator/Dockerfile
    - services/java-parser/Dockerfile
    - services/frontend/Dockerfile
---

## 1. 使用的系统与工具

仓库采用**按服务拆分、各自独立构建**的多语言微服务架构，每个 `services/*` 子模块自带完整的依赖声明与构建配置：
- **Python 服务**（orchestrator、test-executor）：使用 `uv` 作为包管理与环境管理，`hatchling` 作为 Python 打包后端，`pyproject.toml` + `uv.lock` 锁定版本。
- **Java 服务**（java-parser）：基于 Spring Boot 3.4 + Maven，通过 `pom.xml` 管理依赖，启用 `maven-checkstyle-plugin` 在 `verify` 阶段强制代码规范。
- **前端**（frontend）：Vue 3 + Vite，使用 `pnpm@9` 作为包管理器，`package.json` 中定义 `build`/`lint`/`test` 脚本。
- **容器化**：每个服务提供独立 `Dockerfile`，编排统一由 `deploy/docker-compose.yml` 管理。
- **CI/CD**：GitHub Actions（`.github/workflows/ci.yml`）在 push PR 到 main 或触发 PR 时运行，覆盖 Python lint/typecheck/test、Java verify、前端 build、以及 Docker 镜像构建。

## 2. 关键文件

- `.github/workflows/ci.yml`：CI 流水线入口，定义 python/java/frontend/docker 四个 job。
- `deploy/docker-compose.yml`：本地与测试环境编排唯一事实来源，定义 java-parser / orchestrator / frontend / test-executor / mock-services 服务及依赖关系。
- `services/orchestrator/pyproject.toml`：orchestrator 的依赖、构建、ruff/mypy/pytest 配置。
- `services/test-executor/pyproject.toml`：test-executor 的依赖、构建、ruff/mypy/pytest 配置。
- `services/java-parser/pom.xml`：Maven 项目配置，绑定 checkstyle 到 verify 阶段并设置 `failOnViolation=true`。
- `services/frontend/package.json`：pnpm 脚本与依赖声明。
- 各服务 `Dockerfile`：orchestrator（python:3.11-slim + uv 多阶段）、java-parser（maven 构建 + JRE 运行）、frontend（nginx 占位镜像，待 V2.1 正式构建流程启用）。

## 3. 架构与约定

### 3.1 构建管线分层
- **开发期**：各服务通过自身工具链直接运行（`uv run pytest`、`mvn verify`、`pnpm build`）。
- **CI 期**：GitHub Actions 矩阵式并行执行 Python 两个服务的 ruff 检查、格式化校验、mypy 类型检查、pytest 测试；Java 服务执行 `mvn -B -ntp verify`；前端执行 pnpm install/lint/build。
- **制品期**：push 事件触发 `docker build`，为 orchestrator、java-parser、frontend 分别生成 `llm-autotest/{service}:ci` 镜像。
- **部署期**：`deploy/docker-compose.yml` 是编排的唯一入口，通过 `profiles` 控制可选服务（`exec` 启用 test-executor，`mock` 启用 mock-services），并通过 `depends_on` 与 `healthcheck` 保证启动顺序。

### 3.2 版本与产物命名
- 所有服务版本号统一为 `0.1.0`（见 pyproject.toml、pom.xml、package.json 及 docker-compose image tag）。
- Java 构建产物固定命名为 `java-parser-0.1.0.jar`，由 Dockerfile 硬拷贝至 `/app/app.jar`。
- 共享数据卷名为 `artifacts`，容器内挂载点统一为 `/data/llm-autotest`（见 docker-compose volumes 注释）。

### 3.3 环境与配置
- Python 服务通过 `uv sync --frozen` 确保依赖与 `uv.lock` 严格一致，生产镜像仅安装运行时依赖（`--no-dev --no-install-project`）。
- Java 服务通过 `maven:3.9-eclipse-temurin-21` 镜像进行无网络缓存构建（`-B -ntp`），运行镜像仅含 JRE。
- 前端 CI 使用 `pnpm-lock.yaml` 缓存依赖，构建命令为 `vue-tsc -b && vite build`。
- 环境变量通过 `env_file: .env` 注入 orchestrator，`JAVA_PARSER_URL` 等跨服务通信地址由 compose 自动解析。

## 4. 约定与约束

- **Python 代码质量门禁**：CI 对 orchestrator 与 test-executor 同时执行 `ruff check`、`ruff format --check`、`mypy src`、`pytest -q`，全部通过才视为成功。覆盖率门禁（≥70%）已预留位置但尚未启用（见 CI 注释）。
- **Java 代码规范强制**：`pom.xml` 将 checkstyle 绑定到 `verify` 阶段并设置 `failOnViolation=true`，禁止 SNAPSHOT 依赖进入主干（见 POM 注释）。
- **前端构建流程暂缓**：当前 frontend Dockerfile 仅为 nginx 占位镜像，正式 `vue-tsc + vite build → nginx` 流程待 V2.1 裁决后启用（见 Dockerfile 注释）。
- **服务启停顺序**：docker-compose 通过 `healthcheck`（java-parser 探测 `/health`）和 `depends_on.condition: service_healthy` 确保 orchestrator 在 java-parser 就绪后再启动；frontend 依赖 orchestrator。
- **可选服务隔离**：test-executor 与 mock-services 通过 `profiles: ["exec"]` / `["mock"]` 按需启用，默认 compose 不拉起。
- **Python 包结构**：两个 Python 服务均遵循 `src/<package>` 布局，hatchling 构建目标指向 `packages = ["src/xxx"]`。
- **Ruff 规则统一**：两个 Python 服务共享相同的 line-length=100、target-version=py311、select 规则集，并对中文注释场景关闭 RUF001/002/003（见 pyproject.toml 注释引用 docs/09 第二章）。
- **CI 触发条件**：仅在 push 到 main 分支或 PR 事件时运行；Docker 构建 job 额外限制为 `if: github.event_name == 'push'`，避免 PR 构建镜像。
