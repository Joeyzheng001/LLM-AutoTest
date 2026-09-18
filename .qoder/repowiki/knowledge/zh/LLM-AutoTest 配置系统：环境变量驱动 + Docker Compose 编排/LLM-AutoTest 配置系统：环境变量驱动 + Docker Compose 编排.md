---
kind: configuration_system
name: LLM-AutoTest 配置系统：环境变量驱动 + Docker Compose 编排
category: configuration_system
scope:
    - '**'
source_files:
    - deploy/docker-compose.yml
    - deploy/.env.example
    - services/orchestrator/src/orchestrator/config.py
    - services/orchestrator/src/orchestrator/__main__.py
    - services/java-parser/src/main/resources/application.yml
    - services/java-parser/src/main/java/com/llmautotest/parser/ParserProperties.java
    - services/frontend/vite.config.ts
    - deploy/nginx.conf
    - services/orchestrator/pyproject.toml
---

## 1. 总体方案

仓库采用**“环境变量即配置”**的 12-Factor 风格，所有运行时参数通过环境变量注入，禁止在代码中硬编码。配置加载集中在各服务的入口模块，由 `docker-compose.yml` 统一注入，配合 `.env.example` 提供默认值与说明。

- Python 服务（orchestrator、test-executor）使用 `os.environ` + `dataclass` 显式读取并校验。
- Java 服务（java-parser）使用 Spring Boot `@ConfigurationProperties(prefix = "parser")` 绑定 `application.yml`，并通过环境变量覆盖。
- 前端（Vite）仅保留开发代理占位，生产构建产物由 nginx 反代。
- 部署层通过 `deploy/docker-compose.yml` 集中声明环境变量、卷挂载、健康检查与服务依赖。

## 2. 关键文件

| 层级 | 文件 | 作用 |
|---|---|---|
| 部署编排 | `deploy/docker-compose.yml` | 唯一事实来源，声明服务、端口、环境变量、卷、healthcheck、profile |
| 环境模板 | `deploy/.env.example` | LLM 调用参数示例（API Key、Base URL、模型名、温度、Top-P、Seed） |
| Orchestrator 配置 | `services/orchestrator/src/orchestrator/config.py` | `Settings` dataclass，从环境变量构建，缺失必填项抛 `RuntimeError` |
| Orchestrator 入口 | `services/orchestrator/src/orchestrator/__main__.py` | CLI 子命令 `run/status/serve`，统一 `Settings.from_env()` 加载配置 |
| Java 配置 | `services/java-parser/src/main/resources/application.yml` | `server.port`、`parser.scan-concurrency`、`parser.max-files-per-repo` |
| Java 属性类 | `services/java-parser/src/main/java/com/llmautotest/parser/ParserProperties.java` | `@ConfigurationProperties(prefix="parser")`，构造器校验正数约束 |
| 前端代理 | `services/frontend/vite.config.ts` | dev 代理 `/api` → `http://localhost:8000` |
| 反向代理 | `deploy/nginx.conf` | `/api` 转发 orchestrator，`client_max_body_size=64m` 对齐 API 限制 |
| 依赖/工具 | `services/orchestrator/pyproject.toml` | ruff/mypy/pytest 等工具链配置 |

## 3. 架构与设计约定

### 3.1 配置分层

1. **默认值**：写在 `from_env` / `application.yml` / `.env.example` 中，作为本地开发与演示基线。
2. **环境变量覆盖**：`docker-compose.yml` 通过 `environment:` 和 `env_file:` 注入实际值；Java 侧 Spring Boot 支持环境变量覆盖 YAML 键。
3. **启动时校验**：
   - Orchestrator：`Settings.from_env()` 捕获 `KeyError` 并包装为 `RuntimeError("缺少必填环境变量：...")`，进程直接失败。
   - Java Parser：`ParserProperties` 构造器对 `scanConcurrency`、`maxFilesPerRepo` 做正数校验，非法则抛 `IllegalArgumentException`。

### 3.2 敏感信息与采样参数

- LLM 相关变量（`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL_NAME`、`LLM_TEMPERATURE`、`LLM_TOP_P`、`LLM_SEED`、`LLM_TIMEOUT_S`、`LLM_MAX_TOKENS`）全部通过环境变量传入，不写入代码。
- `Settings.public_summary` 专门生成不含密钥的配置摘要用于日志输出，避免泄露。
- 文档红线（docs/09 第五章）明确要求：**采样参数禁止硬编码，仅允许 GLM-5.3（冻结基线）**。

### 3.3 产物路径与 Schema 目录

- `ARTIFACTS_ROOT`（默认 `/data/llm-autotest`）是 orchestrator 与 test-executor 共享的产物卷挂载点，由 docker-compose volume `artifacts` 持久化。
- `SCHEMAS_DIR`（默认 `schemas`）指向仓库内 JSON Schema 目录，用于产物校验。
- Java Parser 通过 `JAVA_TOOL_OPTIONS=-Xmx192m -Xms64m` 在容器内限制 JVM 堆大小，适配 2C/888MB 小规格机器。

### 3.4 服务间通信配置

- `JAVA_PARSER_URL` 由 compose 注入 `http://java-parser:8080`，orchestrator 通过 HTTPX 客户端调用。
- 前端通过 nginx 反代 `/api` 到 orchestrator:8000，nginx 配置 `proxy_read_timeout=300s` 以匹配长耗时任务。

## 4. 约定与约束

| 规则 | 来源/依据 | 说明 |
|---|---|---|
| 运行时配置必须来自环境变量，禁止硬编码 | `config.py` 注释 docs/09 第五章；`ParserProperties` 注释 docs/09 第三章 | 所有服务遵循同一模式 |
| 必填环境变量缺失时启动失败 | `Settings.from_env` 捕获 `KeyError` 抛 `RuntimeError` | 保证运行期可观测性 |
| 并发与截断参数必须为正数 | `ParserProperties` 构造器校验 | 防止资源耗尽或空循环 |
| LLM 采样参数仅允许 GLM-5.3 | `.env.example` 注释 docs/09 第五章 | 冻结基线，便于结果复现 |
| 产物卷统一挂载 `/data/llm-autotest` | `docker-compose.yml` 注释 docs/09 第十一章 | orchestrator/test-executor/java-parser 共享 |
| 请求体上限 64MB | `nginx.conf` 注释 C-12 | 与 API 层限额对齐 |
| 前端 dev 代理指向 orchestrator 8000 | `vite.config.ts` | 开发阶段前后端联调 |
| 测试代码允许 S101（assert 使用） | `pyproject.toml` ruff per-file-ignores | 区分业务与测试约束 |
| 服务入口必须绑定 `0.0.0.0` | `__main__.py` ruff per-file-ignore S104 | 容器网络暴露必需 |

## 5. 总结

该仓库的配置系统以**环境变量为中心**，通过 `docker-compose.yml` 作为编排唯一事实来源，在各服务内部以强类型 dataclass / `@ConfigurationProperties` 进行解析与校验。敏感信息、模型参数、路径等全部外置，启动时严格校验，确保多服务、多环境下的配置一致性与可追溯性。