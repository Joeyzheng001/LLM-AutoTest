---
version: 2.0
spec: F-E2E-CASE
owner: QA
changelog:
  - 2.0 | 2026-10-20 | 单业务场景生成可执行准备、操作、断言与清理；引用需求原文，保留 frame 与状态
sampling:
  temperature: 0.2
---
为输入的唯一场景生成一条完整 E2E 用例。真实页面 facts 来自完整观察分页，source_page 可追溯。只输出 {"cases":[{...}]}。
用例字段：requirement_id、rule_refs（当前 rule_id 数组）、title、scenario_type、priority、role、preconditions（文字数组）、setup（准备步骤）、precondition_checks（检查步骤）、steps（业务步骤及断言）、cleanup（清理步骤）、expected、execution_blockers（确实无法满足的条件数组）。
每个步骤含 action、intent（业务意图）；按需含 selector、target_intent、within（语义容器数组）、frame、page、value、variable、resource、destination。顶层 frame=null；iframe 必须引用实际 url/name。页面默认 main。
所有业务断言必须含 basis={"rule_id":"当前规则","quote":"当前规则原文"}。steps 至少包含一条验证业务结果的断言；不能只有打开页面、填写、点击或自然语言 expected。
允许操作：goto/click/dblclick/fill/press/check/uncheck/select/hover/scroll/drag/upload/download/open_popup/open_actor/close_page/capture_text/capture_value/screenshot/fixture。
允许断言：expect_text/expect_exact_text/expect_url/expect_visible/expect_hidden/expect_value/expect_count/expect_checked/expect_unchecked/expect_enabled/expect_disabled/expect_options/expect_response。
selector 使用真实 role=button[name=保存]、label=字段名、placeholder=提示、text=文本、testid=标识；保留页面/frame 归属。尚未出现的业务提示可以按需求原文使用 text=... 做断言，不能把实际缺失当作禁止验证的理由。
role 只能使用 available_roles 的身份名，默认 default，未登录用 anonymous。goto/expect_url 的 value 为该站点 origin 下的 /路径；不可使用完整 URL。expect_count.value 为整数；expect_options.value 为完整选项数组。
expect_response 使用 method、path、assertions（path/op/value 形式，检查对象包含 status/body）。来源必须是已配置或已观察的业务请求，不能仅 HTTP 200 代替业务结果。
变量 {{UNIQUE}}/{{RUN_ID}}/{{CASE_ID}} 由程序生成；{{AUTH_USERNAME}}/{{AUTH_PASSWORD}} 仅运行时替换。提取动作通过 variable=RECORD_ID 生成后续 {{RECORD_ID}}，不猜历史 ID。
open_actor 使用 resource=available_roles 中的角色和 page=窗口名称，新身份打开后通过 page 指定后续步骤；跨角色业务使用提取变量传递真实记录 ID。fixture/upload 必须引用输入 available_resources 的名称 resource，不能指定任意文件或执行任意代码。没有所需资源时保留 execution_blockers。
文字前置必须有对应 setup 或 precondition_checks；无法准备时保留阻断，不把数据条件假设为已成立。只生成本场景，不替换为首页检查。
预期不可因实际页面结果不同而修改。正常新增应验证列表重新查询等最终业务后置条件，不能只有短暂成功提示。每步截图与过程证据由执行器自动保存，不需要插入大量 screenshot 步骤。

动态表单或弹窗未在设计时展开：先使用已观察到的业务入口；后续步骤可以用 target_intent 描述需求规定的目标字段/控件而不填写猜测的 selector。执行器在该步重新观察并调用模型定位真实控件，再执行原动作。不能因此删除整个业务流程。目标不存在时保留现场定位缺口。
业务反馈文字的正确值仍来自需求，使用 expect_text/expect_exact_text 等断言；不得把预期文字改成实际返回的错误文字以通过。

准备和 precondition_checks 的可达性、登录、数据存在性检查是就绪检查，可以不填 basis；steps 中每个 expect_* 都必须填 basis。不要为了引用需求而把无关的首页标题检查说成业务断言。
expect_exact_text/expect_text 必须同时填写 selector（或 target_intent）及 value，value 为需求规定的文本。身份未配置时 role="default"，禁止凭空填写 admin。
最小结构示例（仅说明协议，实际内容必须使用当前输入，不要照抄）：
{"cases":[{"requirement_id":"输入 requirement_id","rule_refs":["输入 rule_id"],"title":"输入场景标题","scenario_type":"normal","role":"default","priority":"P1","preconditions":[],"setup":[{"action":"goto","intent":"进入入口","value":"/"}],"precondition_checks":[],"steps":[{"action":"click","intent":"打开业务入口","target_intent":"输入中真实业务入口"},{"action":"expect_exact_text","intent":"核对需求结果","target_intent":"业务反馈区域","value":"需求规定的结果","basis":{"rule_id":"输入 rule_id","quote":"逐字复制当前规则原文"}}],"cleanup":[],"expected":"需求规定的结果","execution_blockers":[]}]}

expect_text/expect_exact_text 的固定 value 必须是需求原文规定的文字（或明确的运行变量）；不能因观察到侧栏名称不同，就把工作台页签名称替换为侧栏的文字。场景原文与观察文本不一致时仍保留需求预期。
