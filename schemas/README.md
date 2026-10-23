# schemas/

全部结构化产物的 JSON Schema 存放目录（红线见 [docs/09_开发规范说明.md](../docs/09_开发规范说明.md) 第六章）。

## 约定

1. 每个 Schema 定义含 `schema_version` 字段；结构变更必须升版本，禁止原地改语义；
2. Schema 是数据模型的**单一事实来源**：orchestrator / test-executor 中的 pydantic 模型与之一一对应，java-parser 返回结构与 [contracts/](contracts/) 契约对齐；
3. LLM 输出（需求条目、接口还原、用例草案、Review 标注）必须通过对应 Schema 校验后才可进入下游产物；
4. **横切约定**（三人公共语言，详见 [docs/11](../docs/11_三人分工任务书.md) §5.4）：ID 命名 `REQ-001` / `CAND-001` / `IF-<协议>-001` / `TC-001` / `DEF-001`；时间戳 UTC ISO-8601；协议枚举小写 `rest/grpc/graphql/soap/mq`；`is_fallback` 全链路透传；错误结构 `{code, message, node_id?, details}`。

## 契约变更记录

- `testcases.json` 1.1（2026-10-23）：`unit_cases` 增加独立 `UT-xxx` 编号、目标组件、源码分支与关联 HTTP 用例，保留 1.0 历史产物读取。
- `execution.json` 1.1（2026-10-23）：单测结果增加缺口映射和目标类的 JaCoCo 行／分支合跑覆盖率，保留 1.0 历史产物读取。

## 目录规划（11 个产物 Schema，与 docs/11 §5.3 主笔表同一事实源）

| 文件 | 对应产物 | 主笔 | 依据 |
|------|----------|------|------|
| `requirement_items.json` | 需求解析条目 | 甲 | 模块 1（REQ） |
| `requirement_review.json` | 需求质量评审（REQ 子阶段） | 甲 | 模块 1（REQ） |
| `code_inventory.json` | 源码清单（仓库四态） | 乙 | 模块 2（CODE） |
| `interface_candidates.json` | 接口候选清单（与契约 ExtractJob 一致） | 乙 | 模块 3（REV 静态侧） |
| `interfaces.json` | 接口还原清单（LLM 补全后） | 甲 | 模块 3（REV） |
| `review_mapping.json` | 映射表 / 风险 / 遗漏三视图 | 甲 | 模块 4（REVIEW） |
| `testcases.json` | 生成的用例与脚本元数据 | 丙 | 模块 5（GEN） |
| `execution.json` | 执行结果（断言二态 + 环境失败分离） | 丙 | 模块 6（EXEC） |
| `defects.json` | 本地缺陷记录 | 丙 | 模块 8（DEF） |
| `regression_index.json` | 回归资产索引 | 丙 | 模块 9（REG） |
| `metrics.json` | 自动化率度量 | 甲 | 模块 10（MTR） |
| `samples/` | 跨人假数据样例（`_fake: true` 标注，演示物料禁用） | 见主笔表 | docs/11 §5.3 |
| `contracts/` | 服务间契约（OpenAPI / JSON Schema） | 分服务 | docs/09 第七章 |

> 上表为 M0 规划草案；首个已落地契约为 [contracts/java-parser.openapi.yaml](contracts/java-parser.openapi.yaml)（v0.1 草案，待 B+C 评审定稿）。
