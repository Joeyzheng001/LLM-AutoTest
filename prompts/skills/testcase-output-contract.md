---
version: 1.8
spec: K-GEN-02
owner: QA
changelog:
  - 1.7 | 2026-10-19 | 支持源码 oracle 的 null_or_missing 响应断言
  - 1.6 | 2026-10-15 | 用源码已确认的场景决定数量，未确认分支留在 Code Review 缺口中
  - 1.5 | 2026-10-15 | 固化源码 oracle 与响应断言操作符契约；明确 empty 不受支持且缺失/null/空值不可混用
  - 1.4 | 2026-10-08 | 解除整批 40 条封顶：REQ 测试点按维度充分展开，用例数等于测试点数视为漏拆；本批不封顶，超长字符串仍用 __LONG_<n>__ 记号防截断
  - 1.3 | 2026-09-24 | 展开加收敛点（每 REQ 点 1~2 条、整批封顶 40 条）；引入 __LONG_<n>__ 超长字符串记号
  - 1.2 | 2026-09-24 | 明确每个 REQ 测试点须有对应用例并回填 testpoint_id；用例数不得停在接口数×3
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
{"cases":[{"interface_id":"输入中存在的 IF-...","category":"normal","title":"...","testpoint_id":"TP-001","params":{},"expected":"status=200"}]}
```

禁止输出 `body`、`data`、`response`、`content`、`result`、`test_cases`、`testcases`、
`analysis`、`message` 或任何展示/解释包装。不要把 JSON 再编码为字符串；`cases` 必须是
真正的数组。生成前在内部检查：每个 `interface_id` 都来自当前批次，`category` 只能为
`normal`、`boundary`、`exception`，`params` 是对象，`testpoint_id` 必须来自本批 `testpoints`。

即使当前批次只有一条用例，也必须输出 `{"cases":[...]}`；服务端只对字段完整的单对象做兼容提升，
不会替模型补标题、参数或预期。必须逐项覆盖输入批次 `test_oracles` 的每个 oracle_id，包括待准备执行条件的设计；
没有源码预期的类别保留评审缺口，不得为凑三类编造用例或改变顶层结构。

**数量要求**：逐项覆盖本批源码 oracle，不按测试点数或拆分提示条数要求补产。相同请求和预期应合并；不同本接口行为分别设计。testpoint_id 仅在对应 Claim 被 oracle.covered_claim_ids 确认且确有业务响应断言验证时填写，否则省略。

**超长字符串记号**：字符串的「超长」边界**不要写字面量**，写紧凑记号 `__LONG_<n>__`
（`<n>` 为目标字符数，如 `__LONG_1025__`），程序会展开成真实长度的字符串。
逐字符手写 1025 个字符会耗尽生成长度，是本节点整批输出作废的主要原因。

**响应断言**：有源码 oracle 时，`assertions` 必须复制 oracle 原值；没有 oracle 时不输出该用例。
`op` 只能是 `equals`、`not_equals`、`exists`、
`not_exists`、`null_or_missing`、`non_empty`、`contains`、`greater_than`、`less_than`、`not_blank`、`equals_request`；`empty` 不受支持。
字段不存在用 `not_exists`，精确空字符串/空数组/空对象只有在源码证据证明该值时才分别用
`equals` 和 `""`、`[]`、`{}`。不能把缺失、null 与空值互相替代。

## 条件化测试设计

state_conditions 描述下游返回或环境状态，独立于 HTTP preconditions。未接入控制能力不妨碍生成用例；
程序继承这些条件并在执行时检查。不得把外部状态写成请求字段，不得用运行观测替换源码预期。
