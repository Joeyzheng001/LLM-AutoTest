# schemas/

全部结构化产物的 JSON Schema 存放目录（红线见 [docs/09_开发规范说明.md](../docs/09_开发规范说明.md) 第六章）。

## 约定

1. 每个 Schema 定义含 `schema_version` 字段；结构变更必须升版本，禁止原地改语义；
2. Schema 是数据模型的**单一事实来源**：orchestrator / test-executor 中的 pydantic 模型与之一一对应，java-parser 返回结构与 [contracts/](contracts/) 契约对齐；
3. LLM 输出（需求条目、接口还原、用例草案、Review 标注）必须通过对应 Schema 校验后才可进入下游产物。

## 目录规划

| 文件 | 对应产物 | 依据 |
|------|----------|------|
| `requirement_items.json` | 需求解析条目 | 模块 1（REQ） |
| `interfaces_reconstructed.json` | 接口还原清单 | 模块 3（REV） |
| `review_mapping.json` | 映射表 / 风险 / 遗漏三视图 | 模块 4（REVIEW） |
| `testcases.json` | 生成的用例与脚本元数据 | 模块 5（GEN） |
| `execution.json` | 执行结果（断言二态 + 环境失败分离） | 模块 6（EXEC） |
| `defects.json` | 本地缺陷记录 | 模块 8（DEF） |
| `regression_index.json` | 回归资产索引 | 模块 9（REG） |
| `metrics.json` | 自动化率度量 | 模块 10（MTR） |
| `contracts/` | 服务间契约（OpenAPI / JSON Schema） | docs/09 第七章 |

> 上表为 M0 规划草案；首个已落地契约为 [contracts/java-parser.openapi.yaml](contracts/java-parser.openapi.yaml)（v0.1 草案，待 B+C 评审定稿）。
