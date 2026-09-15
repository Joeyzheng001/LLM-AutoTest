---
kind: dependency_management
name: 多语言服务依赖管理：pnpm + uv.lock + Maven POM 锁定策略
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
---

## 1. 使用的系统/工具

仓库采用**按服务拆分、按语言选择包管理器**的多语言依赖管理模式：
- **Python 服务**（`services/orchestrator`、`services/test-executor`）：使用 `uv` 作为包管理器与解析器，通过 `pyproject.toml` 声明运行时与开发依赖，并通过同目录下的 `uv.lock` 进行精确版本锁定。
- **Java 服务**（`services/java-parser`）：使用 Maven，通过 `pom.xml` 声明依赖，继承 `spring-boot-starter-parent:3.4.0` 作为 BOM 管理 Spring 生态版本；第三方库版本通过 `<properties>` 中的变量集中声明（如 `javaparser.version`、`checkstyle.version`）。
- **前端服务**（`services/frontend`）：使用 `pnpm@9.15.0`（由 `package.json` 的 `packageManager` 字段强制），通过 `package.json` 的 `dependencies` / `devDependencies` 声明依赖，未提交 `pnpm-lock.yaml`。

所有 Python 服务的 `uv.lock` 均指向 `https://pypi.org/simple` 公共 PyPI 源，未发现私有 registry 或 `uv` 镜像配置。

## 2. 关键文件

- `services/orchestrator/pyproject.toml`、`services/orchestrator/uv.lock`：编排服务依赖声明与锁定。
- `services/test-executor/pyproject.toml`、`services/test-executor/uv.lock`：测试执行服务依赖声明与锁定。
- `services/java-parser/pom.xml`：Java 服务依赖与构建插件配置。
- `services/frontend/package.json`：前端依赖与脚本入口。

## 3. 架构与约定

- **每服务独立依赖**：每个 `services/*` 子目录都是独立的包，拥有自己的 `pyproject.toml` / `pom.xml` / `package.json`，不存在跨服务共享的顶层依赖清单。
- **Python 依赖分组**：通过 `[dependency-groups]` 将 `dev` 组（pytest、ruff、mypy）与生产依赖分离，仅生产依赖进入运行环境。
- **构建后端统一**：两个 Python 服务均使用 `hatchling` 作为 build-backend，wheel 打包目标为 `src/<package>` 结构。
- **Java 版本基线**：`pom.xml` 注释明确“基线：Java 21 LTS + Maven”，且禁止 SNAPSHOT 依赖进入主干。
- **前端包管理器锁定**：通过 `packageManager: pnpm@9.15.0` 在 `package.json` 中固定 pnpm 版本，确保团队一致。
- **无 vendoring**：仓库中未发现 `vendor/`、`node_modules/`、`.venv/` 等已提交的第三方代码，依赖全部通过包管理器从远程源拉取。

## 4. 约定与约束

- **Python 依赖必须提交 `uv.lock`**：两个 Python 服务都提交了 `uv.lock`，用于锁定所有传递依赖的精确版本与哈希，保证可复现构建。
- **Python 最低版本要求**：`requires-python = ">=3.11"`，并在 `uv.lock` 中通过 `resolution-markers` 区分 `python_full_version >= '3.15'` 与 `< '3.15'` 两种解析路径。
- **Java 禁止 SNAPSHOT 依赖**：`pom.xml` 注释明确要求“禁止 SNAPSHOT 依赖进入主干”。
- **Java 第三方库版本集中管理**：通过 `<properties>` 定义 `javaparser.version`、`maven-checkstyle-plugin.version`、`checkstyle.version`，避免硬编码版本号散落各处。
- **Maven checkstyle 在 verify 阶段失败即中断**：`maven-checkstyle-plugin` 配置 `failOnViolation=true`，在 `verify` 阶段执行，违反检查规则会阻断构建。
- **前端 devDependencies 与 dependencies 分离**：Vue/Vite/Eslint/Prettier 等工具链放在 `devDependencies`，仅 `vue` 放入 `dependencies`。
- **未发现私有仓库或代理配置**：所有 `uv.lock` 条目来源均为 `https://pypi.org/simple`，未见 `.netrc`、`pip.conf`、`settings.xml` 等私有源配置。