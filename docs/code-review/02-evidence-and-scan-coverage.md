# 02 初始证据与扫描完整性

[返回目录](README.md) · [上一步](01-input-and-source-snapshot.md) · [下一步：需求证据召回](03-claim-evidence-selection.md)

## 目的与分工

把扫描结果转换成可追溯的源码证据，并说明审查范围有哪些缺口。由程序完成。

## 输入与输出

输入：代码清单、接口定义、快照路径、接口扫描完整性记录及需求 Claim。

输出为后续共用的数据：

- `code_evidence`：带 `EVD-xxxx` 编号的证据。
- `call_chains`：已有的调用链信息和未解析关系。
- `incremental_focus`：增量审查焦点。
- `deterministic_checks`：部分高风险需求类别的证据要求。
- `scan_coverage`：扫描完整性及缺口。

## 当前处理过程

1. 从扫描清单及接口位置收集接口实现、调用实现、行为/配置等证据。
2. 对接口/调用实现证据，按已记录的源码范围重新读取完整片段。
3. 不按“行号离得近”猜测某段代码属于哪个方法。找不到有效范围时，标记 `excerpt_truncated=true`，表示该证据不完整或不可用。
4. 对源码中识别出的敏感配置值进行脱敏；完整传输不意味着传输凭据原值。
5. 为初始证据分配 EVD 编号，整理调用链、增量焦点和确定性检查项。
6. 读取 `change_set.json` 的 `scan_integrity`；必要时回退读取 `interface_candidates.json`。
7. 汇总仓库读取状态、编码跳过、扫描范围以及接口实现证据缺失情况。

## 扫描完整性的含义

当前 `scan_coverage.complete` 主要要求：有仓库清单、仓库状态正常、没有已知范围/编码缺口、每个接口有完整实现证据、上游扫描完整性记录通过。

它是“目前记录的扫描条件满足”的结论，不能解读为所有业务路径均已解析。该函数的 `call_chain_truncated` 当前固定为 false，也不据此证明动态调用链完整。

## 失败处理

- 文件、编码、方法范围无法读取：保留缺口标记，不伪装成完整方法证据。
- 没有扫描完整性记录：`complete=false`，后续不能仅凭没找到实现就判断需求缺失。
- 某接口缺实现证据：进入 `interface_evidence_missing`。
- 证据缺失不会证明功能一定缺失；后续需区分“实现不存在”和“当前看不到”。

## 示例

接口存在，但 Service 实现文件读取失败：可以证明接口契约存在，不能仅凭接口路径证明自动审批已经实现，也不能据未读到的 Service 判定功能未实现。

## 当前限制

- 依赖上游接口识别和源码范围的准确性。
- 扫描完整性并非跨方法数据流或完整控制流的证明。
- 本步骤生成的初始证据用于需求审查；DTO、响应对象等额外证据还会在步骤 05 补充，当前不会自动回头重审所有 Claim。

## 代码入口

[code_review.py](../../services/orchestrator/src/orchestrator/nodes/code_review.py)：`_collect_code_evidence`、`_expand_oracle_evidence`、`_derive_call_chains`、`_derive_deterministic_checks`、`_scan_coverage`、`_load_scan_integrity`。
