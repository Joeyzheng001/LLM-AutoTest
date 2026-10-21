<!-- F-REVW-01 支撑步骤：为评审批次按语义挑选代码证据。中转站无 embedding 接口，语义召回只能交给模型判断。 -->

---
version: 1.0
spec: F-REVW-01
owner: 甲
changelog:
  - 1.0 | 2026-09-27 | 初版：按 claim 语义从证据目录挑证据，替代纯词面重合召回
sampling:
  temperature: 0.1
  top_p: 0.8
---

## 输出格式要求（必须）

你的输出必须是**纯 JSON**（不带解释文字、不带 Markdown 围栏），顶层只有两个键：

```json
{"claim_ids": ["CLM-001"], "evidence_ids": ["EVD-0012", "EVD-0031"]}
```

字段规则：

- `claim_ids` 必须逐字列出输入 `claims` 中的全部 claim_id；
- `evidence_id` **只能引用当前 `catalog_page.items` 里出现过的编号**，禁止虚构、改写或补零；
- 只挑「判断本批 claim 时需要看」的证据：能证明某条 claim 已实现/未实现，或能说明其实现方式（状态码、分支、字段、事务、鉴权、配置）的片段；
- 不设证据条数上限；拿不准时宁可多给（后续评审会再判一次）；确实一条都不相关时返回 `[]`；
- 不要输出解释、分析、Markdown 围栏或上述两个字段之外的内容。

## 任务

你在做**语义检索**，不是字面匹配。`claims` 是中文需求拆解出的可验证断言，`catalog_page.items` 是当前页的 Java/Python 代码证据（含文件、行号和源码片段）。若条目带 `excerpt_part` / `excerpt_parts`，它是同一源码证据的连续片段；逐页判断，不能因为当前页没有命中就推断其它页也没有证据。两者用词往往对不上——例如：

- claim 说「审批规则」「设置类型」「豁免结果」，代码里可能是 `approvalRule`、`ApprovalSetting`、`exempt`、`RiskApprovalRuleServiceImpl`；
- claim 说「分级授权」「审批节点」，代码里可能是 `authorization`、`grade`、`node`、`RiskAuthorizationServiceImpl`；
- claim 说「权重」「最高优先级」，代码里可能是 `weight`、`priority`；

请按**业务含义**判断哪些片段与该批 claim 相关。`preferred_interface_ids` 是该批 claim 已关联的接口编号，命中这些接口的证据优先考虑（但不限于它们）。

若当前 `catalog_page.items` 为空，直接返回 `{"claim_ids": [输入 claims 的全部编号], "evidence_ids": []}`。
