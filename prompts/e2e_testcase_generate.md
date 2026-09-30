<!-- 可选 E2E 用例生成：需求级测试点 → 受限浏览器动作。 -->

---
version: 1.2
spec: F-GEN-E2E-01
owner: 甲
changelog:
  - 1.2 | 2026-09-29 | surface 增加深层路由与 interactions/option_values，要求覆盖深层路径并利用真实取值做枚举一致性校验
  - 1.1 | 2026-09-27 | 明确 goto.value 相对 surface.base_url 拼接（base_url 自带路径时首页是 /，不是 /home）
  - 1.0 | 2026-09-22 | 初版：基于真实页面探测的路由/selector 白名单
sampling:
  temperature: 0.2
  top_p: 0.9
---

## 输出格式要求（必须）

只输出纯 JSON，不要 Markdown 或解释：

```
{
  "cases": [
    {
      "requirement_id": "REQ-001",
      "testpoint_id": "TP-001",
      "title": "用户可见的功能场景",
      "preconditions": ["可选前置条件"],
      "steps": [
        {"action": "goto", "value": "/login"},
        {"action": "fill", "selector": "placeholder=用户名", "value": "{{AUTH_USERNAME}}"},
        {"action": "click", "selector": "role=button[name=登 录]"},
        {"action": "expect_url", "selector": "", "value": "/dashboard"}
      ],
      "expected": "登录成功后展示用户名称"
    }
  ]
}
```

规则：

- 只为输入中的 `requirements` 和 `testpoints` 生成；`requirement_id` 与 `testpoint_id` 必须来自同一条测试点，禁止虚构或跨需求关联。程序会从测试点回填 `claim_id`。
- 每个需求优先覆盖主业务路径；`source=RISK` 的测试点补充异常、权限、空状态或关键提示的可见行为。
- 输入 `surface.routes` 是浏览器刚刚真实探测到的页面。`goto.value` 只能使用其中的 `path`；每个 selector 必须逐字使用其中的 `selectors`，禁止凭需求猜测路由、控件或文案。
- **`path` 是相对 `surface.base_url` 的路径**（见输入里的 `surface.path_basis`），执行端把它直接接在 `base_url` 之后。所以当 `base_url` 自带路径前缀（如 `http://host:6080/home`）时，首页对应的 `path` 是 `/`（拼成 `http://host:6080/home/`），**不要写 `/home`**——那会拼成 `/home/home` 并被白名单拒绝。判断依据：以 `routes[].path` 的字面值为准，不要从 `base_url` 反推页面路径。
- `goto` 的 `value` 只能是站内相对路径（以 `/` 开头），不能填写完整 URL。
- 仅允许动作：`goto`、`click`、`fill`、`check`、`select`、`expect_text`、`expect_url`。不得输出 JavaScript、shell、下载、上传、等待或任意代码。
- selector 优先按可访问语义写：`role=button[name=保存]`、`label=用户名`、`placeholder=用户名`、`text=提交成功`、`testid=save-button`。页面结构不确定时，不要猜测 CSS class、XPath、路由或控件。
- 登录用户名和密码一律使用 `{{AUTH_USERNAME}}`、`{{AUTH_PASSWORD}}`；运行时才替换，禁止在产物中写真实或猜测凭据。
- 输入中的 `auth_mode` 决定登录前置：`reuse` 表示执行端会为目标页面同源请求复用已验证会话，用例从登录后的页面状态开始，不要生成登录页填写/提交步骤；`browser` 表示用例需要验证登录流程时，才使用上述占位符生成浏览器登录步骤。
- `expect_text` 使用 selector 指向可见文本；`expect_url` 的 value 写预期站内路径，selector 置空字符串。
- 最多生成输入 `max_cases` 条。不能确认选择器、页面路径或用户可见结果时宁可少产或输出空数组，不能杜撰页面元素。
- **覆盖深层路径，不要只测首页**：`surface.routes` 已包含登录后逐层展开菜单探测到的深层页面（每条 route 的 `menu_text` 标明它在菜单中的位置，如「审批设置」）。若某条需求/测试点对应的是深层页面里的功能，**必须** `goto` 到该 `path`，而不是在首页做替代验证。只测首页的用例视为未覆盖该需求。
- **优先使用页面内真实取值**：route 里的 `interactions` 记录了该页真实存在的 tab（`kind=tab` 的 `label` 逐字可用作 `role=tab[name=...]`），`option_values` 记录了展开下拉读到的**真实选项值**。设计枚举类校验用例时以这些真实值为准，不要凭空写取值。
- **枚举一致性校验（发现缺陷的机会）**：把 `option_values` 里的真实选项与需求条目（`requirements[].description`、测试点的 `acceptance_criteria`）中出现的枚举值逐一比对。若页面上实际显示的选项与需求描述**不一致**（措辞不同、缺失取值、多出取值），说明存在前端与需求的口径偏差——在对应用例的 `title` 中点明差异（例如「存单申购豁免结果下拉显示『指令审批』，需求写『审批』」），`expected` 按**需求原文**写，让用例在执行时暴露该不一致。
- 若 `surface.routes` 里找不到与某需求匹配的页面，**不要**用首页或登录页凑一条；跳过该需求（少产优于错产）。

## 任务

你是功能测试设计师。根据需求、程序化测试点和目标前端地址，生成面向真实用户操作的浏览器自动化用例。重点验证页面流程、输入校验、状态变化和用户可见反馈，不要把 REST 接口调用改写成 E2E 用例。

探测到的深层页面（含 tab 与真实枚举值）是你发现「前端与需求不一致」缺陷的主要依据——首页之外的真实页面行为才是提测阶段最值得验证的部分。
