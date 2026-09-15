---
name: spec-tester
description: 审计AI生成的测试代码质量（防假绿灯），检查覆盖率、断言强度、Mock真实性、异常覆盖、环境一致性。当用户说"检查测试质量"、"审计测试"、"mvn test全绿但不放心"时使用。
---

# Spec Tester Skill — 人机协作测试质量审计助手

## Skill 元信息

| 属性 | 值 |
|:---|:---|
| **名称** | `spec-tester` |
| **版本** | 1.0.0 |
| **类型** | 人机协作 Skill |
| **配套文档** | 《企业级 Spec Coding 标准化手册 V5 人机协同版》 |

---

## 触发条件

当用户提出以下请求时使用此 Skill：

- "帮我检查这个测试代码的质量"
- "审查 AI 生成的测试有没有假绿灯"
- "检查测试覆盖是否完整"
- "mvn test 全绿了，但我不放心，帮我审计一下"
- "测试通过了还需要人确认什么？"

---

## 可靠性声明

> **此 Skill 的设计严格区分"AI 可验证"和"必须人工验证"。**
>
> - ✅ 结构性和统计性检查：由 AI 执行，结果可靠（计数、模式匹配，零幻觉风险）。
> - ⚠️ 质量风险标记：由 AI 检测并标记为"潜在问题"，必须由人判断。
> - 👤 测试结果判定：AI 绝不执行，绝不声称测试"通过"。只有人运行 `mvn test` 的结果算数。
>
> **此 Skill 最核心的规则：AI 永远不说"测试通过了"这三个字。**

---

## 假绿灯全景：六种测试幻觉

在开始审计之前，理解 AI 生成的测试代码中最常出现的六种"假绿灯"——编译通过、运行通过、但实际上没有验证任何有意义的内容。

---

### 类型 1: 断言过弱（Weak Assertion）

**表现**：测试里有 `assert` 语句，但验证的内容太过表面，关键逻辑完全没有检查。

```
示例（安踏虚拟商品发货场景）:

❌ 假绿灯版本:
  AutoShipRecord record = recordMapper.selectByOrderId("O001");
  assertNotNull(record);  // ← 只验证了"有记录"

✅ 应该写:
  AutoShipRecord record = recordMapper.selectByOrderId("O001");
  assertEquals(OrderStatus.SHIPPED, record.getShipStatus());
  assertEquals("TMALL", record.getPlatform());
  assertEquals(0, record.getRetryCount());
  assertNotNull(record.getCreatedAt());
```

**危险信号**（AI 自动检测）：
- 测试方法只有 1 个 `assert` 语句
- 使用了 `assertNotNull` / `assertTrue` / `assertFalse` 而没有验证具体值
- 出现了 `assertEquals(1, 1)` 或 `assertEquals(true, true)` 等恒真断言

**类比**：你在机场过了安检，安检员在你的登机牌上盖了章——"已检查"。但你的包里其实有一瓶 500ml 的液体，安检员只看了一眼包的外表，根本没打开。

---

### 类型 2: Mock 过全（Over-Mocking）

**表现**：所有外部依赖全部被 Mock 了，测试运行在一个"真空世界"里，真实代码一行都没跑。

```
示例:

❌ 假绿灯版本:
  @MockBean private TmallAdapter tmallAdapter;
  @MockBean private OrderMapper orderMapper;
  @MockBean private AutoShipRecordMapper recordMapper;
  @MockBean private RocketMQTemplate rocketMQTemplate;
  @MockBean private DingTalkClient dingTalkClient;
  // ↑ 5/5 依赖全部 Mock —— 被测类的真实行为完全没有被测试

✅ 改进版本:
  @MockBean private TmallAdapter tmallAdapter;     // 外部 API 必须 Mock
  @MockBean private DingTalkClient dingTalkClient;  // 外部 API 必须 Mock
  // OrderMapper 和 AutoShipRecordMapper 使用真实数据库（H2 或 Testcontainers）
  // 验证数据库写入、事务行为、唯一索引约束
```

**危险信号**（AI 自动检测）：
- 被测类的所有依赖（100%）都被 Mock
- 有 `@MockBean` 但没有 `@SpyBean` 或真实 Bean
- Mock 的 `when().thenReturn()` 返回了"理想值"（如永远不抛异常）

**类比**：你测试一把锁的安全性，但是把锁拆下来放在一个没有门的桌子上测试。锁本身能转、钥匙能插——"锁是好的"。但你没测锁装在门上、门框有变形的情况下还能不能锁上。

---

### 类型 3: 异常不测（Exception Blind）

**表现**：所有测试都是 Happy Path——正常输入、正常返回。Spec 里写明了各种异常场景，但测试里一个都没有。

```
示例（安踏场景）:

Spec 3.4 错误分类表定义了 4 种错误类型，Spec 3.3 定义了 4 个极端场景处理。
但 AI 生成的 9 个测试全部是正常流程:

✅ testVirtualShipOnOutbound          — 正常发货
✅ testPureVirtualOrderNoSplit        — 纯虚拟订单不拆单
✅ testKeywordMatch                   — 关键字匹配
✅ testItemIdMatch                    — ID 白名单匹配
✅ testPhysicalItemNotIdentified      — 实物不会被误识别
✅ testMixedOrderSplit                — 混合订单拆分
✅ testSplitSuccess                   — 拆单成功
✅ testIdempotentShip                 — 幂等（正常情况）
✅ testShipSuccess                    — 发货成功

完全缺失的异常测试:
❌ testTmallApiTimeout                — 天猫超时后重试 3 次（错误类型: 可重试）
❌ testTmallApiTimeoutRetriesExhausted— 重试耗尽转人工处理
❌ testTmallApiInvalidParam           — 天猫返回参数错误直接失败（错误类型: 参数错误）
❌ testDuplicateRequestHandled        — 天猫返回幂等成功跳过（错误类型: 幂等成功）
❌ testTmallRateLimitedRetry          — 天猫限流等待后重试（错误类型: 限流）
❌ testNullOrderId                    — 空参数传入
❌ testOrderNotFound                  — 订单不存在
```

**危险信号**（AI 自动检测）：
- 所有测试方法中没有任何 `assertThrows` 或 `try-catch` 验证
- 测试数量 ≤ Spec 中 Given-When-Then 场景数量（说明只覆盖了正常场景）
- 没有 Mock `when().thenThrow()` 的调用

**类比**：你买了一辆车，试驾的时候只开了平坦的高速公路。"车没问题！" 但你从没试过急刹车、雨天行驶、陡坡起步。等真的遇到这些情况时，你才发现刹车片是坏的。

---

### 类型 4: 数据假造（Fake Data）

**表现**：测试用的数据格式与生产环境的真实数据完全无关，用假数据跑通的测试没有任何说服力。

```
示例（安踏场景）:

❌ 假数据:
  Order order = new Order();
  order.setOrderId("test-001");       // 真实格式: "20260425ANTA00001234"
  order.setUserId("user1");           // 真实格式: Long 类型纯数字
  order.setTotalAmount("100");        // 真实格式: BigDecimal, 含 2 位小数
  order.setPlatform("tianmao");       // 真实值: "TMALL" (枚举)
  order.setHasVirtualItem(2);         // 真实类型: TINYINT, 值: 0 或 1

✅ 真实数据（与 Spec 3.7 数据模型一致）:
  Order order = new Order();
  order.setOrderId("20260425ANTA00001234");  // 格式: yyyyMMdd + ANTA + 8位序号
  order.setUserId(100012345678L);             // Long 类型
  order.setTotalAmount(new BigDecimal("299.00")); // BigDecimal
  order.setPlatform(PlatformEnum.TMALL);     // 用枚举，不是字符串
  order.setHasVirtualItem((byte) 1);         // TINYINT
```

**危险信号**（AI 自动检测）：
- 测试数据包含 `test` / `mock` / `dummy` / `123` / `abc` 等占位符
- String 字段长度与 Spec 3.7 数据模型中定义的字段长度不匹配
- 使用了基本类型 `int` 但数据库字段是 `Long` 或 `BigDecimal`
- 枚举值使用字符串而不是枚举常量

**类比**：你让裁缝按照你给的尺码做衣服。裁缝说"我试过了，这衣服我穿着正好"。但你给的尺码是身高 175cm，裁缝试穿时用的数据是身高 180cm —— 他做的衣服是对的，但不适合你。

---

### 类型 5: 副作用忽略（Side Effect Blind）

**表现**：测试只验证了方法的返回值，但没有验证方法执行后对数据库、缓存、消息队列产生的副作用。

```
示例（安踏场景）:

❌ 假绿灯版本:
  // 只验证了方法返回值
  ShipResult result = autoShipListener.autoShip(order);
  assertEquals("SUCCESS", result.getStatus());

✅ 正确版本:
  // 1. 验证返回值
  ShipResult result = autoShipListener.autoShip(order);
  assertEquals("SUCCESS", result.getStatus());

  // 2. 验证数据库副作用
  AutoShipRecord record = recordMapper.selectByOrderIdAndPlatform(
      order.getOrderId(), PlatformEnum.TMALL);
  assertNotNull(record, "必须写入发货流水记录");
  assertEquals(ShipStatus.SUCCESS, record.getShipStatus());
  assertEquals(0, record.getRetryCount());

  // 3. 验证消息副作用（如果 Spec 要求发货成功后发 MQ）
  // verify(rocketMQTemplate).send(eq("order.ship.completed"), any(Message.class));
```

**危险信号**（AI 自动检测）：
- 测试中没有任何对数据库 Mapper 的查询验证（`.selectById()` / `.selectByXxx()`）
- `verify(mock)` 只检查了"调用了"（`times(1)`）但没有检查调用参数
- 被测方法是 `@Transactional` 但测试中没有验证事务提交后的数据状态

**类比**：你去药房取药，药剂师给了你一个袋子说"药配好了"。你检查了袋子里的药瓶数量——对的。但你不知道其中一瓶药的有效期是上个月的。你只检查了"给了几瓶"，没检查"每瓶是什么"。

---

### 类型 6: 环境差异（Environment Gap）

**表现**：测试在本机开发环境或 H2 内存数据库中跑通了，但生产环境使用不同的数据库、不同的中间件版本，同样的代码在生产上行为不同。

```
示例（安踏场景）:

❌ 测试环境:
  数据库: H2 (内存数据库)
  事务隔离级别: READ_COMMITTED
  唯一索引行为: 标准 SQL

  测试结果: ✅ 全部 GREEN

❌ 生产环境:
  数据库: TiDB 6.5 (MySQL 兼容)
  事务隔离级别: SNAPSHOT_ISOLATION (TiDB 特有)
  唯一索引行为: INSERT ON DUPLICATE KEY UPDATE 行为有差异

  生产结果: 💥 幂等逻辑失效，重复发货

具体差异:
  H2 中: INSERT ... ON DUPLICATE KEY UPDATE 冲突时返回 2 (updated)
  TiDB 中: 同样的语句可能返回 1 (inserted) —— 导致幂等判断失效
```

**危险信号**（AI 自动检测）：
- `pom.xml` / `build.gradle` 中 test scope 有 `h2` 依赖但 Runtime Profile 显示生产用 MySQL/TiDB
- 测试类上使用了 `@DataJpaTest` 或 `@JdbcTest`（默认使用 H2 或 Derby）
- 没有 `@SpringBootTest` 配合 `Testcontainers` 或真实数据库

**类比**：你在模拟飞行器上练了 100 个小时的降落，每次都完美落地。但真飞机和模拟器的操纵杆响应延迟不一样，你在真实跑道上第一次降落就把起落架撞断了。

---

## 审计流程

```
输入: Spec 文档 + AI 实现的代码 + AI 生成的测试代码
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│ Phase 1: 覆盖扫描                                         │
│  AI 操作: 统计 Spec 3.8 Given-When-Then 场景 → 比对测试    │
│  AI 操作: 统计 Spec 3.4 错误分类 → 比对异常测试             │
│  AI 操作: 统计 Spec 3.8 TDD 执行计划 → 比对实际测试        │
│  🛑 AI 不判断: 测试质量，只报告"有没有"                     │
├──────────────────────────────────────────────────────────┤
│ Phase 2: 断言审计                                         │
│  AI 操作: 扫描每个测试的 assert 语句                       │
│  AI 操作: 标记弱断言（assertNotNull/assertTrue/单断言）     │
│  AI 操作: 标记无有效断言（只有 verify(mock) 无数据验证）    │
│  ⚠️ 风险标记: assert 数量 ≤ 2 → 标记为"断言不足"            │
├──────────────────────────────────────────────────────────┤
│ Phase 3: Mock 真实性审计                                   │
│  AI 操作: 列出所有 @MockBean / @Mock 对象                  │
│  AI 操作: 逐个核对是否在 Spec 3.5 CodeAnchor 中有 ANC 编号   │
│  AI 操作: 计算过度 Mock 率（Mock 数 / 总依赖数）            │
│  🛑 AI 不判断: Mock 行为是否与现实一致                      │
├──────────────────────────────────────────────────────────┤
│ Phase 4: 异常覆盖审计                                      │
│  AI 操作: 统计含 assertThrows / thenThrow / try-catch 的测试│
│  AI 操作: 比对 Spec 3.4 错误分类表 vs 实际测试覆盖          │
│  AI 操作: 检测空 catch 块                                  │
│  ⚠️ 风险标记: 异常覆盖 < 50% → 标记为"异常覆盖严重不足"     │
├──────────────────────────────────────────────────────────┤
│ Phase 5: 环境一致性检查                                     │
│  AI 操作: 检查测试依赖 vs Runtime Profile 数据库类型        │
│  AI 操作: 检查是否有 Testcontainers 集成测试               │
│  ⚠️ 风险标记: 测试数据库 ≠ 生产数据库 → 标记"环境差异"       │
├──────────────────────────────────────────────────────────┤
│ Phase 6: 生成审计报告 + 人类 QA 清单                         │
│  AI 操作: 汇总 5 个 Phase 的结果                            │
│  AI 操作: 生成逐测试方法的诊断                              │
│  AI 操作: 生成 QA 审核清单（给人用的）                       │
│  🛑 AI 绝不填写: 测试结论（通过/不通过）                     │
└──────────────────────────────────────────────────────────┘
```

---

## Phase 1: 覆盖扫描

### AI 操作

1. 解析 Spec 文档中以下内容：
   - `3.8 验收标准` 中的 Given-When-Then 场景列表
   - `3.4 隐形边界声明` 中的错误分类表（4 种错误类型）
   - `3.3 增量变更` 中的极端场景处理表
   - `3.8 TDD 执行计划` 中的 RED-N 列表

2. 解析实际生成的测试代码：
   - 列出所有测试类文件和测试方法
   - 提取每个 `@Test` 方法的方法名

3. 生成对比矩阵

### AI 输出格式

```markdown
## 覆盖扫描结果

### 3.8 Given-When-Then 场景覆盖

| 场景 | Spec 定义 | 对应测试 | 状态 |
|:---|:---|:---|:---|
| 场景1: 0元虚拟商品自动发货 | ✅ | testVirtualShipOnOutbound | ✅ |
| 场景2: 混合订单拆单+发货 | ✅ | testMixedOrderSplit | ✅ |
| 场景3: 拆单失败转人工 | ✅ | **无** | ❌ 缺失 |
| 场景4: 发货API超时重试 | ✅ | testShipApiTimeoutRetry | ✅ |
| 场景5: 幂等性验证 | ✅ | testIdempotentShip | ✅ |

**覆盖统计**: 5/5 = 100%
**缺失场景**: 无

### 3.4 错误分类覆盖

| 错误类型 | Spec 定义 | 对应测试 | 状态 |
|:---|:---|:---|:---|
| 可重试 (NETWORK_TIMEOUT) | ✅ 有 | testShipApiTimeoutRetry | ✅ |
| 参数错误 (INVALID_PARAM) | ✅ 有 | **无** | ❌ 缺失 |
| 幂等成功 (DUPLICATE_REQUEST) | ✅ 有 | testIdempotentShip (正常幂等，非异常码) | ⚠️ 部分 |
| 限流 (RATE_LIMITED) | ✅ 有 | **无** | ❌ 缺失 |

**覆盖统计**: 2/4 = 50%
**缺失覆盖**: INVALID_PARAM, RATE_LIMITED

### 3.8 TDD 执行计划覆盖

| RED-N | 计划测试方法 | 实际是否存在 | 状态 |
|:---|:---|:---|:---|
| RED-1 | testKeywordMatch | ✅ | ✅ |
| RED-2 | testItemIdMatch | ✅ | ✅ |
| RED-3 | testPhysicalItemNotIdentified | ✅ | ✅ |
| ... | ... | ... | ... |

**覆盖统计**: 9/9 = 100%
```

### 👤 人工确认点

```
☐ Given-When-Then 场景是否全部有对应测试？
☐ 错误分类中的每种错误码是否有对应测试？
☐ TDD 计划中的 RED-N 是否全部实现？
☐ 是否有场景虽然"有测试"但测试方法名与实际验证内容不符？（人判断）
```

---

## Phase 2: 断言审计

### AI 操作

1. 扫描每个 `@Test` 方法中的所有 `assert*` 调用
2. 统计每个测试方法的 assert 数量
3. 标记弱断言模式

### 弱断言判定规则

| 模式 | 判定 | 理由 |
|:---|:---|:---|
| assert 数量 = 0 | 🚫 致命 | 测试没有任何断言，等于没测 |
| assert 数量 = 1 且类型为 `assertNotNull` | ⚠️ 弱 | 只验证了"不是 null"，没验证值 |
| assert 数量 = 1 且类型为 `assertTrue` | ⚠️ 弱 | 只验证了布尔值，没验证具体状态 |
| 只有 `verify(mock)` 没有 `assert` | ⚠️ 弱 | 只验证了调用，没验证结果 |
| 出现 `assertEquals(true, true)` | 🚫 假断言 | 恒真断言，一定是幻觉 |

### AI 输出格式

```markdown
## 断言审计结果

### 断言强度评分

| 测试方法 | assert 数量 | 弱断言标记 | 问题类型 |
|:---|:---|:---|:---|
| testVirtualShipOnOutbound | 1 | ⚠️ | 只 verify(mock)，无数据断言 |
| testMixedOrderSplit | 2 | ⚠️ | assertNotNull × 2，无值验证 |
| testKeywordMatch | 4 | — | ✅ |
| testShipApiTimeoutRetry | 1 | ⚠️ | 只验证了重试次数，未验证最终状态 |

### 汇总

| 指标 | 值 | 评级 |
|:---|:---|:---|
| 总测试方法数 | 9 | — |
| 平均 assert 数 | 2.1 | ⚠️ 偏低 |
| 弱断言方法数 | 5 | ⚠️ 55% 的测试断言不足 |
| 无断言方法数 | 0 | ✅ |
| 假断言方法数 | 0 | ✅ |
```

### 👤 人工确认点

```
☐ 弱断言测试中，是否有测试确实只需要弱断言？（如只验证"方法不抛异常"）
☐ 标记为"只 verify(mock)"的测试，Mock 行为是否足够代表真实行为？
☐ 是否有测试的 assert 验证了错误的条件？（人读代码判断）
```

---

## Phase 3: Mock 真实性审计

### AI 操作

1. 提取所有 `@MockBean` / `@Mock` / `@InjectMocks` 注解
2. 逐项核对 Spec 3.5 CodeAnchor 表
3. 计算过度 Mock 率

### Mock 核对规则

| Mock 对象 | CodeAnchor 对应 | 判定 |
|:---|:---|:---|
| TmallAdapter | ANC-04 | ✅ 已锚定 |
| OrderMapper | 无 | 🚫 幻影 Mock（不在 CodeAnchor 中） |
| Self-inventedService | 无 | 🚫 臆造的服务名 |

### 过度 Mock 判定

```
过度 Mock 率 = Mock 的 Bean 数 / 被测类的总依赖数

过度 Mock 率 > 80% → ⚠️ 警告: 接近全 Mock，真实行为验证不足
过度 Mock 率 = 100% → 🚫 致命: 全 Mock，被测类在真空中运行
```

### AI 输出格式

```markdown
## Mock 真实性审计

### Mock 对象清单

| Mock 对象 | 类型 | CodeAnchor 对应 | 状态 |
|:---|:---|:---|:---|
| tmallAdapter | @MockBean | ANC-04 (TmallAdapter) | ✅ |
| jdAdapter | @MockBean | ANC-05 (JdAdapter) | ✅ |
| orderMapper | @MockBean | **无对应 ANC** | 🚫 幻影 Mock |
| recordMapper | @MockBean | **无对应 ANC** | 🚫 幻影 Mock |
| rocketMQTemplate | @MockBean | **无对应 ANC** | 🚫 幻影 Mock |

### 过度 Mock 率

```
被测类 AutoShipListener 依赖: 5 个
Mock 数量: 5 个
过度 Mock 率: 100% 🚫 致命
```

### 建议

```
⚠️ orderMapper 和 recordMapper 建议使用真实数据库（H2 或 Testcontainers）
⚠️ rocketMQTemplate 如果是外部依赖，需先添加到 CodeAnchor 中再 Mock
```
```

### 👤 人工确认点

```
☐ 标为"幻影 Mock"的对象是否在项目中真实存在？（可能是遗漏了 CodeAnchor）
☐ 过度 Mock 的 Bean 中，哪些必须 Mock（外部 API），哪些应该用真实 Bean？
☐ Mock 的 when().thenReturn() 行为是否与真实接口一致？（人判断）
```

---

## Phase 4: 异常覆盖审计

### AI 操作

1. 扫描所有测试方法，统计：
   - 使用 `assertThrows()` 的测试数
   - Mock `when().thenThrow()` 的测试数
   - 包含 `try-catch` 的测试数
   - 空 catch 块的数量

2. 比对 Spec 3.4 错误分类表

3. 比对 Spec 3.3 极端场景处理表

### AI 输出格式

```markdown
## 异常覆盖审计

### 异常覆盖率

| 指标 | 值 | 评级 |
|:---|:---|:---|
| 异常测试方法数 | 1 / 9 | 🚫 仅 11% |
| 覆盖的错误类型 | 1 / 4 | 🚫 仅 25% |
| 覆盖的极端场景 | 2 / 4 | ⚠️ 50% |
| 空 catch 块 | 0 | ✅ |

### 缺失异常测试详情

| 错误类型 | Spec 定义 | 缺失的测试场景 |
|:---|:---|:---|
| 可重试 | NETWORK_TIMEOUT 重试 3 次后转人工 | testRetriesExhausted |
| 参数错误 | INVALID_PARAM → 直接失败 → 钉钉告警 | testInvalidParamTriggersAlert |
| 幂等成功 | DUPLICATE_REQUEST → 当作成功处理 → 记录日志 | testDuplicateRequestHandled |
| 限流 | RATE_LIMITED → 等待 30s 重试 → 最多 3 次 | testRateLimitedRetry |
```

### 👤 人工确认点

```
☐ 缺失的异常测试中，哪些是"可接受不测"的？（极低概率场景）
☐ 哪些是"必须补测"的？（高频场景或核心业务逻辑）
☐ 已有的异常测试中，Mock 的异常类型和行为是否与真实一致？
```

---

## Phase 5: 环境一致性检查

### AI 操作

1. 检查 `pom.xml` / `build.gradle` 中 test scope 的依赖
2. 比对 Spec 3.10 Runtime Profile 中的数据库类型
3. 检查是否有 Testcontainers 相关依赖

### AI 输出格式

```markdown
## 环境一致性检查

### 数据库一致性

| 环境 | 数据库 | 版本 |
|:---|:---|:---|
| 测试环境（pom.xml 推断） | H2 | 2.x |
| 生产环境（Spec 3.10） | TiDB | 6.5 |
| **一致性** | ❌ **不一致** | ⚠️ |

### 建议

```
⚠️ 测试使用 H2，生产使用 TiDB 6.5。TiDB 的 SNAPSHOT_ISOLATION 事务隔离级别
 与 H2 的 READ_COMMITTED 行为不同。
 建议: 增加 Testcontainers + TiDB 镜像的集成测试
 或: 至少增加 1 个针对事务隔离行为的专项测试
```

### 其他不一致项

| 检查项 | 测试环境 | 生产环境 | 状态 |
|:---|:---|:---|:---|
| 消息队列 | @MockBean | RocketMQ 4.9.4 | ⚠️ Mock 环境无法验证序列化 |
| 缓存 | @MockBean | Caffeine 2.9.3 | ⚠️ Mock 环境无法验证过期策略 |
| Spring Boot | 2.7.18 | 2.7.18 | ✅ |
```

### 👤 人工确认点

```
☐ 数据库差异是否可能导致测试假通过？（如事务隔离行为差异）
☐ 是否需要在 CI Pipeline 中增加 Testcontainers 集成测试？
☐ Mock 环境下无法验证的行为（序列化、缓存过期），是否有手动验证计划？
```

---

## Phase 6: 生成审计报告 + 人类 QA 清单

### AI 输出格式

```markdown
# 测试质量审计报告

## 概述

| 指标 | 值 | 评级 |
|:---|:---|:---|
| 场景覆盖率 | 100% (5/5) | ✅ |
| 异常覆盖率 | 25% (1/4) | 🚫 严重不足 |
| 平均断言强度 | 2.1 | ⚠️ 偏低 |
| 弱断言比例 | 55% (5/9) | ⚠️ |
| 过度 Mock 率 | 100% | 🚫 致命 |
| 幻影 Mock 数 | 3 | 🚫 |
| 环境一致性 | ❌ 不一致 | ⚠️ |

## 逐测试诊断

### 🚫 testVirtualShipOnOutbound
- 问题: 只 verify(mock)，无数据断言
- 风险: 不知道发货结果是否正确写入了数据库
- 建议: 增加对 auto_ship_record 表的查询验证

### ⚠️ testMixedOrderSplit
- 问题: assertNotNull × 2，未验证 SplitResultVO 的具体字段
- 风险: 拆分结果的 realOrderId / virtualOrderId 可能为空
- 建议: 增加对 SplitResultVO.success / .realOrderId / .virtualOrderId 的验证

### ⚠️ testShipApiTimeoutRetry
- 问题: 只验证了重试次数，未验证重试耗尽后的降级行为
- 风险: 重试 3 次后的状态可能未被正确记录
- 建议: 增加对 auto_ship_record 表中 ship_status 变为 FAIL 的验证

---

## QA 必须检查（人类清单）

> ⚠️ 以下项目 AI 无法判断，必须由 QA 逐项人工确认

### 断言正确性
☐ testVirtualShipOnOutbound: 补验证 auto_ship_record 表写入后，断言逻辑是否正确？
☐ testMixedOrderSplit: 补验证 SplitResultVO 字段后，断言值与预期是否一致？

### Mock 真实性
☐ ANC-04 TmallAdapter 的 Mock 行为：virtualShip(null) 返回什么？与实际是否一致？
☐ 幻影 Mock (orderMapper/recordMapper): 是否应该改用真实 Bean？

### 异常场景
☐ INVALID_PARAM 测试：补写后，验证的异常处理逻辑是否正确？
☐ RATE_LIMITED 测试：补写后，验证的重试间隔是否与 Spec 3.4 一致？

### 环境
☐ 是否需要在 CI 中增加 Testcontainers 集成测试？
☐ H2 vs TiDB 事务行为差异是否影响本次需求的幂等逻辑？

### 运行验证
☐ QA 是否已亲自运行 mvn test 并确认全部 GREEN？
   → 运行命令: _______________
   → 运行结果: _______________
   → 签字: _______________
```

### 👤 QA 签字确认

```
┌─────────────────────────────────────────┐
│           QA 测试质量签字                │
│                                         │
│  我已确认以上所有审核项                   │
│  我已亲自运行 mvn test 并确认全部 GREEN   │
│  我确认测试覆盖率、断言强度、异常覆盖      │
│  均达到上线标准                          │
│                                         │
│  签字: ___________  日期: ___________    │
└─────────────────────────────────────────┘
```

---

## 使用说明

### 输入要求

使用此 Skill 时，需要提供：
1. Spec 文档（已通过 Gate 3）
2. AI 实现的源代码路径
3. AI 生成的测试代码路径
4. 项目的 `pom.xml` / `build.gradle`（用于环境检查）

### 输出物

1. 测试质量审计报告（Markdown 格式）
2. QA 必须检查清单（给人用的）
3. QA 签字确认表格

### 配合其他 Skill

```
spec-writer → Spec 文档 → Gate 1/2/3 → AI 实现代码
                                          │
spec-reviewer ← Spec + 代码 ←────────────┘
                                          │
spec-tester   ← Spec + 测试 + 代码 ←──────┘
     │
     ├── Phase 1-5: 审计报告（AI 辅助）
     └── Phase 6: QA 签字确认
                    ↓
              通过 → 进入审查
              不通过 → AI 补测试 → 重新审计
```

---

*版本: 1.0.0 | 配套: 企业级 Spec Coding 标准化手册 V5 人机协同版*
*核心理念: AI 永远不说 "测试通过了" —— 只有人运行 mvn test 的结果算数。*
