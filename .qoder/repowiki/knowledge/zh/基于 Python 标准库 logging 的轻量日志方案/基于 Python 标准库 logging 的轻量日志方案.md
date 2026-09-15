---
kind: logging_system
name: 基于 Python 标准库 logging 的轻量日志方案
category: logging_system
scope:
    - '**'
source_files:
    - services/orchestrator/src/orchestrator/__main__.py
    - services/orchestrator/src/orchestrator/config.py
    - services/java-parser/src/main/resources/application.yml
---

## 1. 使用的系统/框架

仓库中仅 **orchestrator** 服务使用了 Python 标准库 `logging`，其余服务（java-parser、test-executor、frontend）未发现显式日志代码。
- orchestrator 在入口 `__main__.py` 中通过 `logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")` 初始化根 logger，并以 `logging.getLogger(__name__)` 获取模块级 logger。
- java-parser 是 Spring Boot 应用，但 `application.yml` 中未配置 logback/slf4j 相关项；当前未见任何 Java 源码使用 SLF4J / Logback / Commons Logging 等日志 API。
- frontend (Vue + Vite) 与 test-executor 均未发现结构化或框架化日志输出。

因此，本仓库的“日志系统”目前是一个**极轻量的默认实现**：仅在 orchestrator 启动时打印一行 INFO 级别消息，无统一 logger 封装、无结构化字段、无文件/远程 sink、无日志级别动态调整机制。

## 2. 关键文件

| 文件 | 作用 |
|---|---|
| `services/orchestrator/src/orchestrator/__main__.py` | 唯一调用 `logging` 的位置：设置 root logger 格式、打印启动摘要 |
| `services/orchestrator/src/orchestrator/config.py` | 提供 `Settings.public_summary` 属性，作为“不含密钥的配置摘要”，被启动日志复用，避免把 `llm_api_key` 等敏感字段写入日志 |
| `services/java-parser/src/main/resources/application.yml` | 仅声明 server 端口与 parser 并发度，未包含任何日志配置 |

## 3. 架构与约定

- **单点初始化**：所有日志由 `__main__.py` 中的 `basicConfig` 一次性配置，后续模块直接 `getLogger(__name__)` 取子 logger。没有独立的 `logger.py`/`log_config.py` 等集中配置模块。
- **日志级别策略**：固定为 `INFO`，未暴露环境变量控制；生产/调试场景无法通过配置切换。
- **日志格式**：采用 Python 标准占位符 `%(asctime)s %(name)s %(levelname)s %(message)s`，属于人类可读的纯文本行，非 JSON 结构化。
- **安全约定**：`config.Settings.public_summary` 明确标注“不含密钥的配置摘要（日志安全口径）”，并在启动日志中使用该摘要，体现“禁止将密钥类配置写入日志”的约束。
- **跨服务一致性**：其他服务尚未引入日志，不存在统一的日志 SDK 或共享包；若未来扩展，需自行决定是否与 orchestrator 保持一致。

## 4. 约定与约束

- **已观察到的行为（描述性）**
  - orchestrator 启动时会输出一条包含 `port`、`java_parser_url`、`artifacts_root`、`model`、`temperature`、`top_p`、`seed` 等信息的 INFO 日志。
  - `llm_api_key` 虽存在于 `Settings`，但通过 `public_summary` 刻意排除在日志之外。
  - 日志输出到标准错误/标准输出（`basicConfig` 默认），未配置文件、syslog、ELK 等 sink。

- **可推断的约束（来自代码注释/命名）**
  - `config.py` 注释强调“采样参数禁止硬编码”，间接要求运行期配置（包括可能影响日志输出的配置）应通过环境变量注入。
  - 当前仓库未定义任何强制性的日志规范文档；`rules/` 下存在 `python-code-style.md`、`java-code-style.md`，但未在工具输出中看到其中对日志的专门约束（如需确认，可进一步读取这些规则文件）。

## 5. 结论

该仓库目前**没有成体系的日志系统**：仅有 orchestrator 使用 Python 标准库 `logging` 做最基础的 INFO 级别控制台输出，且遵循了“不在日志中泄露密钥”的安全约定。对于生产环境而言，缺少结构化日志、分级控制、持久化与聚合能力，若需要完善，建议在各服务中统一引入结构化日志框架（如 structlog、loguru 或各语言生态的标准方案），并通过环境变量控制级别与输出目标。