---
name: mumu-spec-r1-proposal
description: >-
  MumuSpec R1 (proposal): elicits from 02, writes 03 proposal and 04 PRD.
  Use when creating project scope, Phase 1/2 boundaries, success criteria,
  or converting meeting notes into 03/04. Not for API or user stories.
---

# R1 — 需求采集与立项（02 → 03 → 04）

## 职责

- **`02`**：结构化会议纪要、邮件、访谈（S-01/S-02/S-03），标出来源与「下一步」。  
- **`03`**：Why、Phase 1 交付块、Phase 2 Backlog（占位）、成功标准、风险、立项前置。  
- **`04`**：产品场景、用户流程、功能列表；**不写** SQL、接口 path、像素级 UI。

## 输入

- 用户提供的纪要/邮件/口述，或已有 `02` 草稿。  
- 模块编号、模块名称（与全库一致，如 `M1-QA`）。

## 步骤

1. 读 `MumuSpec/01-Spec写作总则与文档编号索引.md` 第二节（不混层；兼容旧路径 `Spec模板/`）。  
2. 若有原始素材，先补全 **`02`**，每条需求带可追溯来源。  
3. 写 **`03`**：参照同目录 `03-立项提案与范围说明.md` 章节；**冻结 Phase 1**，Phase 2 仅列表。  
4. 写 **`04`**：场景与流程对齐 `03` 的「做/不做」；每条能力能对应到后续 `05` 故事来源字段。  
5. 文末更新变更记录；元数据表填模块编号/名称/版本/状态。

## 输出质量

- `03` 的「不做」至少 3 条，避免范围蔓延。  
- `04` 不出现 `09` 级接口细节或表字段名。  
- `03`→`04` 追溯字段一致（成功标准能落到后续 P0/P1）。

## 完成后

提示用户进入 **@mumu-spec-r2-align**（05 + 09）。若仅有模糊想法，先停在 R1，不要提前写 `09`。
