---
version: 1.0
spec: K-IF-02
owner: QA
changelog:
  - 1.0 | 2026-09-18 | 接口还原节点末尾 JSON 契约
sampling:
  temperature: 0.2
---

## 最终输出契约

只输出 `{"interfaces":[...]}` 这一个 JSON 对象；禁止 `body`、`data`、`result`、
`content`、解释和系统回填字段。每个条目只输出 `candidate_id`、`signature`、`params`、
`returns`，并原样回填当前批次候选的 `candidate_id`；未知类型写 `unknown`，不新造接口。
