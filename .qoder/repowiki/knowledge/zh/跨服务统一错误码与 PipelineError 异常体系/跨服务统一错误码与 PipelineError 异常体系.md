---
kind: error_handling
name: 跨服务统一错误码与 PipelineError 异常体系
category: error_handling
scope:
    - '**'
source_files:
    - services/orchestrator/src/orchestrator/errors.py
    - services/orchestrator/src/orchestrator/engine.py
    - services/orchestrator/src/orchestrator/artifacts.py
    - services/orchestrator/src/orchestrator/doc_loader.py
    - services/orchestrator/src/orchestrator/javaparser_client.py
    - services/test-executor/src/executor/result.py
    - services/java-parser/src/main/java/com/llmautotest/parser/HealthController.java
    - services/java-parser/src/main/java/com/llmautotest/parser/ParserProperties.java
---

## 1. 采用的系统/方法

- **Python 侧（orchestrator、test-executor）**：自定义 `PipelineError` 异常 + 集中式错误码常量，作为流水线各节点对外暴露的统一错误结构 `{code, message, node_id?, details}`。该结构由 `errors.py` 的 `to_dict()` 固化输出契约，并通过 `NodeState.error` 字段持久化到 `state.json`。
- **Java 侧（java-parser）**：Spring Boot 微服务，当前仅暴露 `/health` 健康端点；配置校验通过抛出 `IllegalArgumentException` 表达非法参数，未定义全局异常处理器或业务异常类。
- **前端（frontend）**：当前为占位视图，无 API 调用与错误处理逻辑。

## 2. 关键文件与包

| 文件 | 作用 |
|---|---|
| `services/orchestrator/src/orchestrator/errors.py` | 定义 `PipelineError` dataclass 及 X-LLM-* / X-ORCH-* 错误码常量 |
| `services/orchestrator/src/orchestrator/engine.py` | 编排引擎核心：所有状态变更（init/open/reconcile/marked_*）均通过 `raise PipelineError(...)` 表达失败 |
| `services/orchestrator/src/orchestrator/artifacts.py` | 产物校验失败时抛 `PipelineError(X_ORCH_01)` |
| `services/orchestrator/src/orchestrator/doc_loader.py` | Prompt/文档加载失败抛 `PipelineError` |
| `services/orchestrator/src/orchestrator/javaparser_client.py` | 下游 java-parser 通信失败抛 `PipelineError(X_ORCH_03)` |
| `services/test-executor/src/executor/result.py` | 执行结果二态模型：`FailureKind.ASSERTION` vs `ENVIRONMENT`，用于区分断言失败与环境失败 |
| `services/java-parser/src/main/java/com/llmautotest/parser/HealthController.java` | 健康检查端点（docker-compose 依赖探测） |
| `services/java-parser/src/main/java/com/llmautotest/parser/ParserProperties.java` | 配置项校验，非法值抛 `IllegalArgumentException` |

## 3. 架构与约定

### 3.1 统一错误结构
`PipelineError` 是 orchestrator 内唯一的业务异常类型，其 `to_dict()` 产出固定四字段结构：
- `code`：必须来自 `errors.py` 中预定义的 X-LLM-* / X-ORCH-* 常量表，禁止新增“表外错误码”（注释引用 docs/12 §四）。
- `message`：人类可读描述。
- `node_id`：可选，标记触发错误的节点。
- `details`：可选，结构化附加信息（如 `blocked_by`）。

日志输出格式固定为 `[code] message（node）`，便于在 CI 日志中快速定位。

### 3.2 错误码分类
| 前缀 | 含义 | 示例 |
|---|---|---|
| `X-LLM-01` | LLM 调用超时/网络失败（可重试） |
| `X-LLM-02` | LLM 输出未过 Schema 校验（不重试，人工介入） |
| `X-LLM-03` | Prompt 文件缺失/元信息不合规（配置错误，人工介入） |
| `X-ORCH-01` | 产物校验失败 / state.json 损坏 / job 已存在等编排错误 |
| `X-ORCH-02` | 跳序请求：前序节点未完成 |
| `X-ORCH-03` | java-parser 服务不可达/通信失败 |

### 3.3 错误传播路径
- 底层 I/O 异常（`json.JSONDecodeError`、`OSError`、`KeyError`、`TypeError`）被捕获后包装为 `PipelineError` 再向上抛出，避免原始异常泄漏。
- `Engine.mark_failed` 将 `PipelineError.to_dict()` 写入 `NodeState.error`，实现错误状态的持久化与后续对账。
- `Engine.reconcile` 在读取 `state.json` 时发现产物失效会回退节点至 `PENDING` 并记录 `X-ORCH-01` 错误。

### 3.4 测试执行结果的错误建模
`test-executor` 使用 `CaseResult.failure_kind: FailureKind` 区分两类失败：
- `ASSERTION`：断言失败，计入通过率分母。
- `ENVIRONMENT`：环境失败，隔离至“待环境确认”，不计入通过率分母。
`ExecutionSummary` 提供程序化汇总属性 `failed_assertion` / `failed_environment`，确保指标计算不经过 LLM。

### 3.5 Java 侧现状
java-parser 服务目前仅实现健康检查端点，未定义业务异常类或全局异常处理器；配置校验直接抛出 `IllegalArgumentException`。这意味着 orchestrator 与 java-parser 之间的错误语义尚未通过统一的 HTTP 错误码协议对齐（`X-ORCH-03` 仅在 Python 侧定义）。

## 4. 约定与约束

- **错误码白名单**：`errors.py` 中的常量是唯一合法来源，新增错误码需同步更新 docs/12 §四 总表（代码注释明确引用该规范）。
- **宁停不猜**：`JobState.from_dict` 在反序列化失败时直接抛 `PipelineError(X_ORCH_01)`，拒绝猜测损坏的 state.json。
- **原子落盘**：`_save` 使用 tmp + replace 模式写 `state.json`，避免并发写入导致的状态不一致。
- **失败即回退**：`mark_failed` 将节点状态重置为 `PENDING` 并记录错误，而非停留在 `RUNNING`。
- **降级可追踪**：`mark_degraded` 同时设置 `MANUAL_INTERVENTION` 状态并追加到 `degraded` 清单，保证报告能分列统计。
- **结果一致性**：`CaseResult.__post_init__` 强制“通过的用例不得携带失败类型”，在构造阶段拦截自相矛盾的结果。
- **健康检查**：orchestrator 与 java-parser 均暴露 `/health` 端点，供 docker-compose 编排依赖探测。

## 5. 差距与待完善

- orchestrator 的 FastAPI 应用（`api.py`）未注册全局异常处理器，`PipelineError` 如何映射为 HTTP 响应尚不明确。
- java-parser 缺乏业务异常与全局异常处理，与 orchestrator 的错误协议尚未打通。
- 前端尚无错误展示逻辑（当前仅为占位页面）。