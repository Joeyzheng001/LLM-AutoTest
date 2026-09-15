---
kind: design
name: 将 mumu-spec Skills 落地为 .rules/ 下一 Skill 一 rule 的文档规范
source: session
category: adr
---

# 将 mumu-spec Skills 落地为 .rules/ 下一 Skill 一 rule 的文档规范

_来源：4b37aeb → 08effe3 提交周期内记录的编码计划——内容为规划时意图，实现可能滞后或有出入。_

**状态：** accepted

## 背景
项目需要将外部 mumu-spec Skills（SKILL.md）转化为可在本仓库中触发的规则，以约束 docs、MumuSpec、prompts、schemas 等目录下的 Spec 文档写作流程。

## 决策驱动
- 与现有 llm-integrity 和 09_开发规范说明 兼容
- 每个 Skill 独立可维护
- 模板与样例作为只读素材不干扰构建

## 备选方案
- **一 Skill 一 rule + 统一 frontmatter glob 触发** — 优点：职责清晰、便于增量维护；glob 覆盖所有相关目录；冲突时显式声明以 llm-integrity / 09 为准
- **合并为一个巨型 spec 规则文件** _（已否决）_ — 优点：减少文件数；缺点：难以定位单个 Skill 变更；违反「一 Skill 一 rule」约定
- **引入 OpenSpec CLI 或 spec-manifest.json 管理映射** _（已否决）_ — 优点：自动化程度更高；缺点：本次未启用，增加额外依赖与维护成本；仅保留一句「如启用则另行约定」

## 决策
在 .rules/ 下新增 10 个规则文件（spec-main、spec-init、spec-r1-proposal、spec-r2-align、spec-r3-design、spec-r4-finalize、spec-sync、spec-check、spec-status、spec-tasks），每个文件使用统一的 YAML frontmatter 将 trigger 设为 glob，匹配 docs/**、MumuSpec/**、Spec模板/**、prompts/**、schemas/**；冲突时以 llm-integrity 与 09_开发规范说明 为准。

## 影响
新增规则与现有四个 .rules 文件并存但不修改它们；Spec 文档写作顺序被固化为 R1→R4，契约真源锁定为 09 对外与 10 存储；后续如需自动化门禁需单独实现 CI 脚本。