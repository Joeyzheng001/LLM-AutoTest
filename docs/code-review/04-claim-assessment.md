# 04 需求符合性审查与汇总

[返回目录](README.md) · [上一步](03-claim-evidence-selection.md) · [下一步：接口证据及分支](05-interface-evidence-and-branches.md)

## 目的与分工

判断每条需求声明由哪些接口、哪些源码支持。LLM 作语义判断；程序校验引用、检查必要证据并汇总结论。

## 输入与输出

输入：一条 Claim、一个接口、当前证据页、相关调用链/增量焦点、确定性检查项和扫描完整性。

模型输出 `assessments`，每项包含 Claim 编号、状态、置信度、接口与证据引用、已满足条件、未满足条件和待核查项。

最终汇总为 `mappings`：需求级结论及其逐 Claim 明细。它不直接产生 HTTP 测试请求。

## 当前调用粒度

```text
每条 Claim
  → 每个接口
    → 当前接口关联的每个证据页
      → 一次模型审查（格式错误时可能再修复一次）
```

长证据先按约 4,000 字符连续拆片，再按约 8,000 序列化字符分页。每页标明 Claim、接口、证据页序号。

在多接口或多证据页情况下，`interfaces_complete` 不会告诉模型“本页就是全仓所有内容”。

## 调度及合并顺序（2026-10-20）

- 每任务默认最多 4 条独立 Claim 同时进行召回/审查，同 Claim 的接口和证据页继续按顺序执行。
- Claim 全部完成后，按输入 Claim 顺序合并，随后执行原有确定性门槛与需求汇总；完成先后不改变结果编号及汇总顺序。
- worker 不共享可变的 assessment 列表。任一核心调用抛出错误时，取消并等待同批其他 worker 退出，再向上抛出原始错误，禁止后台残留调用继续写产物。任务取消同样回收 worker。
- `review_log.jsonl` 在召回前记录 Claim started，完成后记录 completed、完整耗时和配置并发数；批次 index 是原始序号，不是已完成计数。

## 状态与程序处理

| 状态 | 含义 |
| --- | --- |
| implemented | 给出的证据支持该声明全部条件 |
| partial | 有已实现部分，也有明确未满足部分 |
| missing | 有充分依据判定能力缺失，且扫描条件允许该判断 |
| undetermined | 证据不足或当前无法判断 |

程序检查当前批 Claim 是否被正确引用、接口/EVD 是否存在、各状态是否有要求的条件说明。格式或引用错误通过公共 JSON 修复机制再调用一次模型。

跨页/接口合并当前采用规则：有 partial 优先为 partial；否则有 implemented 则为 implemented；只有全为 missing 且扫描完整时才判 missing；其余为 undetermined。引用和条件说明去重保留。

随后应用确定性门槛：扫描不完整时将 missing 降为 undetermined；高风险类别没有相应引用支持时，也可能降为 undetermined 并限制置信度。

需求级汇总再合并其全部 Claim：全实现才为 implemented；实现与未知混合为 partial；置信度取相应 Claim 的最低值。

## 示例

“开启自动审批时阶段值必须合法”需要校验逻辑证据。只有审批接口存在，或只有一个同名参数，不足以证明该 Claim 已实现。

一个需求包含三条 Claim，其中两条已实现、一条无法判断，则需求整体为部分实现，保留三条各自的依据。

## 当前限制

- 多页结论通过上述规则合并，不会再把全部原文一起交给模型作最终联合审查。
- 某页 implemented 加另一页 undetermined，当前可能合并为 implemented；这不是跨页全部条件的形式化证明。
- 后续接口专用证据补齐后，不会自动重审全部 Claim。
- 声明“源码已实现”不代表测试已执行或所有业务分支已覆盖。

## 代码入口

- [code_review.py](../../services/orchestrator/src/orchestrator/nodes/code_review.py)：`run_code_review`、`_merge_claim_assessments_compat`、`_merge_interface_claim_assessments`、`_apply_claim_deterministic_gates`、`_aggregate_claim_assessments`。
- [需求审查提示词](../../prompts/code_review.md)。
