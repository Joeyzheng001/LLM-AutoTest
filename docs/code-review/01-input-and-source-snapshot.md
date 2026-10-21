# 01 输入读取与源码快照

[返回目录](README.md) · [下一步：初始证据](02-evidence-and-scan-coverage.md)

## 目的与分工

确定本次审查的需求、接口及源码版本。由程序完成，不调用 LLM。Code Review 不在此处重新指定基线，也不自行把某个 commit 当成需求变更基线。

## 输入与输出

| 输入 | 用途 |
| --- | --- |
| 任务状态 `JobState` | 检查节点能否运行，记录运行状态 |
| `requirement_items.json` | 读取需求条目和 Claim |
| `interfaces.json` | 读取接口、参数、返回类型、源码位置 |
| `code_inventory.json` | 仓库、文件、变更范围等扫描清单 |
| `change_set.json` | 获取接口还原阶段实际使用的源码快照路径及扫描信息 |
| `job_input.json` | 后续读取模型、运行观测、造数及控制夹具等配置 |

本步骤输出为内存中的需求列表、接口列表、Claim 列表、接口索引以及补充源码路径后的仓库清单。没有独立的步骤产物文件。

## 当前处理过程

1. `engine.check_runnable` 检查前置条件，随后将节点标记为运行中。
2. 读取需求和接口；分别要求存在 `items`、`interfaces` 数组。
3. 读取代码扫描清单，缺文件时暂用空对象。
4. 从 `change_set.json` 回填各仓库的 `source_path` 到内存清单。
5. 将需求声明展开为 Claim；新格式按一条 Claim 一批准备审查。
6. 建立接口简表，保留编号、协议、签名、参数、返回类型和源码位置。

`source_path` 的意义是让评审读取接口还原时解析的同一份代码，减少“新源码配旧行号”的错位。回填不会改写原来的 `code_inventory.json`。

## 失败与兼容处理

- 缺需求或接口产物、对应数组无效：抛出 `X-ORCH-01`，不能进入正常评审。
- 缺代码清单：不是立即抛错，后续证据和覆盖判断会受影响。
- 缺 `change_set.json`：不会新建快照，沿用当前清单可用的仓库路径。
- 历史需求没有 Claim：进入旧格式兼容路径，读取需求到接口的映射；不等同于新格式逐声明审查。
- CLI 任务没有 `job_input.json`：后续主动探测、自动造数等能力默认关闭。

## 示例

接口还原在目标版本快照中识别到审批试算方法。评审应继续从该快照读取方法；即使开发工作区随后改动，也不应把它混入本次证据。

目标版本用于确定“审查哪份代码”；需求/代码差异基线来自上游配置和产物，两者不能混用。

## 当前限制

- 快照定位依赖上游产物是否保存了有效路径；缺路径时没有凭空恢复快照的能力。
- 简化后的接口索引用于需求映射，不包含原接口产物的所有辅助字段。
- Code Review 不负责重新扫描整个仓库来修复无效的上游接口清单。

## 代码入口

[code_review.py](../../services/orchestrator/src/orchestrator/nodes/code_review.py)：`run_code_review`、`_load_requirements`、`_load_interfaces`、`_load_code_inventory`、`_attach_scan_source_paths`、`_compact_interfaces`、`_flatten_requirement_claims`。
