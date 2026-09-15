---
kind: logging_system
name: 多语言服务的轻量日志体系（Python logging + Spring Boot 默认日志）
category: logging_system
scope:
    - '**'
source_files:
    - services/orchestrator/src/orchestrator/__main__.py
    - .rules/python-code-style.md
    - services/java-parser/src/main/resources/application.yml
    - services/java-parser/pom.xml
---

## 1. 使用的系统/方案

仓库包含三个服务，日志方案按语言分别采用最简实现：

- **orchestrator（Python）**：使用 Python 标准库 `logging`。在入口 `services/orchestrator/src/orchestrator/__main__.py` 中通过 `logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")` 统一配置根 logger，并以模块级 `logger = logging.getLogger(__name__)` 获取实例。
- **java-parser（Spring Boot / Java）**：未引入额外日志框架，依赖 Spring Boot 默认的 Logback/SLF4J 输出；`pom.xml` 中 `<consoleOutput>true</consoleOutput>` 表明控制台输出开启。
- **test-executor（Python）**：当前仅含极小模块，未发现显式日志调用。
- **frontend**：前端为 Vue/Vite 应用，无服务端日志逻辑。

## 2. 关键文件

- `services/orchestrator/src/orchestrator/__main__.py` — 唯一集中配置日志级别与格式的位置。
- `.rules/python-code-style.md` — 规定 Python 侧日志的编码规范（见下文“约定与约束”）。
- `services/java-parser/src/main/resources/application.yml` — 仅声明 server/port、parser.scan-concurrency，未配置 log level 或日志格式，走 Spring Boot 默认行为。
- `services/java-parser/pom.xml` — 启用 Maven Surefire 的 `consoleOutput`，测试阶段将日志打印到控制台。

## 3. 架构与约定

- **单点初始化**：orchestrator 仅在 `__main__.py` 调用 `basicConfig`，其他业务模块通过 `getLogger(__name__)` 复用该根 logger，避免重复配置。
- **日志级别策略**：全局固定为 `INFO`，未暴露环境变量控制；错误场景通过 `RuntimeError` 等异常抛出（如 `config.py` 中缺失必填环境变量时），由上层捕获后记录。
- **结构化字段**：当前未使用 JSON 行格式或结构化字段；日志为纯文本，字段顺序为 `时间戳 模块名 级别 消息`。
- **安全口径**：`Settings.public_summary` 属性专门用于生成不含密钥的配置摘要，供启动日志使用，避免把 `LLM_BASE_URL`、`LLM_SEED` 等敏感值直接写入日志。
- **Java 侧**：未自定义日志配置，遵循 Spring Boot 默认输出（控制台 JSON 或 plain text 取决于 starter 版本）。

## 4. 约定与约束

### 已实现的约束

- orchestrator 启动时以 `INFO` 级别输出运行摘要，且摘要经 `public_summary` 脱敏后再写入日志（`__main__.py:20`）。
- 缺少必填环境变量会抛 `RuntimeError`，由进程退出码体现失败（符合需求文档中对 stderr/退出码的判定要求）。

### 规范文件中规定的约束（尚未完全落地）

`.rules/python-code-style.md` 明确规定：
- 必须使用模块级 `logging.getLogger(__name__)`；
- 应使用 **JSON 行格式** 统一配置日志输出；
- **禁止用 `print`** 记录运行信息；
- **禁止拼接敏感字段与用户文本** 到日志中。

当前 orchestrator 仅实现了“模块级 logger”和“禁止 print”的部分约定，但尚未切换到 JSON 行格式，也未在其他模块中统一应用该规则。

### 约束来源

| 约束 | 来源 | 状态 |
|---|---|---|
| Python 使用 `logging.getLogger(__name__)` | `.rules/python-code-style.md` | 部分落地（orchestrator 入口） |
| 统一 JSON 行格式 | `.rules/python-code-style.md` | 未落地（当前为 `asctime/name/level/message` 文本格式） |
| 禁止 `print` 记录运行信息 | `.rules/python-code-style.md` | 未扫描全部代码验证 |
| 禁止拼接敏感字段 | `.rules/python-code-style.md` | 通过 `Settings.public_summary` 间接遵守 |
| Java 控制台输出 | `services/java-parser/pom.xml` `<consoleOutput>true</consoleOutput>` | 已启用 |
