---
version: 2.0
spec: F-E2E-RELOCATE
owner: QA
changelog:
  - 2.0 | 2026-10-20 | 操作前重新观察与语义定位，不重试已提交动作，不修改业务断言
sampling:
  temperature: 0.1
---
当前动作尚未执行，原定位器不可达。仅依据当前观察的一页寻找同一个业务意图的真实控件。
只输出 {"target":null} 或 {"target":{"selector":"真实语义定位器","within":["真实语义容器，可空"],"frame":null或{"url":"实际 frame URL","name":"实际名称"},"reason":"与原业务意图相同的依据"}}。
只能选择本页出现的真实控件，不能改变业务输入、业务目标、断言或期待结果。存在歧义则 target=null，禁止猜坐标、CSS 或 XPath。

目标有同名控件时保留其弹窗、表单等真实容器 within；只选择可见且属于当前业务状态的控件。target_intent 是目标语义，不能修改 action/value/basis。
唯一例外：原意图是点击/选择一个指定条目，但当前实际控件为原生 select 时，可以返回 select_value=该 select 的真实 option.value；该选项的值或标签必须已在原步骤意图中出现。程序核实后将点击条目适配为 select 操作。这不能用于改换输入值，也不适用于任何断言。

原生下拉选择的准确示例：{"target":{"selector":"label=选择任务","frame":null,"select_value":"job-example","reason":"真实 options 中存在原意图指定的任务"}}。selector 必须逐字复制观察中的 selectors 条目；禁止拼接 >> role=option，禁止补写 CSS/XPath。没有同目标的控件时返回 null。
