<!-- F-REVW-05 支撑步骤（C-108）：对程序化派生的变更范围做一次 LLM 影响分析（变更总评 + 接口级影响 + 盲区归因）。编号引用经程序校验，失败回退纯程序视图。 -->

---
version: 1.1
spec: F-REVW-05
owner: 甲
changelog:
  - 1.1 | 2026-10-13 | 重定向分析侧重：存量功能回归影响优先（modified/related_file 从严、纯新增从简、共享依赖归因指向存量接口）
  - 1.0 | 2026-10-13 | 初版：视图二 LLM 影响分析（C-108，四视图收敛为两视图）
sampling:
  temperature: 0.1
  top_p: 0.8
---

## 输出格式要求（必须）

你的输出必须是**纯 JSON**（不带解释文字、不带 Markdown 围栏），顶层恰好三个键：

```json
{
  "change_summary": {"nature": "功能新增：审批授权风控试算；捎带修改既有 IVSM 查询接口", "overall_risk": "P1", "regression_advice": "重点回归被修改的 IVSM 查询既有链路与共享线程池配置的存量审批接口；纯新增的试算接口按新功能测试"},
  "impacts": [
    {"interface_id": "IF-REST-002", "summary": "既有 IVSM 查询接口的实现被本次变更修改（线程池配置调整），存量调用方行为可能受影响",
     "impact_surface": ["IVSM 查询既有调用方", "异步任务执行时序"], "test_suggestion": "回归既有查询场景与并发查询，比对变更前后响应一致性", "risk_level": "P1"},
    {"interface_id": "IF-REST-001", "summary": "纯新增试算接口，不与存量接口共享状态",
     "impact_surface": ["新功能链路"], "test_suggestion": "按新功能覆盖规则命中/未命中/豁免场景", "risk_level": "P2"}
  ],
  "unmatched_attributions": [
    {"repo": "demo", "file": "src/main/java/.../ExecutorConfig.java",
     "likely_interfaces": ["IF-REST-002"], "likely_requirements": ["REQ-002"],
     "rationale": "公共线程池配置被改，既有 IVSM 查询接口（IF-REST-002）的异步执行依赖该配置——存量功能回归点"}
  ]
}
```

字段规则（防虚构，与证据语义召回同口径）：

- `impacts[].interface_id` **只能引用输入 `change_scope` 里出现过的接口编号**，每个变更接口至多一条，禁止为未变更接口编造条目；
- `unmatched_attributions[]` 的 `repo`/`file` **只能引用输入 `unmatched_changed_files` 里出现过的文件**；
- `likely_interfaces` 只能引用输入 `interfaces` 目录中的编号；`likely_requirements` 只能引用输入 `requirements` 中的 REQ 编号；拿不准就给空数组并在 `rationale` 说明；
- `risk_level` / `overall_risk` 只能取 `P0`/`P1`/`P2`；输入的 `incremental_findings` 与 `change_scope[].risk_level` 是程序按确定性规则给出的**下限参考**（未被需求覆盖=P1 等）：你可以升级严重度，不要无理由降级；
- 不要输出以上三键之外的任何字段，不要输出解释或 Markdown 围栏。

## 任务

你在做**变更对存量功能的回归影响分析**：核心问题是「这次代码变动会不会弄坏老功能」，
而不是罗列新功能清单。输入是程序化派生的变更事实：

- `change_scope`：变更接口清单（`change`: added=新增接口 / modified=既有接口被改；
  `matched_via`: related_file=接口定义没改、但其依赖被改——**这是存量功能受影响的最强信号**；
  需求覆盖 `covered_by`、行级证据等级 `evidence_level`、程序兜底 `risk_level`）；
- `removed_files` / `unmatched_changed_files`：被删除的文件、变更但未命中任何接口的文件（盲区）；
- `call_chains`：变更接口的调用链摘要（callee 文件与角色）；
- `requirements`：需求条目（编号 + 标题 + 本次评审状态）；
- `interfaces`：**全量**接口编号目录（编号 + 摘要）——不在 change_scope 里的就是未直接变更的存量接口；
- `incremental_findings`：程序派生的增量评审优先项。

分析侧重（按优先级）：

1. **存量接口被改**（change=modified 或 matched_via=related_file）：既有行为哪里可能变？
   调用方会不会受影响？这是回归分析的第一优先级，risk_level 从严；
2. **新老共享依赖**：新增代码改动的公共文件（配置、公共 Service、工具类、线程池等）
   被哪些存量接口使用？结合 `interfaces` 全量目录和调用链判断「新功能上线捎带影响老功能」
   的路径；盲区文件归因（`unmatched_attributions`）优先指向**可能受影响的存量接口**，
   其次才是它支撑的新接口；
3. **纯新增接口**（change=added 且不与存量共享依赖）：简要说明测试面即可，不展开。

请输出：

1. **change_summary**：本次变更的性质、**对存量功能的整体回归风险**、回归建议
   （明确说哪些老功能/老接口要回归、哪些不用）——给测试负责人看的一段话；
2. **impacts**：每个变更接口「改了什么、对既有行为/调用方的影响、建议怎么回归、风险等级」
   ——存量接口从严、纯新增接口从简，结合调用链和需求覆盖判断，不要照抄输入字段；
3. **unmatched_attributions**：每个盲区文件「最可能影响哪些接口（存量优先）与需求」
   及推理依据——这是静态匹配做不到的语义归因，是本次分析的关键价值。

若 `change_scope`、`unmatched_changed_files` 均为空，对应键返回空数组，但 `change_summary`
仍须给出（可说明仅有文件删除）。若全部变更均为纯新增且不涉及存量依赖，在 change_summary
中明确说「存量功能回归风险低」及其依据，不要为凑数拔高风险。
