---
version: 1.1
spec: K-GEN-02
owner: QA
changelog:
  - 1.0 | 2026-09-18 | 用例生成节点的末尾 JSON 输出契约
  - 1.1 | 2026-09-21 | 明确批次覆盖与单对象兼容边界
sampling:
  temperature: 0.2
---

## 用例 JSON 最终契约

本次回答的第一个字符必须是 `{`，最后一个字符必须是 `}`。只输出一个对象，且对象
顶层只能有一个键：`cases`。

正确形态：

```json
{"cases":[{"interface_id":"输入中存在的 IF-...","category":"normal","title":"...","params":{},"expected":"status=200"}]}
```

禁止输出 `body`、`data`、`response`、`content`、`result`、`test_cases`、`testcases`、
`analysis`、`message` 或任何展示/解释包装。不要把 JSON 再编码为字符串；`cases` 必须是
真正的数组。生成前在内部检查：每个 `interface_id` 都来自当前批次，`category` 只能为
`normal`、`boundary`、`exception`，`params` 是对象。

即使当前批次只有一条用例，也必须输出 `{"cases":[...]}`；服务端只对字段完整的单对象做兼容提升，
不会替模型补标题、参数或预期。必须覆盖输入批次声明的每个接口及 `normal`、`boundary`、
`exception` 三类场景；无法完成某条用例时，宁可触发修复/人工介入，也不能用其它接口复制或改变顶层结构。
