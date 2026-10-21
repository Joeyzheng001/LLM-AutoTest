# codebase-graph-prd-rules 适用性评估

评估日期：2026-10-20。

仓库：[xsoway/codebase-graph-prd-rules](https://github.com/xsoway/codebase-graph-prd-rules)。本次读取的提交为 [98d6a7222b3bdce3c2fbdd78bfa818b4ed2d50aa](https://github.com/xsoway/codebase-graph-prd-rules/tree/98d6a7222b3bdce3c2fbdd78bfa818b4ed2d50aa)，包版本 1.0.0。

本次为静态源码与文档评估。没有安装、执行外部包，没有启用新分析流程，也没有用评估结论改写已有测试设计。

## 结论

适合借鉴业务规则抽取方法、证据分级与文档契约；模板可在保留许可要求的前提下复用。作为我们后台的自动分析组件，仍需做接口、结构化产物和公司模型调用适配。

建议优先利用现有 JavaParser 调用关系，增加有来源的业务规则整理环节，验证其对 Claim 到业务断言关联的帮助。是否引入外部 Graphify，应另行验证，当前不作为前置条件。

## 实际交付内容

| 仓库内容 | 已确认的用途 | 对我们意味着什么 |
| --- | --- | --- |
| 全项目与单模块两个 Skill | 指导 Agent 从源码整理规则文档 | 可以借用分析流程，不是已经运行的服务 |
| Prompt 与 references | 规定术语映射、链路回查、规则/测试表结构 | 可以适配成公司模型的分步输入输出契约 |
| Python 包 | 分发两个 Skill 的资产目录 | 安装包本身不会自动提取 Java 规则 |
| verify-skill-package | 检查必要文件、元数据和部分凭据模式 | 验证的是包结构，不是业务规则准确性 |
| eval YAML | 描述成功、输入不完整、范围边界三类用例 | 当前主要检查关键词，不能证明复杂 Java 或弱模型效果 |
| Graphify 使用说明 | 要求通过图谱定位后回查源码 | 仓库未实现图谱构建、Java 解析或查询引擎 |

`pyproject.toml` 的运行依赖为空；唯一控制台入口是包校验器。`__init__.py` 主要提供技能目录定位与枚举。模块 eval 配置使用 Codex，不能据此推断公司 deepseek-chat 的效果。

## 最有价值的借鉴

### 1. 在证据与测试设计之间整理业务规则

文档契约建议规则表包含：规则 ID、条件、决策、结果、例外、来源和测试断言。我们可将这些内容设计成结构化产物，再关联已有 Claim 和 oracle，而不是把“接口相关”直接当作“需求已被断言验证”。

当前问题是 44 个源码场景已经有请求，但 37 个接口维度需求点涉及的 10 条 Claim 仍缺有效业务断言或关联。规则整理有助于分辨：真正少了规则、缺可观察结果、只缺有证据的关联，还是接口归属过宽。它不自动消除这些缺口。

### 2. 图谱用于发现，源码用于确认

仓库明确区分 EXTRACTED、INFERRED、源码/SQL/配置复核和运行观测。我们可以将推断调用关系留为候选，只在源码确认后纳入预期依据；实际响应仍只证明特定环境和输入。

### 3. 从业务术语走完整路径

检索从业务名、对象、动作、状态、外部系统和表字段出发，再沿入口、服务、规则方法、Mapper/SQL、配置和外部边界回查。相比只依据接口标题关联需求，更利于确定某个 Claim 实际由哪个方法、哪个入口负责。

### 4. 将前置过滤、选择规则和执行控制分开

仓库分别描述资格过滤、候选排序、运行时控制。适配到风控接口时，具体类别需要按真实业务调整，但分清“能否进入”“如何选择”“何时提前返回/失败”的方法有价值，可帮助识别前序分支对目标路径的影响。

### 5. 以模块复用规则，保留入口差异

共享规则方法可以被多个接口引用，规则定义可复用；每个入口的请求字段映射、前序校验、状态条件和返回关系仍应单独记录，不能因为共享方法而宣称所有接口覆盖同一 Claim。

## 可直接复用与需要适配的部分

| 部分 | 建议 |
| --- | --- |
| 规则表、测试矩阵、待确认项的文档结构 | 可按 MIT 许可要求借用并注明来源 |
| 证据分级和图谱回查原则 | 可纳入现有说明及分析设计 |
| 单模块 Skill | 适合由已配置工具的 Agent 辅助整理文档；后台自动化需另外适配 |
| 整段 Prompt | 拆成规则抽取、验证、Claim 关联等步骤，并输出我们可校验的 JSON |
| Graphify | 单独核实具体实现、版本、Java/Spring/MyBatis 解析、公司模型配置及私有源码处理方式 |
| 包校验器及 eval | 可参考组织方式；验收必须增加真实源码、精确引用、规则条件与断言正确性检查 |
| 自动请求生成、断言执行、状态控制 | 继续使用本工具现有链路；该仓库没有交付这些组件 |

复制实际模板或代码时保留其版权和许可文本。许可来源见仓库 [LICENSE](https://github.com/xsoway/codebase-graph-prd-rules/blob/98d6a7222b3bdce3c2fbdd78bfa818b4ed2d50aa/LICENSE)。

## 建议的最小试点（尚未实现）

从审批试算一个接口开始，使用同一源码快照和公司配置模型：

```text
现有调用图及源码证据
  → 业务规则候选
  → 原文、条件、适用入口和输出关系复核
  → Claim 与规则的有依据关联
  → 已有 oracle 或待补设计
  → 沿用请求生成与执行流程
```

规则产物至少需要稳定 ID、来源版本、适用入口、条件、处理结果、例外/前序路径、证据引用、可观察结果及关联状态。具体 Schema 需实施前确定，以上不是当前已存在的产物字段。

评估重点：

- 对 10 条 Claim 的定位与关联是否更准确，是否减少无关接口展开。
- 是否新增有源码支持的业务断言，避免只增加结构检查或用例数量。
- SQL、配置及跨方法条件是否有有效来源，无法确定时是否保留缺口。
- 推断关系是否被误当事实，是否出现无依据的 covered_claim_ids。
- 公司模型输入输出是否完整留痕；用分批和规则复用控制任务规模，不裁掉所选源码。

图谱查找不能替代上下文；规则候选必须回查完整相关代码。无论是否使用 Graphify，都继续保持现有业务预期校验和执行状态区分。

## 主要来源

- [包定义](https://github.com/xsoway/codebase-graph-prd-rules/blob/98d6a7222b3bdce3c2fbdd78bfa818b4ed2d50aa/pyproject.toml)
- [包实现](https://github.com/xsoway/codebase-graph-prd-rules/blob/98d6a7222b3bdce3c2fbdd78bfa818b4ed2d50aa/src/codebase_graph_prd_rules/__init__.py)
- [单模块分析 Prompt](https://github.com/xsoway/codebase-graph-prd-rules/blob/98d6a7222b3bdce3c2fbdd78bfa818b4ed2d50aa/codebase-graph-module-rules/prompts/codebase-graph-module-rules.md)
- [图谱与证据边界](https://github.com/xsoway/codebase-graph-prd-rules/blob/98d6a7222b3bdce3c2fbdd78bfa818b4ed2d50aa/codebase-graph-business-rules/references/graphify-and-evidence.md)
- [规则文档契约](https://github.com/xsoway/codebase-graph-prd-rules/blob/98d6a7222b3bdce3c2fbdd78bfa818b4ed2d50aa/codebase-graph-business-rules/references/document-contract.md)
- [包校验脚本](https://github.com/xsoway/codebase-graph-prd-rules/blob/98d6a7222b3bdce3c2fbdd78bfa818b4ed2d50aa/codebase-graph-module-rules/scripts/verify_skill_package.py)
- [单模块成功用例](https://github.com/xsoway/codebase-graph-prd-rules/blob/98d6a7222b3bdce3c2fbdd78bfa818b4ed2d50aa/codebase-graph-module-rules/evals/cases/basic-success.yaml)

我们已有的调用关系实现见 [ExtractJobRunner.java](../../services/java-parser/src/main/java/com/llmautotest/parser/job/ExtractJobRunner.java) 的 `collectCodeSpans` / `walkCallGraph`；现有覆盖问题见 [报告优化记录](../testcases/report-improvements-2026-10-20.md)。
