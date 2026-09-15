---
name: mumu-spec-main
description: >-
  Routes MumuSpec work across 01–14: picks the right round skill (R1–R4)
  or sync skill, enforces single source of truth in MumuSpec/*.md.
  Use when creating or editing Spec 01–14 docs, splitting requirements from
  meeting notes, calibrating user stories against API contracts, or
  coordinating OpenSpec changes with the 14-document suite.
---

# Spec Coding 十四文档 — 总控

## 使用流程

1. **识别场景**：用户给素材/需求/已改某篇
2. **选轮次**：查下表 → 决定 R1/R2/R3/R4/sync
3. **转交子 Skill**：本 Skill 只做路由，不直接写 01–14

## 用哪个 Skill

| 场景 | 调用 |
|------|------|
| 新项目首次创建 01-14 骨架 | **@mumu-spec-init** |
| 整理会议纪要/邮件，或写立项、PRD | **@mumu-spec-r1-proposal** |
| 写用户故事并与 API 对齐 | **@mumu-spec-r2-align** |
| 写架构、数据、功能规格、NFR、安全 | **@mumu-spec-r3-design** |
| 写计划、测试、追溯矩阵终检 | **@mumu-spec-r4-finalize** |
| 已改某一编号，要列联动并改其它篇 | **@mumu-spec-sync** |
| 不确定从哪开始 / 全流程 | 本 Skill 定轮次后 **转交** 上表对应 Skill |

## 不可违反的全局规则

1. **真源目录**：`MumuSpec/`（兼容 `Spec模板/`）下 `01`～`14` 的 `.md`。  
2. **写作顺序**：R1→R2→R3→R4（见 `01` 第十一节），**不要**按编号 01→14 顺序硬写。  
3. **契约真源**：对外字段以 **`09`**、存储以 **`10`** 为准。  
4. **OpenSpec**：只作变更壳；合并结果回写 01～14（如项目启用 OpenSpec，需另行约定映射）。

## 四轮一览

| 轮次 | Skill | 文档 |
|------|-------|------|
| R1 | mumu-spec-r1-proposal | `02`（可选）→ `03` → `04` |
| R2 | mumu-spec-r2-align | `05` ↔ `09` |
| R3 | mumu-spec-r3-design | `08` + `10` → `06` + `07` + `11` |
| R4 | mumu-spec-r4-finalize | `12` + `13` → `14` |

`01`：团队首次建库时人工定稿，一般不由轮次 Skill 批量生成。

## 文档侧门禁（R4 前自检）

- P0 US 在 `14` 有行；每条 REQ 在 `13` 有 TC；`09` 有示例 JSON；`10` 有约束。  
- 不满足 → 状态保持 **Draft**。

## 详细速查

[reference.md](reference.md)
