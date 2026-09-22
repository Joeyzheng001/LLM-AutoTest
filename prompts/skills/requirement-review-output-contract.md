---
version: 2.0
spec: K-REV-02
owner: QA
changelog:
  - 2.0 | 2026-09-22 | 仅输出引用约束下的 findings；分数与展示对象由程序生成
  - 1.0 | 2026-09-18 | 需求评审节点末尾 JSON 契约
sampling:
  temperature: 0.2
---

## 最终输出契约

只输出一个 JSON 对象，根级只能有 `findings` 数组。每项只能包含主 Prompt 中规定的字段：
`type`、`priority`、`dimension`、`claim_ids`、`source_refs`、`location`、`question`、
`reason`、`impact`。`claim_ids`/`source_refs` 必须来自当前批；`reason` 必须说明明确依据。

没有问题时只输出 `{"findings":[]}`。不要输出旧版本的大型评审对象；`score`、`quality_grade`、
`summary`、`dimension_scores`、`quality_assessment`、`risk_flags`、`testable_features` 和
`mindmap` 都由程序派生。禁止 `body`、`data`、`result`、Markdown 围栏、注释或额外顶层键。

提交前逐项确认 JSON 可解析、类型/枚举正确、引用在本批、同一 claim 的 finding 没有重复。
