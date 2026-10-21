# 07 设计归一、校验与修复

[返回目录](README.md) · [上一步](06-test-design-generation.md) · [下一步：执行条件](08-execution-readiness.md)

## 目的与分工

区分格式瑕疵、引用登记问题、错误业务预期和未准备的执行条件。归一与校验由程序完成；需要补充或修正设计时调用同一任务配置的 LLM。

## 输入与输出

输入：模型原始候选、当前接口、其完整证据及分支目录。

输出可以是有效设计、带执行阻断的待补设计，或无法接受的候选及具体原因。原始回答保留在模型调用文件中，不用归一后的结果覆盖原始回答。

## 第一层：归一登记

程序可以做的整理包括：

- 给缺省的可选数组补空数组，移除不应由模型填写的运行派生字段。
- 在引用的原文有效时补登记 `evidence_refs`。
- 将等价的 Java 引文绑定回确切源码原文：只处理词法空白/注释差异及支持的相邻字符串常量拼接。
- 在唯一确定时，为缺失的请求条件引用绑定请求体注解、空白校验或读取字段的完整方法。
- 自动绑定顶层 RequestParam/PathVariable/RequestHeader 注解，以及声明 DTO 类型中的实际字段注解或字段声明。DTO 引用精确到当前字段，不引用整类而混入相邻字段的校验规则。
- 对符合规则的普通入口清除空 `branch_id` 等元数据。
- 对可确认的直接异常传递，联合补入“目标 if 抛出固定消息 → 控制器 try 调用该方法 → 匹配 catch 获取消息 → 写入实际返回对象”的多段原文。同一断言允许多条引文；消息被变换、嵌套路径或存在多个不同处理器时不猜测。
- 将必填请求体为空的输入归到框架边界，避免借用 Service 内部 null 分支。

这些处理不能改变变量、操作符、业务字面值或凭空补出响应预期。

## 第二层：严格校验

| 类别 | 主要检查 |
| --- | --- |
| 格式 | 顶层、字段集合、类型、路径、操作符、状态表达式 |
| 归属 | 接口存在，引用来自当前接口，目标分支可定位 |
| 条件 | 请求字段可由 HTTP 设置，DTO/上游调用不使该输入不可达 |
| 原文 | 各前提、状态条件、断言和状态依据有当前源码原文支持 |
| 响应 | 字段/映射依据有效，固定值有字面值支持，明确 HTTP 状态有对应依据 |
| 关系 | 请求回显确实来自请求 getter 与响应 setter，删除字段有删除/忽略证据 |
| 路径 | 不能把不被目标路径调用的错误工厂常量套到透传路径 |

`in/not_in` 支持非空有限值数组。`exists` 只确认路径存在，值为 null 也可通过；`null_or_missing` 用于允许空值或未序列化的字段。

## 第三层：修复与保留

1. JSON/契约校验失败时，公共修复机制最多再调用一次模型，保留原始业务输入并附错误。
2. 接口设计还会逐个处理失败候选，先检查是否是真实不可达输入，再进行定向修复。
   branch_id 失效属于可修复的证据定位错误，不再在预检阶段直接放弃模型修复；真实 DTO 不可达仍单独处理。
3. 一条候选失败不删除其他有效候选。
4. 修复仍失败时，先按解析器明确标识的 assertions[index] 逐项隔离错误断言，重排引文目标编号，再走完整解析器；不修改预期值。剩余断言可证时保留设计，清空需求覆盖声明，并单列未验证字段。普通入口若只剩有依据的 HTTP 检查，可按步骤 06 保留入口检查。
5. 若不能保留有效断言子集，正常、边界或异常候选可尝试移除响应断言，再走严格解析器。只有分支/出口、请求/状态条件及 HTTP 依据仍全部通过时，才保留为 `pending_expectation`；增加“响应断言未通过源码校验；保留分支设计，暂不执行”，清空业务覆盖声明。

此保留机制不适用于所有失败：要求有效来源分支/出口、原本非空的断言数组及能重新核验的条件。错误条件或错误来源不能靠移除断言被接受。保留下来的未证实断言设计仍 blocked，不生成虚假的业务通过。

异常消息与错误码分别核验。例如本出口实际调用 `ResponseCode.COMMON_ERROR.getReturnCode()`，没有取得枚举定义时，另一异常处理器中的 `-1` 不能证明该出口错误码也是 -1。若消息的跨方法传递链已证实，可保留消息断言，同时隔离错误码；不把整条异常设计删除，也不宣称错误码已验证。

## 示例

模型输出 `equals_request: source.ErrorCode`：source 是内部返回对象，不能当请求参数。模型改成 `equals: "source.ErrorCode"` 仍然不成立，因为这是变量引用文字，不是预期字面值。修复失败后，若源码分支和其他前提有效，可以保留该分支待补，不能判通过。

## 当前限制

- 校验是有限规则，不是完整 Java 类型、控制流与数据流证明。
- 部分判断仍依赖模式和场景文字，可能需要继续纠正误判。
- 引文确实存在只能证明来源真实，不能单独证明复杂业务语义全部正确。
- 断言强度分类和业务覆盖关联也不是完整的需求语义证明。

## 代码入口

- [code_review.py](../../services/orchestrator/src/orchestrator/nodes/code_review.py)：`_parse_test_oracle_response`、`_oracle_candidate_preflight_reason`、`_retain_pending_oracle_design`。
- [oracle_normalization.py](../../services/orchestrator/src/orchestrator/oracle_normalization.py)：`normalize_oracle_bookkeeping`。
- [java_source_citations.py](../../services/common/src/common/java_source_citations.py)：`exact_java_source_quote`、`factory_assertion_issue`。
- [response_assertions.py](../../services/common/src/common/response_assertions.py)、[dto_validation.py](../../services/common/src/common/dto_validation.py)。
- [llm_repair.py](../../services/common/src/common/llm_repair.py)：`complete_with_json_repair`。
