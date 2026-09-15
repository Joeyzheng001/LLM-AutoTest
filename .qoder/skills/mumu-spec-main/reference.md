# MumuSpec 十四文档 — 参考表

## 编号速查

| 编号 | 阶段 | 文件名模式（投研样例） |
|------|------|------------------------|
| 01 | Meta | `01-Spec写作总则与文档编号索引.md` |
| 02 | Meta | `02-需求来源与采集记录.md` |
| 03 | Proposal | `03-立项提案与范围说明.md` |
| 04 | Proposal | `04-产品需求说明.md` |
| 05 | Spec | `05-用户故事与验收标准.md` |
| 06 | Spec | `06-功能规格说明.md` |
| 07 | Spec | `07-非功能需求与约束.md` |
| 08 | Design | `08-系统架构与技术选型.md` |
| 09 | Design | `09-API接口规格.md`（契约真源） |
| 10 | Design | `10-数据模型与存储规格.md` |
| 11 | Design | `11-安全设计规格.md` |
| 12 | Plan | `12-实施计划与里程碑.md` |
| 13 | Test | `13-测试策略与质量门禁.md` |
| 14 | Trace | `14-需求追踪矩阵.md` |

根目录：`MumuSpec/`（兼容旧路径 `Spec模板/`）。

## OpenSpec ↔ 01–14（摘要）

| OpenSpec | 01–14 |
|----------|--------|
| `changes/*/proposal.md` | 03 |
| `specs/` 用户故事聚合 | 05、06 |
| `design.md` | 08、10 |
| API 段落 | 09（须指回同一版本） |
| `tasks.md` | 12 |
| 测试/CI 引用 | 13 |
| `archive` 留痕 | 14 + Git |

如项目启用 OpenSpec，另行约定 changes ↔ 01–14 的映射记录位置。

## 已实现的 6 个 Skill

见仓库 `.qoder/skills/` 下子目录。已实现：

- `mumu-spec-r1-proposal` · `mumu-spec-r2-align` · `mumu-spec-r3-design` · `mumu-spec-r4-finalize` · `mumu-spec-sync` · `mumu-spec-tasks` · `mumu-spec-check` · `mumu-spec-status`

## 维护成本说明

- **Git**：版本与 diff（替代部分 OpenSpec 历史能力）。  
- **Skill**：生成与跨文档一致性（OpenSpec CLI 不自动写 14 篇正文）。  
- **manifest / console**：结构展示；改节点关系时同步 `spec-manifest.json` 并运行 `python3 tools/spec-embed-manifest.py`（若改 HTML 嵌入）。
