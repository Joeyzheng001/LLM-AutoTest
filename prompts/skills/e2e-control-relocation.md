---
version: 1.0
spec: K-E2E-RELOCATE-01
owner: QA
changelog:
  - 1.0 | 2026-10-23 | 操作前按新观察定位同一业务目标，禁止改变待执行动作
sampling:
  temperature: 0.1
---

## 适用任务与输入契约

只用于 `e2e_relocate`。输入是尚未执行的动作、原业务意图与当前观察的一页；只定位当前可见的同一目标。

## 可复现步骤

先确认目标语义、所属弹窗/表单、页面和 frame，再从当前观察挑唯一匹配的真实控件。多个同名控件时用真实容器 `within` 区分；原生 select 适配仅在选项与原意图一致时使用。

## 输出契约与禁止事项

沿用主 Prompt 的 `target` JSON 或 null。不得修改 action、value、业务断言或预期；不猜坐标、CSS、XPath，不重试已提交动作。

## 自检与失败策略

selector 和可选 select_value 必须逐字出现于本页观察，frame 与容器一致。目标缺失或歧义返回 null，由执行器保留定位缺口。

## 验收样例

原意图是弹窗里的“保存”，当前页有弹窗和主页面两个“保存”：只有观察能证明弹窗容器时才返回其 selector 与 within；否则返回 null。
