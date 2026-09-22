---
version: 1.0
spec: K-E2E-02
owner: QA
changelog:
  - 1.0 | 2026-09-18 | E2E 用例节点末尾 JSON 契约
sampling:
  temperature: 0.2
---

## 最终输出契约

只输出 `{"cases":[...]}` 这一个 JSON 对象；禁止 `body`、`data`、`result`、`content`、
解释或其它顶层键。每条只使用 `requirement_id`、`title`、`preconditions`、`steps`、
`expected`；步骤只能使用已探测的路径与 selector，`goto`/`expect_url` 不带 selector。
