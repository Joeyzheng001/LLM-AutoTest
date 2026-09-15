---
name: mumu-spec-sync
description: >-
  After editing one MumuSpec doc (01–14), lists downstream docs to update
  and applies consistent REQ/US/API/TC/RTM IDs. Use when scope, API fields,
  acceptance criteria, or tests changed; says which files to sync in order.
---

# 变更联动（维护 01～14 一致性）

## 职责

用户已改（或即将改）**某一编号**时：

1. **判定变更类型**（行为 / 契约 / 测试 / 范围 / 计划）。  
2. **列出必须联动的其它编号**及顺序。  
3. **按序修改**或给出逐文件修改提纲（用户确认后执行）。  
4. 更新受影响文档的 **变更记录** 与版本号。

## 同步顺序（来自 `01` 第六节）

| 变更类型 | 先改 | 再改 |
|----------|------|------|
| 行为、AC、US | `05`（`06` 若已有细节） | `09`/`10` 若契约变 → `13` → `14` |
| API 字段、path、错误码 | `09` | `10` → `05` AC → `13` TC → `14` |
| 表结构、存储 | `10` | `09` → `13` → `14` |
| 仅测试/门禁 | `13` | `14` |
| 范围增删 | `03` → `04` | `05` → 下游全套 |
| 排期 | `12` | 通常不动契约 |

## 工作流

1. 问清：**改了哪一份、哪一节、什么 ID**（若用户未说明，从 git diff 或当前文件推断）。  
2. 输出 **联动清单**（表格：编号 | 动作 | 原因），等用户确认或默认继续。  
3. 逐文件修改，**不**引入第二套 REQ 前缀。  
4. 最后输出 **终检 mini 清单**（同 R4 四条门禁）。  

## 与 OpenSpec

- 若变更来自 `openspec/changes/*`，合并后仍以 `MumuSpec/*.md`（兼容 `Spec模板/`）为准，并删除或标注已过时的冲突段落。  

## 不做什么

- 不替 R1～R4 **从零新建**整套（应转对应 round Skill）。  
- 不修改 `MumuSpec/`（或 `Spec模板/`）之外的文件，除非用户明确要求。
