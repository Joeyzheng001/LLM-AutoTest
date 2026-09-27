<!-- testpoint_derive 节点的末尾输出契约：唯一顶层键 hints（加载器要求置于上下文末尾）。 -->

---
version: 1.0
spec: K-GEN-03
owner: QA
changelog:
  - 1.0 | 2026-09-24 | 测试点拆分维度补全节点的末尾 JSON 契约
sampling:
  temperature: 0.2
---

## 拆分维度 JSON 最终契约

本次回答的第一个字符必须是 `{`，最后一个字符必须是 `}`；顶层只能有一个键 `hints`。

正确形态：

```json
{"hints":[{"testpoint_id":"输入中存在的 TP-###","case_split_hints":["具体条件一","具体条件二"]}]}
```

禁止输出 `body`、`data`、`result`、`content`、`items`、`testpoints`、`analysis` 或任何解释包装。`hints` 必须是真正的数组，`case_split_hints` 必须是字符串数组。

本批每个输入 `testpoint_id` 必须出现且仅出现一次；不得生成输入之外的编号，也不得改写成用例（禁止出现 `category`、`params`、`expected`、`cases` 等用例字段）。

每条 hint 必须带具体条件或取值。若某条测试点无法确定拆分方向，为该条输出该测试点场景中最直接的边界或异常条件，而不是通用词汇或空数组。
