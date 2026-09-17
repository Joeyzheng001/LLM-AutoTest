# 真实模型运行 Demo：job-web-c3e382183000

这是一次已完成的真实 MiniMax 运行结果的**脱敏快照**，用于演示从需求解析、源码扫描、接口还原、Code Review、接口/E2E 用例生成、执行、缺陷闭环到回归沉淀的端到端产物形态。

## 安全处理

- 未提交原始需求文档、`job_input.json`、测试脚本、运行日志。
- 所有 JSON 中的密码、Token、API Key、认证头和本机绝对路径均已脱敏。
- 结果中的接口行为、断言预期、执行结果和缺陷关系保持原样，供界面与产物格式演示使用。

## 产物索引

- `requirement_review.json` / `requirement_items.json`：需求评审与可追溯功能需求。
- `code_inventory.json` / `interface_candidates.json` / `interfaces.json`：源码扫描与接口还原。
- `review_mapping.json`：需求与实现证据映射、风险与变更范围。
- `testcases.json` / `execution.json` / `defects.json`：测试设计、接口与 E2E 执行、缺陷清单。
- `regression_index.json` / `metrics.json` / `test_report.md`：回归资产、度量和最终报告。

> 该快照是 Demo 证据，不可作为可直接重跑的任务输入；要重跑请通过任务配置页重新上传文档并填写本机仓库目录与目标环境。
