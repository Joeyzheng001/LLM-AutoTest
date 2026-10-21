# 10 交接用例生成、执行与报告

[返回目录](README.md) · [上一步](09-change-impact-and-artifacts.md) · [公共机制：模型传输](11-model-transport-and-audit.md)

## 目的与边界

说明 Code Review 的设计如何成为请求、为何有些设计未发送，以及结果如何统计。本步骤属于 Code Review 之后的节点，列入本文档组用于明确交接关系。

## 输入与输出

输入：`review_mapping.json`、接口、需求测试点、目标地址、鉴权及执行配置。

输出包括 `testcases.json`、执行脚本、`execution.json`、测试报告、缺陷及回归产物。

## 用例生成

1. 再次校验评审设计的编号、接口归属、条件、引用和 DTO 可达性。
2. 使用任务配置的 LLM 生成满足设计前提的请求参数及用例。
3. 用程序核对生成参数是否满足该 oracle 的 preconditions。
4. 有效 oracle 是预期的依据：复制其状态表达式、响应断言和执行条件，不能让生成模型另写一套业务预期。
5. 运行观测契约只适用于原来的精确请求，不接受未证明的参数变体。
6. 没有有效 oracle 的用例移除未经验证的预期，标为 unverified。
7. 有源码设计却没有生成有效请求时，保留缺口/不可发送的占位信息，禁止用占位请求冒充实际执行。

源码设计数、有效请求数、独立 HTTP 请求数不是同一个指标。

### 必填请求体的负向输入

通用“缺少必填参数”检查须识别有源码依据的缺失请求体边界。仅当对应设计是 spring_required_body、boundary，已绑定唯一适用的 oracle，且请求条件明确为该 body equals null 时，允许缺失/null 请求体，不注入默认值。不放行普通业务用例的缺参，也不放行其它必填 path/query/header 参数。随后仍核对完整源码预期、请求条件和脚本。

Code Review、用例页和报告按最终设计对账过程告警，区分已自动修复、已被 DTO 设计替代、当前缺口及执行条件未满足。页面展示系统实际处理结果；原始过程记录折叠保留。历史任务的诊断投影不改变原始设计、请求或执行记录。

### 最终缺口与生成过程分别记录

候选被剔除后仍会进行按 oracle_id 的定向补产。请求绑定告警在补产和脚本定稿之后计算：已经恢复的候选记录为生成修复；只有最终仍未绑定有效请求的场景才报告请求缺口。占位请求和不可加载脚本不算绑定成功；有环境阻断但请求本身有效的设计仍算有请求设计。

需求覆盖按“接口 + 测试点编号”核对，同时读取 testpoint_id 与 testpoint_ids。多个接口上的同一 Claim 会形成多个测试点，告警同时给出测试点数和独立 Claim 数，不能按测试点数量直接凑请求。业务关联仍以源码支持的 covered_claim_ids 和断言为依据，不用结构检查填补业务覆盖。

## 执行前检查

执行器结合设计及用例，汇总全部已知阻断和缺少的控制种类，再检查请求、鉴权、环境及相关准备动作。

仅 HTTP 映射未知、没有额外状态控制要求、且请求前提已满足的异常场景，可以发送观测。它不会因为收到响应就自动获得一个新的正确预期。

未准备的下游 null、注册表缺失、配置清空等场景保留但不发送。一个分支 blocked，不应阻止同接口其他可执行场景。

## 执行状态

| execution_state | 含义 | 是否独立发请求 |
| --- | --- | --- |
| executed | 按已有契约进行了执行和判断，可能通过或失败 | 查看 request.attempted |
| observed_unjudged | 已收响应，但契约待确认或目标路径未触达 | 是 |
| not_executed | 缺条件、无可用预期或请求等，未发出 | 否 |
| reused_response | 使用同接口已有真实响应检查另一条已有设计 | 不新增请求，保留来源 |

不能仅看 passed=false 判产品缺陷；需要查看 `failure_kind`、执行状态及原因。

## 路径观察与有限响应复用

执行后，程序尝试根据本接口的 getter/setter、返回 DTO 字段和部分前序提前返回条件观察路径。

- 若能看出目标路径前序条件不成立，将其记为目标分支未触达；即使空列表等响应断言通过，也不算该目标通过。
- 只对满足限制的下游状态设计尝试复用已有响应；要求同接口、鉴权模式匹配、没有夹具状态上下文冲突、来源请求满足目标前提、可观察条件全部确认。
- 配置、注册表及未知内部条件不能靠响应复用宣布准备完成。
- 复用记录保存来源用例和实际参数，不把目标脚本说成已经独立运行，也不直接归档为独立执行过的回归脚本。

路径观察是有限的源码与响应对应，不是 JVM 分支埋点，也不等同于内部覆盖率工具。

## 报告口径与示例

报告分别展示实际请求数、断言通过/失败、环境失败、已请求待判定、未执行和复用结果；HTTP/结构通过与业务断言通过也分别说明。

顶部汇总展示总数、通过、断言失败、环境失败、待判定、未执行，满足“总数 = 通过 + 断言失败 + 环境失败 + 待判定 + 未执行”。复用响应已经计入通过或失败，另列复用数量，不再次加总；实际请求次数作为另一项指标展示。

页面以统一用例明细展示所有状态，包括通过用例。支持点击汇总卡筛选、按接口筛选、搜索、展开/收起当前列表。详情中请求与响应并排展示（窄屏上下排列），提供复制、请求头脱敏说明和逐字段断言；复用用例展示来源请求、来源耗时并明确没有另发请求，原控制条件以“独立执行所需条件”说明。已核验预期但未执行的记录不会仅因缺少运行时 assertion_scope 就显示成预期待确认。完整 JSON 通过滚动查看，不裁掉内容。

### 按接口与场景查看（2026-10-20）

页面先按接口分组，显示方法、路径和摘要，再分为正常流程（蓝）、边界场景（黄）、异常分支（紫）；类型标签、文字分组和侧边色条同时提供，不依赖颜色单独传意。左侧通过/失败等标识继续表示执行结果，异常用例也可以通过。顶部场景按钮与结果、接口、搜索筛选交叉生效；全局场景数量不因结果筛选而改变，每接口另显示当前可见数/接口总数。

场景类型读取执行记录的 category；仅当关联用例明确 `oracle_basis=integration_entry` 时显示“正常入口”。正常类中可能包含兜底处理或有条件的正常分支，颜色不证明内部路径已触达。API 报告在无额外筛选时还展示接口清单中没有执行记录的接口，提示设计/请求缺口，不把它们算作 0 条通过。

接口和用例元数据并行加载，历史缺少这些附加产物时仍展示执行结果；无法确认入口标记时只使用已有类型分类。通过用例继续可展开查看完整请求、响应、断言。

历史生成产物可能保存补产前告警。报告依据最终请求绑定情况说明已恢复的告警，保留原始生成产物供追溯；不会改写源码预期或实际执行记录。

例：O32 错误导致接口提前返回，响应中的审批列表为空。它可以是一次真实请求，但不能因此证明自动审批开关分支已经走到。缺失请求体收到项目包装的 HTTP 200，也不能继续叫“未执行”，或直接把预期改成 200。

## 当前限制

- 自然环境不保证产生所有条件化场景；本轮暂不接线 mock。
- 响应路径观察支持有限源码模式，未知条件不能按已满足处理。
- 缺少完整分支对账，因此不能仅凭设计数减少或跳过数降低判断优化成功。
- `oracle_status=verified` 表示源码契约通过核验，不表示环境已准备或实际业务已通过。

## 代码入口

- [testcase_generate.py](../../services/test-executor/src/executor/nodes/testcase_generate.py)：`run_testcase_generate`、`_filter_oracle_violations`。
- [auto_execute.py](../../services/test-executor/src/executor/nodes/auto_execute.py)：`run_auto_execute`、`_can_observe_unknown_contract`、`_observe_source_paths`、`_summarize`。
- [java_response_paths.py](../../services/common/src/common/java_response_paths.py)：`response_path_observation`。
- [test_report.py](../../services/orchestrator/src/orchestrator/nodes/test_report.py)、[retest_regression.py](../../services/test-executor/src/executor/nodes/retest_regression.py)。
- [执行产物 Schema](../../schemas/execution.json)。
- [design_coverage.py](../../services/common/src/common/design_coverage.py)：`oracle_request_bindings`。
- [TestReportView.vue](../../services/frontend/src/views/TestReportView.vue)、[ExecutionResultDetail.vue](../../services/frontend/src/components/ExecutionResultDetail.vue)：报告筛选及请求/响应详情。


## 独立 E2E 业务测试（2026-10-20）

新版 E2E 可从工作台独立生成和执行，不以 API oracle 为启动条件；原九节点内开关仍保留。业务协议、逐步证据、缺陷复测及回归快照的代码事实见 [E2E 实现说明](../e2e/implementation.md)。接口覆盖和 UI 业务覆盖分别统计。
