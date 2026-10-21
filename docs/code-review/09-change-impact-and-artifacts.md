# 09 变更影响与评审产物

[返回目录](README.md) · [上一步](08-execution-readiness.md) · [下一步：交接下游](10-generation-execution-and-report.md)

## 目的与分工

将需求实现结论、测试设计和代码改动关联起来，保存可供页面及后续节点使用的评审产物。程序派生变更范围、风险兜底与产物结构，LLM 可补充变更影响说明。

## 输入与输出

输入：需求映射、接口、证据、扫描清单中的变更信息、调用链、已校验测试设计和缺口。

主要输出为 `review_mapping.json`，当前 `schema_version` 为 `2.2`。

| 产物字段 | 内容 |
| --- | --- |
| mappings | 需求级结论和逐 Claim 依据 |
| field_value_contracts | 从接口参数中派生的字段值约束 |
| test_oracles | 源码支持的测试设计，包括有阻断的设计 |
| test_oracle_warnings | 原始设计警告与缺口说明 |
| test_oracle_diagnostics | 根据警告文本派生的分类、接口、场景、处理动作 |
| evidence_catalog | 初始及补充证据目录 |
| scan_coverage / deterministic_checks | 扫描完整性及部分证据门槛 |
| change_scope / removed_files | 关联接口的改动、删除文件 |
| unmatched_changed_files | 尚未直接关联到接口的改动 |
| call_chains | 调用关系和未解析信息 |
| change_summary | 可选的模型变更总评 |

## 当前处理过程

1. 根据扫描清单已有变更信息，关联接口及需求，不在此重新指定基线。
2. 区分接口相关变更、删除文件、未匹配变更文件。
3. 派生确定性增量发现及风险等级兜底。
4. 当存在可分析的变更范围时，将变更、调用关系摘要、接口、需求和映射交给影响分析模型。
5. 核对模型引用的接口、需求和文件身份，附加有效影响说明。
6. 将完整评审字段组装为产物，通过 Schema 校验后写入。
7. 标记 Code Review 完成，后续节点读取该产物。

影响分析使用的是专门整理的结构化输入及调用关系摘要，不等同于再次发送全量源码；“输入不截断”也不应描述为“每个模型调用都包含所有源码”。

## 失败处理

- 可选影响分析失败：回退程序生成的变更视图，不因此让核心评审失败。
- 核心需求/设计模型连接失败：不能用“没有证据”掩盖，核心流程会失败。
- 产物 Schema 不一致：按 `X-ORCH-01` 报程序派生产物错误，不能归咎于模型格式。
- 有接口却没有任何有效测试设计：先保留评审产物供查看，再停止在 Code Review，避免后续只报缺文件。
- 有设计但全部 blocked：不能据此宣称实际测试完成；节点完成与执行完成是两件事。

## 诊断分类

诊断使用原始警告及最终 test_oracles 对账，分为系统已处理（resolved/replaced）、框架边界、运行控制条件未满足、异常触发/数据条件未满足、响应预期未确认、未形成有效候选。正常设计已在最终产物保留时，不再把早期入口补查告警当成当前缺口；已被 DTO 源码设计替代的重复候选单独记录。候选自动修复成功会保存处理记录；已保留但断言待补的设计仍明确标为待确认。

页面展示系统已完成的处理和关联 oracle_id，原始过程原因可展开，已处理记录默认收起。文案不再要求用户修复引用、分支编号或手工完成系统应做的补证。无法确认的预期和未满足的运行条件仍如实保留，不因改文案被当作通过。

`GET /api/v1/jobs/{job_id}/review-diagnostics` 在与任务产物读取相同的权限检查下，按最终保存设计重新计算展示状态；历史任务也生效，不改写原始评审文件，不调用模型，不声称旧任务使用了新生成逻辑。

新出现的警告措辞如果没有专门匹配，可能落入通用分类，仍需查看完整原因及 oracle 字段。

报告节点进一步核对最终请求绑定数量，避免把补产前的候选告警显示为仍有请求缺口；原始 review_mapping.json 和 testcases.json 中的过程记录保留。详见 [执行交接与报告口径](10-generation-execution-and-report.md)。

## 示例

配置文件改动没有直接映射到接口时，应保留在未匹配文件列表；影响模型可以说明可能关联，但其推断不能替代接口真实调用依据或业务测试断言。

## 当前限制

- 变更归因和需求映射并非完整依赖分析。
- 可选影响分析成功不意味着核心测试设计完整。
- 警告列表可能同时保留早期失败及后续恢复信息，不能只数警告条数计算最终缺口。
- 所有步骤目前主要汇入一份评审产物，不是每步各有一份运行 JSON；本目录中的独立文件是逻辑说明文档。

## 代码入口

- [code_review.py](../../services/orchestrator/src/orchestrator/nodes/code_review.py)：`_derive_change_scope`、`_derive_incremental_findings`、`_analyze_change_impact_with_llm`、`_validate_change_impact`、`run_code_review`。
- [oracle_diagnostics.py](../../services/orchestrator/src/orchestrator/oracle_diagnostics.py)：`build_oracle_diagnostics`。
- [评审产物 Schema](../../schemas/review_mapping.json)。
