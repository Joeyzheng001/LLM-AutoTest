---
kind: external_dependency
name: 智谱 GLM-5.3 大模型服务
slug: glm-5-3
category: external_dependency
category_hints:
    - vendor_identity
    - client_constraint
scope:
    - '**'
source_files:
    - .rules/llm-integrity.md
    - .rules/python-code-style.md
---

本项目唯一允许调用的 LLM 后端，冻结基线为 GLM-5.3，禁止引入其他模型。
- 集成点：orchestrator 与 test-executor 的 Python 节点通过外部调用接入；每次调用需记录输入摘要、输出、耗时、token 数于节点执行日志（支撑 D6 半自动采集口径）。
- 约束：temperature / top_p / seed 等采样参数必须从配置文件读取，禁止散落硬编码；LLM 输出必须先经 `schemas/` 对应 JSON Schema 校验，失败按节点失败处理并走断点续跑，禁止降级为截取可用部分。
- 变更控制：任何模型切换须先走变更控制流程，不得直接替换。