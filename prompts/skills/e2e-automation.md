---
version: 2.0
spec: K-E2E-01
owner: QA
changelog:
  - 1.0 | 2026-09-17 | 可执行浏览器用例约束
sampling:
  temperature: 0.2
---

## 输入契约

必须有 surface.routes 的真实 DOM 探测结果、目标根地址和需求测试点。没有探测到可操作元素时输出空数组。

## 工作步骤

1. goto 只使用已探测 path；selector 逐字使用该页面的白名单。
2. 一条用例验证一个用户目标，优先主流程和关键校验；最多生成 max_cases 条。
3. 仅使用 goto/click/fill/check/select/expect_text/expect_url；登录值使用 AUTH 占位符。

## 自检与失败策略

不得猜测 CSS、XPath、DOM 层级、路由、控件或输出 JavaScript。expect_url 不需要 selector。DOM 探测/证书/页面不可达时记录环境问题并跳过；连续两次同指纹通过才可沉淀为 E2E 回归资产。
