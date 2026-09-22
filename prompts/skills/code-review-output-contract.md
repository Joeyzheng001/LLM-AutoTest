---
version: 2.1
spec: K-REVW-02
owner: QA
changelog:
  - 2.1 | 2026-09-22 | 状态与已满足/未满足/待核查说明一致
  - 2.0 | 2026-09-22 | 声明级 assessments 与 evidence_id 引用契约
  - 1.0 | 2026-09-18 | Code Review 节点末尾 JSON 契约
sampling:
  temperature: 0.2
---

## 最终输出契约

只输出 `{"assessments":[...]}`。每个当前批 `claim_id` 恰好出现一次。对象只包含：
`claim_id`、`status`、`confidence`、`interface_ids`、`evidence_refs`、`met_conditions`、
`unmet_conditions`、`verification_needed`。数组字段必须为字符串数组；无内容时输出空数组。

`implemented` 必须至少列出一个已满足条件；`partial` 必须同时列出已满足和未满足条件；`missing`
必须列出直接证据支持的未满足条件；`undetermined` 必须说明具体待核查项。

`status` 只能为 `implemented`、`partial`、`missing`、`undetermined`。只能引用本批输入中
真实存在的 ID；接口和证据可以多选。不要输出父级 mappings、风险清单、change scope、证据目录、
代码位置副本或解释文本——这些由系统汇总和派生。

不得包入 `body/data/result/content`，不得返回裸数组、Markdown 围栏或代码之外的文字。
输出前核对：全部 claims 一一覆盖；ID 有输入依据；肯定结论引用直接证据；数组和数值类型正确。
