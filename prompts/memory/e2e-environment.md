---
version: 1.0
spec: M-E2E-01
owner: QA
changelog:
  - 1.0 | 2026-09-17 | E2E 环境经验
sampling:
  temperature: 0.2
---

已验证经验：测试环境可能使用自签名 HTTPS 证书，执行器是否忽略证书错误由任务配置决定。此经验只约束用例设计不要把证书异常当作页面功能断言；浏览器环境策略由程序执行。

