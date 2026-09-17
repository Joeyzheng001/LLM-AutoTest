---
version: 1.0
spec: K-REQ-02
owner: QA
changelog:
  - 1.0 | 2026-09-17 | 需求条目提取稳定性规则
sampling:
  temperature: 0.2
---

只输出主 Prompt 约定的 items 数组对象。每个条目必须只引用当前批次文本事实；不可确认时保留待澄清描述，不要把数组包进 data 或 result，也不要附带解释、统计字段或模型诊断。

