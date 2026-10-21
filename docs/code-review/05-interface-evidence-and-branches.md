# 05 接口证据补齐与分支定位

[返回目录](README.md) · [上一步](04-claim-assessment.md) · [下一步：测试设计生成](06-test-design-generation.md)

## 目的与分工

补充理解接口自身处理逻辑所需的实现、请求/响应类型和辅助方法，建立可引用的分支目录。由程序收集和解析，不调用 LLM。

## 输入与输出

输入：接口完整定义、源码快照、初始证据、仓库文件清单。

输出：追加到 `code_evidence` 的证据条目，以及每个接口的 `branches`。补充证据继续分配 EVD 编号，最终进入 `evidence_catalog`。

## 证据补齐顺序

1. Controller 类注解和映射信息。
2. Service 声明及能够定位的实现。
3. 请求 DTO、校验注解、全局异常处理类。
4. 枚举定义和相关辅助数据访问方法。
5. Controller、Service 调用的可确定方法及相关常量。
6. 返回 DTO、继承字段、序列化注解和相关构造对象。

方法和类型的定位会使用包名、import、声明类型、方法名和参数数量等信息。存在多个候选时，不应任意挑一个实现作为事实。

## 分支目录如何建立

`_oracle_source_branches` 对每个证据片段：

1. 屏蔽字符串和注释对语法定位的干扰，按平衡括号提取 `if (...)`。
2. 按证据编号和条件顺序生成 `branch_id`，如 `EVD-0066:if:1`。
3. 收集本片段的局部赋值、循环别名及集合写入。
4. 沿这些关系传播条件来源标签。
5. 输出条件原文、主要来源和需要的控制种类。
6. 对控制器方法补入 `EVD-xxxx:catch:n` 异常出口和 `EVD-xxxx:return:n` 最终返回出口。catch 条件引用真实捕获语句；存在 Service/Client 调用时标为下游异常控制条件。最终 return 只定位出口，不证明数据库有记录、列表非空或内部 if 已覆盖。

异常候选未填出口编号时，程序根据候选引文与唯一 catch 方法体绑定；不根据场景标题猜编号。显式控制的数据场景可以通过已引用的返回赋值绑定唯一 return 出口。重复的同一源码片段只生成一份出口目录。

Service/Client 异常来源既识别 riskService 等接收者，也识别 `IRiskService service = ...` 后的 `service.execute(...)`，依据实际类型声明与调用绑定，不按异常场景文案猜测。

| 来源 | 识别线索示例 |
| --- | --- |
| request | 表达式关联接口请求参数 |
| downstream_response | Service/Client 调用、execute/exchange 返回 |
| application_config | 执行器配置列表、配置对象 |
| service_registry | 服务缓存、明细处理器注册表 |
| unresolved | 现有关系不足以确定来源 |

多个来源可以同时出现在 `required_controls` 中。`control_kind` 是按实现优先顺序选出的主要标签，不代表只有一种依赖。

## 示例

`if (o32TrialObj == null)` 的条件来自下游返回；`if (riskChannel == 1)` 可以来自请求；`if (detailProcessor == null)` 可能来自注册表查询。它们分别对应不同的测试前提，不能全部塞进请求 JSON。

## 失败处理与限制

- 缺文件、方法重载不唯一、实现类型不明确：保留证据/来源缺口，不假装解析成功。
- 来源传播按证据片段分别进行，避免不同方法同名局部变量串用；仍不是完整的跨方法参数绑定。
- 同文件可唯一定位的辅助调用持续遍历，以方法源码位置去重并停止循环，不再固定截在 8 个方法或两层调用。跨文件调用仍受声明类型、import 和唯一实现解析能力限制。
- 分支目录覆盖 if 条件及控制器 catch/最终 return，不完整枚举 switch、循环路径或全部异常传播。
- 目录包含分支，不等于模型一定为每个成立/不成立方向都生成了设计；当前没有全量对账补产保证。

## 代码入口

[code_review.py](../../services/orchestrator/src/orchestrator/nodes/code_review.py)：`_collect_oracle_controller_headers`、`_collect_oracle_service_evidence`、`_collect_oracle_auxiliary_evidence`、`_collect_oracle_enum_evidence`、`_collect_oracle_dao_evidence`、`_collect_oracle_called_method_evidence`、`_collect_oracle_response_evidence`、`_oracle_source_branches`。

[oracle_paths.py](../../services/orchestrator/src/orchestrator/oracle_paths.py)：控制器出口目录及引文绑定。
