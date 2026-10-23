---
version: 1.0
spec: K-E2E-GROUND-01
owner: QA
changelog:
  - 1.0 | 2026-10-23 | 从当前页面观察提取场景事实，隔离业务预期
sampling:
  temperature: 0.1
---

## 适用任务与输入契约

只用于 `e2e_grounding`。输入是一个已规划场景和一页真实浏览器观察；每页均为完整观察的一部分。

## 可复现步骤

逐条读取本页 entries，筛选与场景有关的入口、字段、反馈区和页面状态。保留真实 selector、source_page、frame 的 URL 与名称；无法定位时记录观察缺口。

## 输出契约与禁止事项

沿用主 Prompt 的 `facts`、`gaps` JSON。事实只说明当前页面可见内容，不生成测试步骤或改写需求预期；不得猜 CSS、XPath、未观察的 frame 或成功消息。

## 自检与失败策略

每个 selector 都能在本页逐字找到，frame 归属正确。没有对应事实时返回空列表及缺口；本页未出现不等于整站不存在。

## 验收样例

页面观察含 iframe 中的“提交审批”按钮及其语义定位器：记录按钮、selector 和 frame；若未展示提交后提示，不生成“提交成功”事实。
