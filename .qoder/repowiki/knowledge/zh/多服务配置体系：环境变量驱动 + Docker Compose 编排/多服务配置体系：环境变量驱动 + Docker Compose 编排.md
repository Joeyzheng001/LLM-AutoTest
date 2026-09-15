---
kind: configuration_system
name: 多服务配置体系：环境变量驱动 + Docker Compose 编排
category: configuration_system
scope:
    - '**'
source_files:
    - services/orchestrator/src/orchestrator/config.py
    - services/java-parser/src/main/resources/application.yml
    - services/java-parser/src/main/java/com/llmautotest/parser/ParserProperties.java
    - deploy/.env.example
    - deploy/docker-compose.yml
    - services/frontend/vite.config.ts
    - services/orchestrator/pyproject.toml
    - services/test-executor/pyproject.toml
    - services/frontend/package.json
---

## 1. 采用的系统/方法

仓库采用**以环境变量为核心的配置加载模式**，各服务通过各自语言的标准方式读取运行时参数，并由 `deploy/docker-compose.yml` 统一注入。具体：
- Python（orchestrator、test-executor）：使用 `os.environ` + `dataclass` 强类型封装，缺失必填项时抛出 `RuntimeError`。
- Java（java-parser）：Spring Boot `application.yml` + `@ConfigurationProperties` 绑定到 record，启动时校验。
- Frontend（Vue/Vite）：开发期通过 `vite.config.ts` 硬编码代理目标，生产构建产物无外部配置入口。
- 部署层：`docker-compose.yml` 通过 `environment` 与 `env_file` 将 `.env` 中的变量注入容器；`.env.example` 提供模板。

## 2. 关键文件

- `services/orchestrator/src/orchestrator/config.py` — orchestrator 的 Settings dataclass，集中声明所有运行配置字段及默认值。
- `services/java-parser/src/main/resources/application.yml` — Spring Boot 配置文件，定义 `server.port` 与自定义 `parser.scan-concurrency`。
- `services/java-parser/src/main/java/com/llmautotest/parser/ParserProperties.java` — 将 `parser.*` 前缀映射为 record，并在构造中强制 `scanConcurrency > 0`。
- `deploy/.env.example` — LLM 调用参数的示例模板（API Key、Base URL、模型名、温度、Top-P、Seed）。
- `deploy/docker-compose.yml` — 编排唯一事实来源，声明服务依赖、端口映射、卷挂载、健康检查、profiles（exec/mock）。
- `services/frontend/vite.config.ts` — 前端开发代理配置，指向本地 orchestrator 8000 端口。
- `services/orchestrator/pyproject.toml` / `services/test-executor/pyproject.toml` / `services/frontend/package.json` — 各子项目依赖与构建配置。

## 3. 架构与约定

### 配置分层
1. **默认值层**：每个配置字段在代码中给出合理默认（如 `PORT=8000`、`LLM_MODEL_NAME=glm-5.3`、`ARTIFACTS_ROOT=/data/llm-autotest`、`SCHEMAS_DIR=schemas`、`LLM_TEMPERATURE=0.2`、`LLM_TOP_P=0.8`、`LLM_TIMEOUT_S=120`、`parser.scan-concurrency=4`）。
2. **环境变量覆盖层**：通过 `os.environ` / Spring Boot 自动绑定覆盖默认值。
3. **Compose 注入层**：`docker-compose.yml` 通过 `env_file: .env` 和显式 `environment:` 键把变量传入容器。
4. **Schema 契约层**：`schemas/` 目录集中定义 orchestrator、java-parser、test-executor 之间的 JSON 产物结构，作为跨服务数据契约。

### 安全与可观测性约定
- 敏感字段（`LLM_API_KEY`）允许为空（本地 Mock/内网网关免鉴权场景），且明确“入参不进日志”。
- `Settings.public_summary` 属性生成不含密钥的配置摘要，用于日志输出。
- LLM 采样参数（temperature、top_p、seed、timeout）全部从环境变量读取，禁止硬编码（注释引用 docs/09 第五章）。
- 仅允许 GLM-5.3 模型（冻结基线），模型名有默认值但仍可从环境变量覆盖。

### 服务间通信配置
- orchestrator 通过 `JAVA_PARSER_URL` 环境变量发现 java-parser 服务地址，由 compose 固定为 `http://java-parser:8080`。
- 前端开发期通过 Vite proxy 将 `/api` 请求转发到 `http://localhost:8000`（即本地 orchestrator）。

### 部署与生命周期
- 共享卷 `artifacts:/data/llm-autotest` 在所有服务间复用，作为产物持久化路径。
- java-parser 暴露 `/health` 端点，compose 用 `wget` 做健康检查，orchestrator 依赖其 healthy 状态后再启动。
- test-executor 与 mock-services 通过 `profiles: ["exec"]` / `["mock"]` 按需启用，避免默认启动无关组件。

## 4. 约定与约束

- **配置不硬编码**：Java 侧 `ParserProperties` 注释明确要求“应用配置（docs/09 第三章：配置不硬编码）”，Python 侧 `config.py` 注释同样强调“采样参数禁止硬编码”。
- **必填环境变量严格校验**：`Settings.from_env` 对缺失的必填键（如 `JAVA_PARSER_URL`、`LLM_BASE_URL`、`LLM_SEED` 条件必填）抛出 `RuntimeError`，确保服务启动失败而非静默降级。
- **并发度受控**：`parser.scan-concurrency` 必须在构造时为正数，否则抛 `IllegalArgumentException`。
- **中文注释约定**：Ruff 配置忽略 RUF001/002/003，允许全角标点与中文 docstring（见 pyproject.toml 注释引用 docs/09 第二章）。
- **测试代码宽松断言**：`tests/**` 下允许 `assert`（忽略 S101），但源码保持 strict mypy。
- **容器网络绑定**：orchestrator 入口允许监听全部网卡（忽略 S104），因为 compose 编排依赖此行为。
- **编排单一事实来源**：compose 文件顶部注释标明“编排唯一事实来源（docs/09 第十一章）”，所有服务端口、卷、依赖关系集中管理。
- **产物路径统一**：所有服务共享 `/data/llm-autotest` 作为 artifacts 根目录，便于备份策略统一落地（文档中标注宿主机路径待 B 定稿）。

## 5. 未覆盖/待完善之处

- 前端目前无生产环境配置注入机制（Vite 构建产物静态化，代理仅在 dev server 生效）。
- test-executor 暂未在默认 compose profile 中启用，需显式 `--profile exec`。
- 尚未发现统一的配置验证 Schema（如 JSON Schema 校验 application.yml 或 .env 键集合），当前依赖各语言框架自身校验。