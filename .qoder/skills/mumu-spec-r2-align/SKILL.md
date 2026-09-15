---
name: mumu-spec-r2-align
description: >-
  MumuSpec R2 (align): writes and cross-checks 05 user stories and 09 API spec
  (dual contract alignment). Use when defining REQ/US/AC, REST paths, request/
  response schemas, or calibrating acceptance criteria against API fields.
  Requires 03/04 approved or stable draft.
---

# R2 — 对齐（05 ↔ 09）

## 职责

- **`05`**：`REQ-*`、`US-*`、`AC-*`；每条故事含功能/设计/验证对齐列。  
- **`09`**：path、方法、参数、**示例请求/响应/错误体**；字段名在此 **冻结**。  
- **互校准**：每条 P0 AC 能在 `09` 找到可观测断言（状态码、字段、错误码）。

## 输入

- 已稳定的 `03`、`04`（至少 Draft 完整）。  
- 模块 ID 前缀（与现有样例一致，如 `REQ-M1QA-`、`TC-M01-` 将在 R4 使用）。

## 步骤

1. 打开 `05-用户故事与验收标准.md`、`09-API接口规格.md` 作结构模板。  
2. 从 `04` 拆 P0 用户故事，分配 `REQ-*` 编号（连续、不跳号）。  
3. 为 P0 故事在 `09` 写对应端点；**禁止**在 `05`/`06` 另起与 `09` 不一致的字段别名。  
4. 逐条 AC 检查：可测试、无「友好」「尽量」类模糊词。  
5. 更新两篇文末变更记录；若 breaking API 变更，升 `09` 小版本并注明。

## 不混层

- `05` 不写 SQL、不写完整 JSON Schema 重复（引用 `09` 章节）。  
- `09` 不写用户价值叙事（留在 `05`/`04`）。

## 完成后

提示 **@mumu-spec-r3-design**（08/10/06/07/11）。若仅改一个 endpoint，改完后建议 **@mumu-spec-sync**。
