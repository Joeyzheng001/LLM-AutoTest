---
kind: frontend_style
name: 前端样式体系：Vue 3 + Vite 占位工程，尚未引入 UI 框架与样式方案
category: frontend_style
scope:
    - '**'
source_files:
    - services/frontend/package.json
    - services/frontend/vite.config.ts
    - services/frontend/eslint.config.js
    - services/frontend/.prettierrc.json
    - services/frontend/src/App.vue
    - services/frontend/src/main.ts
---

## 1. 系统/技术栈
- 框架：Vue 3（`vue@^3.5.13`），单文件组件 `.vue`，使用 `<script setup lang="ts">` 语法。
- 构建工具：Vite 6（`vite.config.ts` 仅启用 `@vitejs/plugin-vue`，并配置 `/api` 代理到 `http://localhost:8000`）。
- 语言：TypeScript（`typescript~5.6.3`，`vue-tsc` 参与类型检查）。
- 包管理器：pnpm 9.15.0（`packageManager` 字段锁定）。
- 代码质量：ESLint flat config（`@eslint/js` + `eslint-plugin-vue` 的 `flat/recommended`），Prettier（`.prettierrc.json`：无分号、单引号、行宽 100）。

## 2. 关键文件
- `services/frontend/package.json`：声明依赖与脚本（`dev/build/lint/test`）。
- `services/frontend/vite.config.ts`：Vite 配置，仅含 Vue 插件与开发代理。
- `services/frontend/eslint.config.js`：ESLint flat 规则，忽略 `dist/`。
- `services/frontend/.prettierrc.json`：统一格式化约定。
- `services/frontend/src/App.vue`：唯一业务组件，当前为占位文本。
- `services/frontend/src/main.ts`：最小入口，挂载 `#app`。
- `services/frontend/index.html`：Vite 默认模板（由 Vite 提供）。

## 3. 架构与约定
- **尚无实际样式实现**：整个 `src/` 目录仅包含 `App.vue`、`main.ts`、`vite-env.d.ts`，没有任何 `.css` / `.scss` / `.less` / `.module.css` 文件，也没有引入任何 UI 组件库或 CSS-in-JS 方案。`App.vue` 中仅渲染一个 `<h1>` 占位文案。
- **样式策略未定义**：仓库内不存在 `tailwind.config.*`、`postcss.config.*`、设计 tokens、主题变量等样式基础设施；样式方案在 V2.1 范围裁决后接入（见 `App.vue` 注释“六页业务实现按 M1–M5 排期接入”）。
- **后端数据口径约束**：`App.vue` 顶部注释引用 `.rules/frontend-code-style.md`，规定“数值一律来自后端 API，前端不做计算”，该约定属于前端逻辑风格，对后续样式层的数据绑定方式有间接影响。
- **构建产物**：`build` 脚本先执行 `vue-tsc -b` 再 `vite build`，输出到默认 `dist/`（被 ESLint ignore）。

## 4. 约定与约束
- **格式约定**：通过 Prettier 强制无分号、单引号、100 列行宽；ESLint 使用 Vue 官方推荐规则集。
- **开发代理**：所有 `/api/*` 请求在 dev 模式代理至 `http://localhost:8000`（orchestrator 服务），路径待 `schemas/contracts/` 契约落地后调整（见 `vite.config.ts` 注释）。
- **样式相关约束**：当前阶段不要求编写样式——前端处于“已批准进入范围但页面待实现”的占位状态，样式方案将在后续迭代中选定并落地。
- **全局规范引用**：前端代码风格受根目录 `.rules/frontend-code-style.md` 约束（如数据来源红线），但样式层面的具体规范尚未在该文件中体现（本仓库未发现针对 CSS/SCSS/Tailwind 的规则条目）。

综上，该仓库的前端样式体系目前仅为“空壳工程”：Vue 3 + Vite + TypeScript + ESLint/Prettier 的工具链已就绪，但尚未引入任何 UI 组件库、CSS 方法论或设计令牌，样式实现待后续版本接入。