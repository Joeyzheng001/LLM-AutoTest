---
kind: dependency_management
name: 多语言服务依赖管理：uv + Maven + pnpm 分模块锁定与 CI 门禁
category: dependency_management
scope:
    - '**'
source_files:
    - services/orchestrator/pyproject.toml
    - services/orchestrator/uv.lock
    - services/test-executor/pyproject.toml
    - services/test-executor/uv.lock
    - services/java-parser/pom.xml
    - services/frontend/package.json
    - services/frontend/pnpm-lock.yaml
    - .github/workflows/ci.yml
---

## 1. 整体方案
本仓库采用**按服务拆分、各语言使用各自生态的包管理器**的多语言依赖管理模式，每个 `services/*` 子目录独立声明依赖并配套锁文件，由 GitHub Actions 统一执行构建与质量门禁。

- Python 服务（`orchestrator`、`test-executor`）：使用 **uv**（`pyproject.toml` + `uv.lock`），通过 `[dependency-groups]` 区分运行期与开发期依赖，构建后端为 `hatchling`。
- Java 服务（`java-parser`）：使用 **Maven**（`pom.xml`），继承 `spring-boot-starter-parent:3.4.0`，通过 `<properties>` 集中声明版本变量。
- 前端（`frontend`）：使用 **pnpm**（`package.json` + `pnpm-lock.yaml`），通过 `packageManager` 字段锁定 pnpm 版本。

所有服务均通过 `.github/workflows/ci.yml` 在 push/PR 时并行执行 lint、类型检查、测试与 Docker 构建，形成统一的依赖可重现流水线。

## 2. 关键文件
- `services/orchestrator/pyproject.toml`、`services/orchestrator/uv.lock`：Python 编排服务的依赖与完整解析快照。
- `services/test-executor/pyproject.toml`、`services/test-executor/uv.lock`：测试执行服务的轻量依赖声明。
- `services/java-parser/pom.xml`：Java 服务 POM，含 Spring Boot 父工程、JavaParser 及 Checkstyle 插件配置。
- `services/frontend/package.json`、`services/frontend/pnpm-lock.yaml`：Vue 3 + Vite 前端依赖与 pnpm 锁定。
- `.github/workflows/ci.yml`：CI 中各语言的依赖安装与验证步骤（`uv sync`、`mvn verify`、`pnpm install --no-frozen-lockfile`）。

## 3. 架构与约定
- **每服务一个 manifest + 一个 lockfile**：Python 两个服务各自维护独立的 `pyproject.toml` 和 `uv.lock`；Java 仅一个服务；前端独立。不存在跨服务共享的顶层依赖清单。
- **Python 依赖分组**：通过 `[dependency-groups].dev` 将 pytest、ruff、mypy、types-* 等工具与运行时依赖分离，避免生产镜像携带开发依赖。
- **Python 最低版本约束**：两个 Python 服务均在 `requires-python = ">=3.11"` 中声明基线，CI 也固定使用 `python-version: "3.11"`。
- **Java 版本集中化**：POM 中通过 `<java.version>21</java.version>`、`<javaparser.version>3.26.2</javaparser.version>` 等属性统一管理版本，并在注释中明确“禁止 SNAPSHOT 依赖进入主干”。
- **前端包管理器锁定**：`package.json` 中声明 `"packageManager": "pnpm@9.15.0"`，CI 通过 `pnpm/action-setup@v4 with version: 9` 复用，lockfile 路径显式缓存。
- **无私有仓库/代理配置**：所有 lockfile 中的 `source.registry` 均为 `https://pypi.org/simple`，未发现 `.npmrc`、`.pypirc`、`settings.xml` 等私有源配置，依赖来源为公共 PyPI / Maven Central / npm registry。
- **无 vendoring**：未检出 `vendor/`、`node_modules/`、`.m2/repository` 等本地打包产物，依赖通过包管理器下载。

## 4. 约定与约束
- **Python**：依赖以 `>=X.Y` 形式声明最小兼容版本（如 `fastapi>=0.115`、`pydantic>=2.9`），实际解析结果由 `uv.lock` 固化；CI 强制 `uv run ruff check/format` 与 `uv run mypy src`，不通过则阻断合并。
- **Java**：POM 注释明文规定“基线：Java 21 LTS + Maven；禁止 SNAPSHOT 依赖进入主干”，并通过 maven-checkstyle-plugin 在 `verify` 阶段 `failOnViolation=true` 强制执行代码规范。
- **前端**：CI 使用 `pnpm install --no-frozen-lockfile` 允许 lockfile 更新，但后续 `pnpm build` 会基于该 lockfile 构建，保证构建可重现。
- **CI 门禁**：`ci.yml` 对三个语言分别执行依赖安装与校验，任何一步失败都会导致 PR 无法合并，从而间接约束依赖变更必须通过 CI。
- **Docker 构建**：`docker` job 仅在 push 到 main 时触发，对 orchestrator、java-parser、frontend 分别执行 `docker build`，依赖状态由对应服务的 lockfile 决定。