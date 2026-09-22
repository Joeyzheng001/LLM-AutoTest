---
version: 1.0
spec: K-REV-02
owner: QA
changelog:
  - 1.0 | 2026-09-18 | 需求评审节点末尾 JSON 契约
sampling:
  temperature: 0.2
---

## 最终输出契约

只输出一个 JSON 对象，首字符 `{`、尾字符 `}`，不得使用 `body`、`data`、`result`、
`content` 或解释包装。顶层必须且只能有：`score`、`quality_grade`、`summary`、
`dimension_scores`、`quality_assessment`、`issues`、`readable_issue_points`、
`completeness_issues`、`consistency_issues`、`untestable_items`、`risk_flags`、
`testable_features`、`mindmap`。不要输出根级 `reason` 或 `weight`；五个评分维度只在
`dimension_scores` 中出现。
