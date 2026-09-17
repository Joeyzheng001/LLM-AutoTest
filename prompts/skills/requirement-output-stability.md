---
version: 2.0
spec: K-REQ-01
owner: QA
changelog:
  - 1.0 | 2026-09-17 | 需求评审结构稳定性规则
sampling:
  temperature: 0.2
---

## 输入契约

输入是分段需求文本与主 Prompt 定义的 JSON Schema。来源、章节和原文证据优先于概括性判断。

## 工作步骤

1. 逐段提取可验证行为、约束、角色、数据和异常条件。
2. 将缺失、歧义、冲突和不可测内容写入 issues，附带影响与澄清理由。
3. 评分只基于已提取证据；测试特性必须是扁平字符串，不输出脑图对象。
4. 长文档按段独立分析，合并时按 requirement_id 去重。

## 输出自检

顶层字段只能来自主 Prompt 白名单；quality_assessment 必须在顶层；dimension_scores 只能含五个固定维度，不能附加 _error、note 或 analysis。issues 必须有 reason。没有证据时标待澄清，不得伪造评分或改变 JSON 层级。

## 失败策略

字段别名、误嵌套由客户端做受控归一化；未知字段和无法修复的结构必须失败并留痕，不能静默丢失。
