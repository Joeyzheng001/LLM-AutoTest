---
version: 1.0
spec: K-REQ-02
owner: QA
changelog:
  - 1.0 | 2026-09-18 | 需求条目节点末尾 JSON 契约
sampling:
  temperature: 0.2
---

## 最终输出契约

只输出 `{"items":[...]}` 这一个 JSON 对象；禁止 `body`、`data`、`result`、`content`、
解释和系统字段。每个条目只使用 `id`、`title`、`description`、可选 `priority` 和可选
`source_location`；`source_location.line` 只能是正整数，功能编号写入 `section`。
