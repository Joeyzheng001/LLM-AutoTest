---
name: mumu-spec-r4-finalize
description: >-
  MumuSpec R4 (finalize): writes 12 plan/milestones, 13 test strategy and TCs, and 14
  requirements traceability matrix. Use for release readiness, quality gates,
  or RTM reconciliation before marking Spec docs Approved.
---

# R4 — 收口（12 + 13 → 14）

## 职责

- **`12`**：里程碑、WBS、依赖；P0 能力对齐 `03` Phase 1。  
- **`13`**：测试分层（L1～L4）、`TC-*`、门禁（G-LINT / G-UNIT / G-INT 等）；每条 TC 指回 `05` REQ 与 `09` 可断言字段。  
- **`14`**：四向追溯（REQ ↔ US ↔ API/Rule ↔ TC）；发布前终检表。

## 输入

- R1～R3 文档至少 Draft 完整（尤其 `05`、`09`、`10`）。  
- ID 前缀与 `05`/`13` 一致。

## 步骤

1. **`12`**：按 `05` 的 P0/P1 排期；不写实现代码。  
2. **`13`**：为每个 P0 REQ 至少 1 个 TC；分层标注；**禁止**不可自动化模糊断言。  
3. **`14`**：逐行填满矩阵；检查无孤立的 REQ、US、TC。  
4. 执行 `01` 第七节门禁清单；全部通过才建议将相关文档标 **Approved**。  
5. 若用户用 OpenSpec：提示 `openspec archive` 后把变更摘要写入 `14` 或各篇变更记录。

## 终检清单（必须输出给用户）

- [ ] 每条 P0 US 在 `14` 有行  
- [ ] 每条 REQ 在 `13` 有 ≥1 TC  
- [ ] `09` 示例请求/响应/错误齐全  
- [ ] `10` 索引与约束齐全  
- [ ] `03` Phase 1 与 `05` P0 范围一致  

## 完成后

大改动走 **@mumu-spec-sync**；可选更新 `spec-manifest.json` 节点状态（仅当用户要求同步画布）。
