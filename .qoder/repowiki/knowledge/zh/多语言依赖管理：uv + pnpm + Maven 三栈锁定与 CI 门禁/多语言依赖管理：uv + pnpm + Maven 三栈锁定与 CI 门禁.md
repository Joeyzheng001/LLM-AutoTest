---
kind: dependency_management
name: 多语言依赖管理：uv + pnpm + Maven 三栈锁定与 CI 门禁
category: dependency_management
scope:
    - '**'
source_files:
    - services/orchestrator/pyproject.toml
    - services/orchestrator/uv.lock
    - services/test-executor/pyproject.toml
    - services/test-executor/uv.lock
    - services/java-parser/pom.xml
    - services/java-parser/checkstyle.xml
    - services/frontend/package.json
    - services/frontend/pnpm-lock.yaml
    - .github/workflows/ci.yml
    - deploy/docker-compose.yml
---

## 1. 使用的工具与策略

本仓库采用**按服务分治的多语言依赖管理**，每个子服务独立声明依赖、独立锁定版本，并通过 GitHub Actions 统一校验。

- **Python（orchestrator、test-executor）**：使用 `pyproject.toml` 声明运行时与开发依赖（`dependencies` / `dependency-groups.dev`），构建后端为 `hatchling`；通过 `uv.lock` 进行精确版本锁定，CI 使用 `astral-sh/setup-uv@v5` 安装后执行 `uv sync`。Python 基线版本在 `requires-python = ">=3.11"` 中约束。
- **前端（services/frontend）**：使用 `package.json` + `pnpm-lock.yaml` 锁定依赖，`packageManager` 字段强制 `pnpm@9.15.0`；CI 通过 `pnpm/action-setup@v4` 安装 pnpm 并缓存 `pnpm-lock.yaml`，构建命令为 `pnpm build`。
- **Java（services/java-parser）**：使用 `pom.xml` 继承 `spring-boot-starter-parent:3.4.0`，通过 `<properties>` 集中声明 `javaparser.version`、`checkstyle.version` 等变量；CI 使用 `setup-java@v4` 配置 Temurin JDK 21 并缓存 Maven 仓库，执行 `mvn -B -ntp verify`。

## 2. 关键文件

| 语言 | 依赖声明 | 锁定/缓存 | 说明 |
|---|---|---|---|
| Python | `services/orchestrator/pyproject.toml`、`services/test-executor/pyproject.toml` | `uv.lock` | 运行依赖（fastapi、httpx、playwright 等）与开发依赖（pytest、ruff、mypy）分离 |
| Frontend | `services/frontend/package.json` | `pnpm-lock.yaml` | 固定 pnpm 版本，devDependencies 包含 eslint/prettier/vitest |
| Java | `services/java-parser/pom.xml` | Maven 本地缓存（CI cache） | 通过 properties 集中管理版本，禁止 SNAPSHOT 进入主干 |
| 编排 | `deploy/docker-compose.yml` | Docker image tag `:0.1.0` | 服务间通过环境变量 `JAVA_PARSER_URL` 通信 |
| CI | `.github/workflows/ci.yml` | 各语言缓存策略 | 统一触发 ruff/mypy/pytest、mvn verify、pnpm lint/build |

## 3. 架构与约定

- **每服务一个依赖清单**：Python 两个服务各自维护独立的 `pyproject.toml` 与 `uv.lock`，互不共享，避免跨服务依赖污染。
- **依赖分组**：Python 使用 `[dependency-groups]` 将测试/类型检查工具（pytest、ruff、mypy、types-*）归入 `dev`，与生产依赖解耦。
- **版本策略**：
  - Python 使用 `>=X.Y` 的宽松下限（如 `fastapi>=0.115`），由 `uv.lock` 锁定实际解析出的具体版本。
  - 前端使用 `^` 语义化版本（如 `vue: ^3.5.13`），由 pnpm lockfile 固化。
  - Java 通过 `<properties>` 变量集中声明版本号，便于统一升级。
- **构建产物镜像化**：Docker Compose 将各服务构建为 `llm-autotest/<service>:0.1.0` 镜像，作为部署时的“已验证依赖快照”。
- **私有仓库/代理**：当前所有 `uv.lock` 条目均指向 `https://pypi.org/simple`，未发现私有 registry 或 `GOFLAGS`/`PIP_INDEX_URL` 等代理配置。

## 4. 规范与约束

- **禁止 SNAPSHOT 依赖进入主干**：`pom.xml` 注释明确声明 “禁止 SNAPSHOT 依赖进入主干”，这是 Java 侧的硬性约束。
- **Java 代码风格强制**：`maven-checkstyle-plugin` 在 `verify` 阶段执行 `check` 目标且 `failOnViolation=true`，违反 checkstyle 规则会阻断构建。
- **Python 质量门禁**：CI 对 orchestrator 与 test-executor 同时执行 `ruff check`、`ruff format --check`、`mypy src`、`pytest -q`，任一失败即中断流水线。
- **前端构建门禁**：CI 执行 `pnpm lint`（eslint + prettier --check）和 `pnpm build`（vue-tsc + vite build），lint 或编译失败即失败。
- **容器资源约束**：java-parser 通过 `JAVA_TOOL_OPTIONS="-Xmx192m -Xms64m"` 显式限制 JVM 堆大小，适配 2C/888MB 小规格机器。
- **服务依赖顺序**：docker-compose 中 orchestrator 通过 `depends_on` + `condition: service_healthy` 确保 java-parser 健康后再启动；frontend 依赖 orchestrator 就绪。
- **覆盖率门禁预留**：CI 注释标注 “覆盖率门禁（≥70%）自 M1 起逐步启用”，表明 pytest 覆盖率尚未开启但已有规划。

## 5. 缺失项

- 未使用 Go 模块（无 `go.mod`/`go.sum`）。
- 未使用 npm/yarn（仅 pnpm）。
- 未配置私有 PyPI/Maven/NPM 源，全部依赖公共仓库。
- 未使用 `pip-tools`/`poetry`/`requirements.txt` 等替代方案，统一走 `pyproject.toml` + `uv`。
