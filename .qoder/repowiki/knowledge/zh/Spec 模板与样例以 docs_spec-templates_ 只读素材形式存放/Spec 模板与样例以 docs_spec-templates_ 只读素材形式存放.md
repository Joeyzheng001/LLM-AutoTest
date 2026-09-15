---
kind: design
name: Spec 模板与样例以 docs/spec-templates/ 只读素材形式存放
source: session
category: adr
---

# Spec 模板与样例以 docs/spec-templates/ 只读素材形式存放

_来源：4b37aeb → 08effe3 提交周期内记录的编码计划——内容为规划时意图，实现可能滞后或有出入。_

**状态：** accepted

## 背景
需要从 zip 导入空白模板与投研助手样例，供规则引用但不参与构建流程，同时处理 macOS 打包导致的 UTF-8 文件名截断问题。

## 决策驱动
- 模板仅作参考，不应影响构建产物
- 保留中文文件名便于阅读
- 修复乱码名以保证路径稳定

## 备选方案
- **docs/spec-templates/blank 与 example-投研助手 两套目录，规则中以相对路径引用** — 优点：与 .rules 解耦；不参与构建；可通过相对路径被规则引用
- **将模板放入 src 或 build 目录并纳入构建** _（已否决）_ — 优点：可被工具直接消费；缺点：增加构建复杂度；与「仅参考素材」意图不符

## 决策
在 docs/spec-templates/ 下分别放置 blank/（14 篇空白模板）与 example-投研助手/（14 篇样例及 README、PM-Spec 合并版、R4 交叉校验清单、03-PM 需求定义合并版），并在 .rules/spec-main.md 中以 ../../docs/spec-templates/... 相对路径引用；对 zip 内截断的文件名按映射表还原为完整中文名称。

## 影响
模板目录成为规则的只读素材来源；若未来需要构建期消费这些模板，需在 .gitignore 或构建配置中显式调整。