---
version: 1.0
spec: K-REVW-SELECT-01
owner: QA
changelog:
  - 1.0 | 2026-10-23 | 按当前 claim 与证据目录页做语义召回，保留召回与证明的边界
sampling:
  temperature: 0.1
---

## 适用任务与输入契约

只用于 `code_review_evidence_select`。输入是当前批 `claims`、当前 `catalog_page.items` 和可选接口编号；目录项可能是同一证据的分页片段。

## 可复现步骤

逐个 claim 提取业务动作、条件和结果；在当前页找可能相关的入口、服务、校验、配置和返回片段。按语义和调用关系召回，不靠相同词语；只引用当前页现有 EVD ID。候选可适当放宽，是否足以证明留给后续审查。

## 输出契约与禁止事项

沿用主 Prompt 的 `claim_ids` 与 `evidence_ids` JSON。不得生成证据 ID、把目录摘要当源码行为、把本页无结果推断为全仓不存在。编号和批次边界由程序核验。

## 自检与失败策略

`claim_ids` 覆盖输入全部 claim，`evidence_ids` 均在当前页且至少与一个 claim 有语义关联。无相关片段返回空数组；输入不完整时不补造目录项。

## 验收样例

claim 写“审批豁免”，源码片段用 `exempt` 标识同一业务路径时可召回；仅另一模块含 `exempt` 但无调用关系时不能据此认定该 claim 已实现。
