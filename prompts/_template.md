<!-- Prompt 模板：所有 prompts/ 下的 Prompt 文件复制本模板起笔。加载器（orchestrator/prompts_loader.py）解析本头部元信息块。 -->

---
version: 1.0
spec: F-XXX-XX            # 对应规格编号（如 F-REQ-01）
owner: 甲                  # 主笔人（甲/乙/丙）
changelog:
  - 1.0 | 2026-09-15 | 初版
sampling:                  # 建议采样参数（实际值以配置文件为准，加载器不得硬编码）
  temperature: 0.2
  top_p: 0.8
  seed: null
---

## 输出格式要求（必须）

LLM 输出须符合 `schemas/<对应schema>.json`，校验失败按节点失败处理，禁止截取可用部分。

## System / User Prompt 正文

（在此书写 Prompt 正文）
