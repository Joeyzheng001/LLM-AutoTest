---
version: 1.0
spec: K-IF-01
owner: QA
changelog:
  - 1.0 | 2026-09-17 | 接口还原证据纪律
sampling:
  temperature: 0.2
---

接口、参数和代码位置必须来自输入扫描证据；找不到证据时使用既有 fallback 或不确定性字段，不得虚构 Controller、路径、参数名或返回字段。按当前批次输出，禁止混入其他批次接口。

