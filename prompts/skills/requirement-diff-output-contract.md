---
version: 1.0
spec: K-DIFF-02
owner: QA
changelog:
  - 1.0 | 2026-09-18 | 需求差异节点末尾 JSON 契约
sampling:
  temperature: 0.2
---

## 最终输出契约

只输出一个 JSON 对象，首字符 `{`、尾字符 `}`。顶层只能是 `summary`（字符串）和
`changes`（数组）；不得输出 `body`、`data`、`result`、`content`、解释或额外键。
每个 change 只使用 `change_type`、`title`、`target_description`、`baseline_description`、
`change_summary`、`source_location`。无差异时输出 `{"summary":"...","changes":[]}`。
