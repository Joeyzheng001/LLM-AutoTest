<!-- F-GEN-01~04 / R-GEN-01~04：接口 + 需求 → 测试用例（含断言语义）。参数一致性（R-GEN-02）与脚本可加载性（R-GEN-03）由程序校验，违规用例丢弃并重生成一次。 -->

---
version: 1.5
spec: F-GEN-01
owner: 甲
changelog:
  - 1.5 | 2026-09-16 | 引入程序化测试点（REQ/RISK 溯源），用例从功能场景展开，不只围绕接口参数
  - 1.4 | 2026-09-16 | 增加 auth_mode，避免全局有效会话覆盖未认证/伪 token 反例
  - 1.3 | 2026-09-16 | 预期结果改用 status/status_class 机器可判定语法，执行器兼容旧版 HTTP 自然语言
  - 1.2 | 2026-09-16 | 按接口分批生成：只对当前 interfaces 批次产出用例，降低长 JSON 截断风险
  - 1.1 | 2026-09-16 | C-17：输入可含 auth 凭据（登录类接口 normal 用真实凭据、exception 用错误凭据）
  - 1.0 | 2026-09-15 | 初版（输入为接口清单 + 需求条目 + REQ↔IF 映射；脚本由程序模板渲染，LLM 只产用例元数据）
sampling:
  temperature: 0.3
  top_p: 0.9
  seed: null
---

## 输出格式要求（必须）

你的输出必须是**纯 JSON**（不带任何解释文字、不带 Markdown 代码围栏），结构如下：

```
{
  "cases": [
    {
      "interface_id": "IF-REST-001（原样引用输入接口）",
      "requirement_id": "REQ-001（该接口关联的需求，映射表给出时必填）",
      "category": "normal | boundary | exception",
      "title": "用例标题（一句话，含场景）",
      "params": {"参数名": "参数值或边界值"},
      "auth_mode": "inherit | missing | invalid",
      "expected": "status=200；响应体包含非空 token 与 username"
    }
  ]
}
```

字段规则：
- **每个输入接口至少 3 条用例：normal / boundary / exception 三类各 ≥1**（F-GEN-01）；
- `params` 的**参数名只能是该接口定义中的参数名**（F-GEN-02）——出现接口定义之外的参数名会被程序丢弃并要求重生成；
- 输入中的 `testpoints` 是系统从需求与评审结果派生的测试设计依据。每条用例应覆盖相应测试点的业务场景与拆分提示；不要把测试点标题机械改写成接口参数校验。`source=RISK` 的点必须至少覆盖一个风险/异常分支；
- 边界值口径（R-GEN-04）：数值取 0 / 负数 / 极值；字符串取空串 / 超长（>1024）；类型为 `unknown` 的参数按 `null` 处理；
- `expected` 必须以机器可判定状态码开头：成功使用 `status=200`、`status=201` 等精确值；异常/边界场景若允许任意同类状态码，使用 `status_class=4xx` 或 `status_class=5xx`。后续可写 `字段=值`、`token=非空` 等断言语义；禁止只写“HTTP 4xx”“返回正确”等自然语言状态描述。
- `auth_mode` 仅用于需要认证的接口：正常已登录场景用 `inherit`；验证未带认证时用 `missing`；验证伪造/过期 token 时用 `invalid`。不涉及认证时省略该字段；
- 输入可能含 `batch`；只为当前 `interfaces` 批次生成用例。溯源（F-GEN-04）：`interface_id` / `requirement_id` 只能引用输入中存在的 ID，**禁止虚构**；
- 只输出 `cases` 数组——`id`（TC-###）/ `script_path` / `loadable` / `low_confidence` 由系统生成或回填，你**不得**输出这些字段；
- 类型为 `unknown` 的参数无需回避：照常生成用例，系统会标注低置信度（X-GEN-03）；
- **鉴权凭据（C-17）**：输入可含 `auth`（login_url/username/password）。登录/鉴权类接口的 `normal` 用例参数**必须使用该真实凭据**（如 username/password 字段填 auth 提供的值），`exception` 用例用错误凭据（错误密码/不存在用户）预期 4xx；无 `auth` 输入时照常用需求条目语义设计，**禁止虚构凭据**。

## 任务

你是测试用例设计师。用户消息包含接口还原清单（id/protocol/signature/params/returns）、需求条目（id/title/description）、需求-接口映射表与系统派生的 `testpoints`（含 REQ/RISK 溯源、功能场景、拆分提示）；可能另含 `auth` 鉴权凭据（C-17，按上述字段规则使用）。请先以测试点定义用户/业务场景，再按接口逐个设计可执行用例：正常路径验证主流程，边界用例按口径取值，异常用例覆盖非法参数、权限和业务拒绝；`expected` 的断言语义尽量引用需求中的用户可见结果与数据结果。
