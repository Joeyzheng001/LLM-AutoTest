# 风险审批试算接口测试用例

> 2026-10-19 验证更新：本文的“0 条”指早期归档，不能代表当前任务。当前任务已形成并实际通过本接口的 2 条精确异常请求契约。完整修复、验证结果和真实环境缺口见 [Code Review 修复与验证记录](code-review-validation.md)。以下其它业务场景仍是需要夹具的设计用例。

> 本文件针对接口 `IF-REST-002`，已结合本工程接口清单、源码和下游 DTO 核对。工程上一次生成归档包含 108 条其它接口用例，但本接口为 0 条：`review_mapping.json` 记录其源码预期未通过校验，生成器按规则跳过了没有可验证 oracle 的接口。阶段映射用例需要可控的 O32 结果、Apollo 开关和审批规则数据；当前执行器未提供这些下游服务桩，因此本文件明确列出测试夹具，不把它们伪装成已生成或已执行的自动化脚本。

## 接口与报文

| 项目 | 内容 |
|---|---|
| 接口 | `POST http://10.51.28.96:13001/icm-gateway/risk/approval/trial` |
| Content-Type | `application/json` |
| 请求体 | 直接传 JSON 对象，不包 `param` 字段 |
| 请求类型 | 控制器接收 `@RequestBody JSONObject`；没有专用请求 DTO，也没有 `@Validated` 字段校验 |

本工程现有 `interfaces.json` 对这个动态 JSON body 只登记了 `serialNo`。`hierarchicalAuthFlag`、`autoApprovalFlag`、`autoApprovalStage` 是继续追踪 Service 后发现的；O32 请求字段则来自 `O32RiskServiceImpl` 到内部 `RiskTrialDto` 的转换。它们不是一个被接口扫描器自动展开的 HTTP 入参 DTO。

网关自身读取的控制字段如下。它们均由 `JSONObject` 动态读取，不受 Bean Validation 约束。

| 字段 | 类型/取值 | 用途 |
|---|---|---|
| `serialNo` | String | 日志与试算响应流水号 |
| `riskChannel` | Integer；缺省按 `0` 处理，`1` 选择 STD，`2` 选择 O32 | 复用通用复合试算通道控制 |
| `hierarchicalAuthFlag` | String；`"1"` 开启 | 查询并返回分级授权标记 |
| `autoApprovalFlag` | String；`"1"` 开启 | 开启自动审批配置查询 |
| `autoApprovalStage` | String；`"00"`、`"01"`、`"02"` | 选择对应的自动审批阶段开关 |

除上述控制字段外，请求还需使用一份在目标环境已验证可成功执行 O32 试算的基础报文。O32 转换层读取 `BusinClass`、`EngagedNO`、`ExpireDate`、`FutSettDate`、`FutSettDate2`、`InstructionNo`、`InstructionType`、`MarketDate`、`SenderSubID`、`PurposeType` 和 `GroupList`；`GroupList` 明细字段按 `RiskTrialItemDto` 转换。源码没有声明这些字段为必填项，因此本用例集不自行编造交易业务值。

建议固定基础测试夹具：令 `riskChannel=2`、`serialNo` 每次唯一；O32 返回 `ErrorCode="0"`，并提供至少一条 `riskOutList`，其第二个 `|` 分隔字段是 `RISK-TC-001`（服务从该位置提取风控序号）。自动审批查询使用可控的配置开关与规则查询桩，以便验证阶段映射而不依赖线上配置数据。

请求体直接使用业务 JSON 对象，不需要外包一层 `param`。把下面控制字段合并到一份在目标环境已验证成功的 O32 试算请求中；其余字段不能用虚构值代替：

```json
{
  "serialNo": "<每次请求唯一值>",
  "riskChannel": 2,
  "hierarchicalAuthFlag": "0",
  "autoApprovalFlag": "1",
  "autoApprovalStage": "00"
}
```

`RiskTrialDto` 是网关转换后发给 O32 的内部 DTO，不是该 HTTP 接口的入参 DTO。根级字段映射为 `businClass`、`engagedNO`、`expireDate`、`futSettDate`、`futSettDate2`、`instructionNo`、`instructionType`、`marketDate`、`operator`、`purposeType` 和 `riskTrialItemList`。`GroupList` 明细识别这些输入字段：

```text
Account, AccruedInterestAmt, AdvanceLimitFlag, BondSettleType, BondSettleType2,
BondYield, BondYield2, BulkType, Bulkunderweight, BusinType, CashOrderQty,
ChangeType, CheckMemo, ContractNo, DestSecurity, EarnestBalance, ExDestination,
ExecBroker, FutSettDate, FutSettDate2, InquireId, InterestPayType, InvestType,
IssueDateEx, LimitUnit, NoticeDays, OrderQty, OriginContractNo, OtherCombiNo,
OtherInvestType, PledgeRatio, Price, PriceModel, RealBondInterest, RepoRate,
SaleChannelCode, SettlType, StockTargetType, StopPrice, StrategyPlacing, Symbol,
Term, WithDrawDate, WithdrawType
```

## 响应结构

成功分支返回 `RiskApprovalTrialResMessage`，继承原试算响应并新增：

| 字段 | 结构/含义 |
|---|---|
| `serialNo` | 原试算流水号 |
| `icmTrialObj` | 原有合规中心试算结果 |
| `o32TrialObj` | O32 结果，含 `ErrorCode`、`ErrorMsg`、`riskOutList` 等字段 |
| `hierarchicalOutList` | `风控序号|是否触发`；`0` 表示触发，`1` 表示未触发；默认 `[]` |
| `autoApprovalOutList` | `风控序号|豁免结果|自动备注`；默认 `[]` |

控制器是 `@RestController` 并直接返回响应对象；成功试算和服务内部捕获后返回的响应对象预期为 HTTP 200。未被服务捕获的异常状态码不在本用例集断言范围内。

## 核心阶段映射用例

每条用例均沿用上文的成功 O32 基础夹具，并让规则查询桩对 `RISK-TC-001` 返回一条匹配 `riskSystem="01"`、请求业务类型的规则，除非该用例明确验证“不查询”。`primaryBondDeclarationEnabled`、`primaryBondBidAwardEnabled` 和 `interbankCdDeclarationEnabled` 为服务端 Apollo 配置开关。

| 用例 ID | 优先级 | 配置与请求字段 | 预期结果 |
|---|---|---|---|
| TC-APPROVAL-TRIAL-001 | P1 | 仅开启 `primaryBondBidAwardEnabled`；请求 `autoApprovalFlag="1"`、`autoApprovalStage="00"` | 自动审批查询使用 `businessType="01"`；规则设 `exemptResult="01"`、`autoRemarkFlag="02"`，返回 `RISK-TC-001|01|`。 |
| TC-APPROVAL-TRIAL-002 | P1 | 仅开启 `primaryBondDeclarationEnabled`；请求 `autoApprovalFlag="1"`、`autoApprovalStage="00"` | 不调用自动审批规则查询；`autoApprovalOutList=[]`。用于防止阶段 `00` 被错误映射到申报开关。 |
| TC-APPROVAL-TRIAL-003 | P1 | 仅开启 `primaryBondDeclarationEnabled`；请求 `autoApprovalFlag="1"`、`autoApprovalStage="01"` | 自动审批查询使用 `businessType="01"`；规则设 `exemptResult="01"`、`autoRemarkFlag="02"`，返回 `RISK-TC-001|01|`。 |
| TC-APPROVAL-TRIAL-004 | P1 | 仅开启 `primaryBondBidAwardEnabled`；请求 `autoApprovalFlag="1"`、`autoApprovalStage="01"` | 不调用自动审批规则查询；`autoApprovalOutList=[]`。用于防止阶段 `01` 被错误映射到中标开关。 |
| TC-APPROVAL-TRIAL-005 | P1 | 仅开启 `interbankCdDeclarationEnabled`；请求 `autoApprovalFlag="1"`、`autoApprovalStage="02"` | 自动审批查询使用 `businessType="02"`；规则设 `exemptResult="01"`、`autoRemarkFlag="02"`，返回 `RISK-TC-001|01|`。 |
| TC-APPROVAL-TRIAL-006 | P2 | 仅开启 `primaryBondBidAwardEnabled`；请求 `autoApprovalFlag=" 1 "`、`autoApprovalStage=" 00 "` | 开关和阶段去除首尾空格后仍映射到阶段 `00`；查询使用 `businessType="01"`。 |

## 其它关键分支

| 用例 ID | 优先级 | 配置与请求字段 | 预期结果 |
|---|---|---|---|
| TC-APPROVAL-TRIAL-007 | P1 | 阶段 `00` 对应开关开启；请求 `autoApprovalFlag="0"`、`autoApprovalStage="00"` | 不查询自动审批规则；试算主体结果保留，`autoApprovalOutList=[]`。 |
| TC-APPROVAL-TRIAL-008 | P1 | 最小请求体 `{"autoApprovalFlag":"1","autoApprovalStage":"99"}`；不需要 O32、数据库或 Apollo 夹具 | `RiskApprovalRequestValidator` 在下游试算前抛出 `RiskServiceException`。本次目标环境对该请求连续两次观测均为 HTTP 200，响应 `returnCode=9999`、`returnMsg="系统繁忙，请稍后再试"`、`returnType="T"`、`body=null`。此响应断言来自运行观测，不是由异常类源码推断。 |
| TC-APPROVAL-TRIAL-009 | P2 | 阶段映射有效，规则查询返回空列表 | 每条有效风控序号生成空结果占位，例如 `RISK-TC-001||`。 |
| TC-APPROVAL-TRIAL-010 | P1 | 阶段映射有效，匹配规则 `exemptResult="01"`、`autoRemarkFlag="01"`、`autoRemark="测试备注"` | 返回 `RISK-TC-001|01|测试备注`。 |
| TC-APPROVAL-TRIAL-011 | P2 | 阶段映射有效，匹配规则 `exemptResult="01"`、`autoRemarkFlag="02"`，`autoRemark` 设置为非空值 | 返回结果第三段为空，例如 `RISK-TC-001|01|`；未开启自动备注时不泄漏配置文本。 |
| TC-APPROVAL-TRIAL-012 | P1 | O32 返回 `ErrorCode` 非 `"0"` | 跳过分级授权和自动审批查询；保留 O32 返回，两个新增结果列表保持 `[]`。 |
| TC-APPROVAL-TRIAL-013 | P2 | O32 返回 `ErrorCode="0"`，但 `riskOutList` 为 null、空或解析不到第二段风控序号 | 不查询审批设置；两个新增结果列表保持 `[]`。 |
| TC-APPROVAL-TRIAL-014 | P1 | 阶段映射到 `businessType="01"`；规则查询为 `RISK-TC-001` 返回两条匹配 O32 规则 | 返回仍为试算响应，HTTP 200；`o32TrialObj.ErrorCode="9999"`，`ErrorMsg="自动审批配置匹配多条O32记录，riskSerial=RISK-TC-001，businessType=01，count=2"`，两个新增结果列表清空。 |
| TC-APPROVAL-TRIAL-015 | P1 | `hierarchicalAuthFlag="1"`；O32 风控序号 R1 有授权记录、R2 无授权记录，且不含互斥业务场景 | `hierarchicalOutList` 按输入风控序号顺序返回 `R1|0`、`R2|1`；自动审批列表仍按其独立开关决定。 |

## 执行注意

- 阶段 `00` 与 `01` 都映射业务类型 `01`，区别在于各自使用的 Apollo 开关；用例 001–004 专门覆盖本次调整的映射互换。
- 阶段 `99` 在入口 `RiskApprovalRequestValidator.validate(param)` 被拒绝，后续 O32 试算不会执行。此前草稿误写为“跳过自动审批查询”，已按源码和本次运行观测修正。
- 自动审批查询异常会被服务捕获并写入 `o32TrialObj.ErrorCode="9999"`，同时清空新增列表；这类捕获后的试算 POJO 响应仍按 HTTP 200 断言。
- 网关鉴权是否强制取决于部署配置；执行时按目标环境鉴权模式携带请求头。不要在用例文件中保存真实令牌或真实业务流水号。
