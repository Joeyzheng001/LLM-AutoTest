# 09 — API 接口规格（引导版模板）

---

| 项 | 值 |
|---|---|
| 模块编号 | 【填写：如 M1-QA】 |
| 模块名称 | 【填写：如 投研问答助手】 |
| 文档版本 | v0.1 |
| 阶段 | Design（How — 契约真源） |
| Base URL | `/api/v1/agent` |

---

### 1.1 路径书写约定（避免 `POST /ask` 与全路径混读）

| 概念 | 约定 |
|------|------|
| **Base URL** | `/api/v1/agent`（不含协议与主机；部署须 TLS，见 `11`） |
| **本文中的简写** | `POST /ask` 等同于 `POST /api/v1/agent/ask`；`GET /sessions` 等同于 `GET /api/v1/agent/sessions` |
| **`13` / `14` / 测试断言** | 允许使用简写或全路径，**但必须能与上表互推**；对外部集成方公布时建议写全路径 |

---

> **本文是全部 API 端点的契约真源**。`05` 定义"用户要什么"，**09（本文）定义"后端必须返回什么"**，`13` 的测试断言以本文为准。

## 1. 端点总览

| # | 端点 | 方法 | 功能 | 成功码 |
|---|------|------|------|--------|
| 1 | `/api/v1/agent/capabilities` | GET | 能力探测 | 200 |
| 2 | `/api/v1/agent/ask` | POST | 问答提交 | 200 |
| 3 | `/api/v1/agent/sessions` | GET | 会话列表 | 200 |
| 4 | `/api/v1/agent/sessions` | POST | 新建会话 | 201 |
| 5 | `/api/v1/agent/sessions/<id>` | DELETE | 删除会话 | 200 |
| 6 | `/api/v1/agent/sessions/<id>/records` | GET | 问答记录 | 200 |

## 2. 统一响应规范

### 成功响应

```json
{ "traceId": "tr_abc123...", /* 业务字段 */ }
```

### 错误响应

```json
{ "error": { "code": "EMPTY_QUERY", "message": "请输入问题", "details": {}, "traceId": "tr_..." } }
```

### 错误码清单

| HTTP | error.code | 触发条件 | details |
|------|-----------|----------|---------|
| 400 | `EMPTY_QUERY` | query 为空/null | `{}` |
| 400 | `INVALID_QUERY` | query 超 500 字符 | `{"max_length":500}` |
| 【补充】 | 【错误码】 | 【触发条件】 | 【details】 |

## 3. ★ 示例：POST /ask — 问答提交

**请求体**：

| 字段 | 类型 | 必填 | 约束 | 说明 |
|------|------|------|------|------|
| `query` | string | **是** | 1–500 字符 | 用户提问原文 |
| `session_id` | string | **是** | UUID | 目标会话 ID |

**成功响应**（200）：

| 字段 | 类型 | 必有 | 说明 |
|------|------|------|------|
| `traceId` | string | 是 | 链路追踪 ID |
| `answer` | string | 是 | 答案文本 |
| `llm_used` | boolean | 是 | 是否使用真实 LLM |
| `model` | string\|null | 是 | 模型标识 |
| `response_time_ms` | integer | 是 | 响应耗时（毫秒） |
| `answer_source` | string | 是 | copaw / bailian / demo |

## 4. POST /sessions — 新建会话（请填写）

**请求体**：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `title` | string | 否 | "新会话" | 会话标题 |

**成功响应**（201）：

| 字段 | 类型 | 必有 | 说明 |
|------|------|------|------|
| `traceId` | string | 是 | 链路追踪 ID |
| 【请补充其余响应字段：session_id、title、created_at、query_count 等】 | | | |

## 5. GET /sessions — 会话列表（请填写）

> 无请求体，返回 sessions 数组。每个 session 至少包含 id、title、created_at、query_count。

【请按上面格式填写响应字段表和响应示例】

## 6. DELETE /sessions/<id> — 删除会话（请填写）

> 路径参数 session_id，无请求体，返回确认消息。注意级联删除关联记录。

【请填写响应字段和副作用说明】

## 7. GET /sessions/<id>/records — 问答记录（请填写）

> 路径参数 session_id，返回 records 数组。每条记录含 query、answer、timestamp 等。

【请填写响应字段表和响应示例】

## 8. 参数校验规则汇总

| 端点 | 字段 | 规则 | 失败 HTTP | error.code |
|------|------|------|-----------|-----------|
| POST /ask | `query` | 非空/非空白 | 400 | `EMPTY_QUERY` |
| POST /ask | `query` | ≤ 500 字符 | 400 | `INVALID_QUERY` |
| POST /ask | `session_id` | 非空 | 400 | `INVALID_QUERY` |
| 【请补充其他端点的校验规则】 | | | | |

---

| 版本 | 日期 | 说明 |
|------|------|------|
| v0.1 | 【日期】 | 首版填写 |
