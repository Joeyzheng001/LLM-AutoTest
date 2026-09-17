---
version: 1.0
spec: M-LLM-01
owner: QA
changelog:
  - 1.0 | 2026-09-17 | 经验证的模型结构漂移经验
sampling:
  temperature: 0.2
---

已验证经验：结构化输出常见失败是固定字段错层、中文别名、额外诊断字段和展示型对象替代字符串数组。生成时优先服从当前主 Prompt 的 JSON 契约；不确定内容填入已定义说明字段，绝不新建键或容器。

