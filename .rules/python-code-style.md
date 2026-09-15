---
trigger: glob
glob:
  - "services/**/*.py"
  - "services/**/pyproject.toml"
  - "**/uv.lock"
---

# Python 代码统一规范（orchestrator / test-executor）

## 项目基线

- 本项目 Python 基线为 **3.11 及以上**，包管理使用 **uv**（锁定 `uv.lock`）；优先使用标准库和现有依赖，不得无需求升级依赖或增加新的运行时包。
- Monorepo 结构：`services/orchestrator/`（9 节点编排、断点续跑、REST API）、`services/test-executor/`（测试脚本加载与无人值守执行）；跨节点数据模型定义于各服务 `models/`，与仓库根 `schemas/` 的 JSON Schema 一一对应（单一事实来源为 Schema）。
- 修改前阅读同目录实现、入口和测试，保持现有架构。节点实现类统一后缀 `Node`（如 `ReqParseNode`）。
- 只做需求范围内的最小改动。公共函数、API 字段、产物 Schema 或持久化格式变化时，必须同步兼容逻辑和测试；产物 Schema 变更必须升 `schema_version`，禁止原地改语义。
- 模块头部注释注明对应规格编号（如 `# 对应 F-ORCH-01`），docstring 与注释使用中文。
- 禁止提交或记录密钥、密码、令牌、证书及 GLM API Key 等敏感信息。

## 风格与命名

- 遵循 PEP 8：模块、函数、变量使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。
- 使用 ruff（lint + format，`line-length = 100`）与 mypy；导入分组为「标准库、第三方、项目内部」，删除无用导入，禁止 `import *`。
- 新模块加入 `from __future__ import annotations`；使用 Python 3.11 类型语法，如 `dict[str, str]`、`str | None`。
- 所有对外函数、类方法和数据模型（pydantic）**强制**准确类型标注；避免无边界扩散 `Any`。类型忽略必须写明原因并缩小到具体行。
- Docstring 说明职责、参数契约、返回值和重要异常，不复述代码。注释解释「为什么」，不解释显然的「做什么」。
- 函数保持单一职责；参数过多时使用 pydantic BaseModel 或有明确类型的对象，不传递含义不明的字典。

## 数据与控制流

- 禁止使用可变默认参数；使用 `None` 后在函数内创建集合。
- 边界输入先验证、规范化，再进入业务逻辑。LLM 输出属于边界输入：必须通过 `schemas/` 对应 JSON Schema 校验成功后才可进入下游产物，校验失败按节点失败处理（走断点续跑口径），禁止降级为「截取可用部分」。
- 路径使用 `pathlib.Path`，文件读写明确指定 UTF-8；结构化产物按任务 ID 分目录写入，路径统一经 orchestrator 的 `artifacts` 模块管理，禁止各节点自行拼路径。
- 对文件和网络资源使用上下文管理器。
- 优先早返回降低嵌套；避免超长函数、重复分支和模块级可变状态。
- 枚举与标识符使用明确类型；时间戳统一使用带时区的 `datetime`（UTC），禁止依赖容器默认时区——产物时间戳是 R-DEF-03 可审计口径的依据。

## 异步与外部调用

- orchestrator 节点调度使用 asyncio；`async` 路径不得执行阻塞式网络、文件或数据库调用，必要时明确移交线程池，禁止线程池与 asyncio 无封装混用。
- 所有外部请求（GLM-5.3 API、java-parser 服务、被测服务）设置连接与读取超时，处理非成功状态、无效响应、取消和重试边界；重试必须有限并避免重复副作用。
- 每次 LLM 调用记录输入摘要、输出、耗时、token 数于节点执行日志（支撑 D6 半自动采集口径）；采样参数（temperature / top_p / seed）从配置文件读取，禁止散落硬编码。

## 异常与日志

- 捕获能够处理的最具体异常，禁止裸 `except` 和无说明的 `except Exception: pass`；节点失败必须显式落盘（状态 + 原因 + 时间戳），作为断点续跑（F-ORCH-02）依据。
- 转换异常时使用 `raise ... from exc` 保留原因；只有边界层负责把内部异常映射为统一错误结构 `{code, message, node_id?, details}`。
- 使用模块级 `logging.getLogger(__name__)`，JSON 行格式统一配置；禁止用 `print` 记录运行信息，禁止拼接敏感字段与用户文本。

## 测试与交付

- 使用 pytest 组织测试；测试必须确定、隔离，不依赖执行顺序、真实时间、随机结果、真实外部服务或真实 GLM 调用（LLM 客户端一律 Mock）。
- 缺陷修复先补回归测试；新增功能覆盖成功路径、边界输入、失败路径及关键副作用。流水线核心逻辑（解析、汇总、断点续跑、度量计算）覆盖率 ≥70%。
- 测试文件命名为 `test_*.py`，与被测模块同构放置，测试名称直接描述行为；复用 fixture，避免复制大段搭建代码。
- 完成前确认：代码可导入、相关测试通过、无调试代码、无吞异常、无未关闭资源，文档与配置和行为一致。
