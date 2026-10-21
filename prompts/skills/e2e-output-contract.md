---
version: 2.0
spec: K-E2E-02
owner: QA
changelog:
  - 2.0 | 2026-10-20 | 与新版业务用例协议统一，取消旧字段白名单冲突
sampling:
  temperature: 0.2
---
只输出 {"cases":[...]}。每次仅处理当前业务场景。字段及动作遵守主提示词；每步必须有 intent，steps 中每个业务断言必须有 basis；准备和 precondition_checks 的就绪检查可无 basis，每条用例必须含可执行的业务结果断言。goto/expect_url 不需要 selector。不能输出代码、解释或包装对象。
