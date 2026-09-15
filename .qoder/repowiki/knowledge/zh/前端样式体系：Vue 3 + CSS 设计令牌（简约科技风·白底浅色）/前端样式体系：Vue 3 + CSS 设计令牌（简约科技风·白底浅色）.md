---
kind: frontend_style
name: 前端样式体系：Vue 3 + CSS 设计令牌（简约科技风·白底浅色）
category: frontend_style
scope:
    - '**'
source_files:
    - services/frontend/src/styles/tokens.css
    - services/frontend/src/App.vue
    - services/frontend/src/main.ts
    - services/frontend/package.json
    - services/frontend/vite.config.ts
    - services/frontend/eslint.config.js
    - services/frontend/.prettierrc.json
    - docs/07_UI演示设计.md
---

## 1. 系统/技术栈

- **框架与构建**：Vue 3 (`vue@^3.5.13`) + Vite (`vite@^6.0.5`)，通过 `@vitejs/plugin-vue` 集成；TypeScript 由 `vue-tsc` 在 build 阶段校验。
- **样式方案**：纯 CSS（无 Sass/Less），采用 **CSS 自定义属性（design tokens）** 集中管理主题色、排版、间距、圆角、阴影等视觉变量，位于 `services/frontend/src/styles/tokens.css`。
- **代码风格工具**：ESLint 9（flat config，启用 `@eslint/js`、`typescript-eslint`、`eslint-plugin-vue` 的 flat/recommended 配置）+ Prettier（`.prettierrc.json`：`semi: false`、`singleQuote: true`、`printWidth: 100`）。ESLint 中显式关闭与 Prettier 冲突的 Vue 格式化规则（`vue/max-attributes-per-line`、`vue/singleline-html-element-content-newline`、`vue/html-self-closing`），将格式统一交由 Prettier 处理。
- **包管理器**：pnpm (`packageManager: pnpm@9.15.0`)。

## 2. 关键文件

- `services/frontend/src/styles/tokens.css` — 唯一事实源的设计令牌文件，定义所有颜色、字体、字号、间距、圆角、阴影及全局重置样式。
- `services/frontend/src/main.ts` — 应用入口，挂载 App 并引入 `tokens.css`。
- `services/frontend/src/App.vue` — 应用外壳（左侧六页导航 + 顶栏），使用 scoped `<style>` 并通过 `var(--*)` 引用令牌。
- `services/frontend/package.json` — 依赖声明与脚本（`dev`/`build`/`lint`/`test`）。
- `services/frontend/vite.config.ts` — Vite 配置，仅启用 Vue 插件并将 `/api` 代理到 `http://localhost:8000`（orchestrator）。
- `services/frontend/eslint.config.js` — ESLint flat 配置，TS + Vue 推荐规则集。
- `services/frontend/.prettierrc.json` — Prettier 格式化约定。
- `docs/07_UI演示设计.md` — 文档化视觉基线，明确“以 `tokens.css` 为唯一事实源”，并约束配色原则（白底低噪声 + 单一强调色 + 细线分隔 + 等宽数字）。

## 3. 架构与约定

### 3.1 设计令牌（Design Tokens）

`tokens.css` 通过 `:root` 下的 CSS 变量组织令牌，分为以下语义分组：

| 分组 | 示例令牌 | 用途 |
|---|---|---|
| 背景层级 | `--bg`、`--bg-elev`、`--surface`、`--surface-2` | 页面底 → 面板 → 卡片，均为浅色 |
| 边框 | `--border`、`--border-strong` | 细线、低对比度 |
| 文本层级 | `--text-1`、`--text-2`、`--text-3` | 主文 / 次文 / 辅助文 |
| 强调色 | `--accent`、`--accent-strong`、`--accent-dim`、`--accent-glow` | 唯一主色：科技蓝 `#2f7ff5` |
| 语义状态色 | `--success`、`--warning`、`--danger` 及其 dim 变体 | 成功/告警/危险，仅用于语义 |
| 排版 | `--font-sans`、`--font-mono`、`--text-xs` ~ `--text-xl` | 字体族与字号 |
| 几何 | `--radius-sm/md/lg`、`--space-1` ~ `--space-6` | 圆角与间距 |
| 阴影 | `--shadow-sm`、`--shadow-md` | 浅色主题用轻投影替代暗色辉光 |

全局 reset：`* { box-sizing: border-box; margin: 0; padding: 0; }`，`body` 设置背景、文字、字体、行高和抗锯齿。滚动条通过 `::-webkit-scrollbar` 定制为细窄样式。

### 3.2 组件级样式策略

- 组件样式写在 `.vue` 文件的 `<style scoped>` 块内（如 `App.vue`），通过 `var(--*)` 引用令牌，而非写死颜色或尺寸。
- 布局采用 CSS Grid（`display: grid; grid-template-columns: 216px 1fr`）实现侧边栏 + 主内容区。
- 交互态（hover、active）通过类名切换 + 令牌中的 `--accent-dim` / `--accent-glow` 实现微光效果。
- 数字与 ID 统一使用 `.mono` 类 + `--font-mono`，并开启 `tabular-nums` 保证对齐。

### 3.3 页面结构约定

- 应用外壳 `App.vue` 维护一个 `views` 数组（键/标签/组件/props），通过动态 `<component :is>` 切换视图，当前已实现“任务配置”“流水线总览”，其余四页占位。
- 顶部显示当前页面标题与 mock 数据标识，底部显示版本号与环境标记。
- 导航项使用小圆点指示器，激活态带发光效果（`box-shadow: 0 0 6px var(--accent-glow)`）。

### 3.4 响应式策略

当前未引入媒体查询或响应式断点，布局基于固定宽度侧边栏（216px）+ 自适应主区。文档中未定义移动端适配要求。

## 4. 约定与约束

- **视觉基线来源**：`docs/07_UI演示设计.md` 明确“实施期真实前端的视觉风格以 `services/frontend/src/styles/tokens.css` 为唯一事实源”，概念图仅作叙事参考，不约束配色。
- **令牌变更纪律**：`tokens.css` 注释写明“只许新增令牌；改值须同步 docs/07 §二之乙 视觉基线说明”。即禁止随意修改已有令牌值，新增需保持向后兼容。
- **强调色唯一性**：设计原则限定“单一强调色（科技蓝 `#2f7ff5`）”，状态色仅用于语义（成功/告警/危险），避免多主色。
- **背景风格**：白底低噪声（`#f5f7fa`）+ 白面板 + 轻投影，不使用暗色主题。
- **字体规范**：正文使用 `Inter` / `PingFang SC` / `Microsoft YaHei` 等无衬线字体；数字与 ID 强制使用等宽字体（JetBrains Mono 等）。
- **代码风格**：ESLint 与 Prettier 分工明确——ESLint 管语法/最佳实践，Prettier 管格式；Vue 模板中与 Prettier 冲突的规则被显式关闭。
- **API 代理约定**：开发时 `/api` 请求代理至 `http://localhost:8000`（orchestrator），契约待 `schemas/contracts/` 落地后调整。
- **Mock 数据标识**：当前页面顶栏显示“mock 数据”徽章，区分演示数据与真实后端数据。

## 5. 适用性判断

该仓库包含完整的前端子工程（`services/frontend`），具备明确的 CSS 设计令牌体系、Vue 组件样式约定、ESLint/Prettier 代码风格配置以及文档化的视觉基线，因此本类别完全适用。