# 08 执行条件与可选运行观测

[返回目录](README.md) · [上一步](07-validation-and-repair.md) · [下一步：变更影响和产物](09-change-impact-and-artifacts.md)

## 目的与分工

把“设计有源码依据”与“环境已具备执行条件”分别记录。由程序判断和整理，不让 LLM 凭空宣布环境状态已经准备。

本步骤有两个位置：可选的固定异常运行观测在生成设计前执行；执行就绪判断在设计校验后进行。

## 输入与输出

输入：有效设计、请求前提、状态条件、源代码、任务配置、控制夹具配置、可选实际观测。

输出主要是 `control_kinds`、`execution_blockers`、`execution_readiness`，以及按配置产生的固定请求运行观测证据。

## 状态条件如何区分

| 条件 | 示例 | 处理 |
| --- | --- | --- |
| integration | 使用常规服务注册和配置，请求真实环境 | 不要求先制造某种异常状态，但仍需实际运行 |
| controlled | 强制下游 null、清空执行器配置、删除注册项 | 记录对应控制能力要求 |
| controlled test_data | 必须存在/处于指定状态的记录 | 无有效数据准备计划时形成阻断 |

控制种类包括 `downstream_response`、`application_config`、`service_registry`；测试数据另以 `test_data` 表达。

## 就绪判断

1. 核对请求前提属于真实请求字段，并检查当前规则能证明的路径条件。
2. 从 controlled 状态条件归集控制种类。
3. 匹配接口、场景及 setup/teardown 配对的控制配置。
4. 保留 HTTP 映射未知、条件来源未绑定、断言待补、数据条件缺失等阻断。
5. 根据缺少的控制能力和阻断，记录 blocked；无阻断但有状态前提可记录 conditional；其余可记录 ready。

最终对所有设计统一计算一次，包括模型循环之后追加的请求体边界、DTO 和枚举设计。生成侧复核历史设计时也汇总阻断，不能同时显示 ready 和 HTTP 映射未确认。映射未知但输入可控、没有其它阻断的请求仍可由执行器发送观测，不能据此判业务通过。

ready 是设计层判断，不代表目标地址可达、登录成功或执行时状态一定满足。配置里有 setup/teardown 也不等于真实服务已接线，执行器仍须运行并检查准备动作。

HTTP/结构检查不会仅因没有业务断言而被禁止；但明确的断言修复失败阻断不会因此被清除。

## 可选固定异常运行观测

仅当 `observe_runtime_contracts` 开启并有目标地址时执行：

1. 读取目标 OpenAPI，核对固定探测路径和方法。
2. 核对固定输入及源码调用顺序，要求能够在业务调用前拒绝。
3. 针对实现中列出的固定异常探测请求采集两次响应。
4. 两次响应一致且满足 JSON 等条件后，绑定精确请求与运行证据。
5. 派生仅适用该精确输入的运行契约，不能扩写为任意参数的通用预期。

这是项目内有限的固定探测能力，不是任意接口都能自动探索的通用机制。此次历史优化验证配置关闭了它；不能把它描述为每次 Code Review 必跑。

## 示例

- 缺失必填请求体：可单独生成框架边界设计；Spring 默认响应与项目包装可能不同。
- O32 返回 null：保留本接口兜底设计；当前无控制条件时暂不执行，不连带禁止普通入口。
- 非法 code：输入可由请求控制，HTTP 映射未知时可由后续执行器发送观测，暂不判通过或缺陷。
- 清空注册表：HTTP mock 不能直接证明远端 JVM 注册表已被修改。

## 当前限制

- 场景与控制配置部分依赖名称匹配，不等于完整接线证明。
- 对来源无法绑定的复杂条件仍可能保守阻断。
- Code Review 的文本诊断由规则分类，具体根因应同时查看设计字段及实际阻断。

## 代码入口

- [code_review.py](../../services/orchestrator/src/orchestrator/nodes/code_review.py)：`_oracle_uncontrolled_state_reason`、`_eligible_control_fixtures`、`_control_fixture_kinds_for_oracle`。
- [oracle_conditions.py](../../services/common/src/common/oracle_conditions.py)：`required_control_kinds`、`execution_blockers`。
- [oracle_runtime.py](../../services/orchestrator/src/orchestrator/oracle_runtime.py)：固定观测及精确输入契约绑定。
