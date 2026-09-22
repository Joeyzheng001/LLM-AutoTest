---
version: 1.0
spec: K-REVW-02
owner: QA
changelog:
  - 1.0 | 2026-09-18 | Code Review 节点末尾 JSON 契约
sampling:
  temperature: 0.2
---

## 最终输出契约

只输出 `{"mappings":[...]}` 这一个 JSON 对象；禁止 `body`、`data`、`result`、`content`、
`risks`、`missing_features`、解释或其它顶层键。每条映射只使用 `requirement_id`、可选
`interface_id`、`status`、`confidence`、`evidence`，且 ID 必须来自当前批输入。
