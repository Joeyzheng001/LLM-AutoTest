---
trigger: glob
glob:
  - "services/frontend/**/*.vue"
  - "services/frontend/**/*.ts"
  - "services/frontend/**/*.js"
  - "services/frontend/**/package.json"
  - "services/frontend/**/*.css"
  - "services/frontend/**/*.scss"
---

# 前端代码统一规范（frontend，Vue 3）

## 项目基线与目录职责

- 本项目前端基线为 **Vue 3 组合式 API + `<script setup>` + TypeScript**，包管理使用 **pnpm**（锁定 `pnpm-lock.yaml`）；Node 20 LTS。不得引入 jQuery、其他前端框架或无需求的新依赖。
- 目录职责：`src/api/` 收敛全部后端调用，`src/stores/` 为 Pinia store（按页面域拆分），`src/components/` 为公共组件，`src/views/` 为页面组件（按 07 六页命名对齐，如 `TaskConfig.vue`、`PipelineDashboard.vue`），`src/types/` 为契约类型定义。
- 修改前检查相邻组件、路由与现有测试，只做需求相关的最小改动；不得把领域逻辑堆进单个巨型组件或巨型 store。
- 组件命名 `PascalCase`；禁止内联事件处理器和重复 DOM ID。
- 禁止提交密钥、令牌或真实个人数据；浏览器存储只保存必要的非敏感数据。

## 展示口径红线

- 界面呈现的度量数值（自动化率、通过率、接口数、缺陷数等）**一律来自后端 API 返回的程序化汇总结果，前端不做任何数值计算、估算或「补全」**（对应 F-RPT-02：数字不经 LLM，也不经前端）。
- 降级节点、兜底仓库结果必须按后端返回的标注字段如实分列展示，禁止前端合并或隐藏（D4 / D5 口径）。
- 不要吞掉 API 错误或用成功提示掩盖失败；异步操作必须呈现 loading/success/error 状态并防止重复提交。

## 模板与可访问性

- 优先使用语义化元素和正确的标题层级。交互操作使用 `button` 或 `a`，不要用普通 `div` 模拟按钮。
- 表单控件必须有关联的 `label`；图像必须有有意义的 `alt`，纯装饰图像使用空 `alt`。
- 键盘可以完成所有核心操作；自定义交互必须处理焦点、Escape、Enter/Space，并提供必要的 ARIA 状态。
- 动态提示、错误和加载状态应对辅助技术可感知；不得只用颜色表达状态。外链新窗口必须使用 `rel="noopener"`。

## TypeScript 与逻辑

- 默认使用 `const`，需要重新赋值时使用 `let`；使用严格相等、模板字符串、可选链和 `async/await`。
- 函数和变量使用 `camelCase`，组件与类型使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`；事件处理函数以行为命名，如 `handleSaveClick`。
- 网络请求复用 `src/api/` 统一层（含认证头与统一错误展示），不得在组件内另建不一致的 fetch 封装。
- 后端契约类型定义于 `src/types/` 并与 `schemas/contracts/` 对齐；字段名、错误码和状态含义必须与后端一致，使用统一错误结构 `{code, message, node_id?, details}`。
- 不直接把不可信内容写入 `v-html` / `innerHTML`；确需渲染富文本必须经明确的安全过滤。
- 轮询类逻辑（流水线状态监控）必须在页面离开或状态失效后停止，避免泄漏。

## 样式

- 类名使用清晰、稳定的 kebab-case，避免依赖深层 DOM 结构、标签选择器和 `!important`；新增样式不得污染无关页面。
- 优先复用现有颜色、间距、圆角、阴影和字体变量（设计令牌）；同类值重复出现时再抽取变量，不创建一次性令牌。
- 使用响应式布局，至少检查窄屏和桌面宽度；避免固定高度截断内容，长文本必须可换行。
- 保留清晰的 hover、focus-visible、disabled、loading 和 error 状态；动画尊重 `prefers-reduced-motion`。

## 构建与验证

- 修改源文件后运行：`pnpm --dir services/frontend lint` 与 `pnpm --dir services/frontend build`；涉及交互逻辑时运行 `pnpm --dir services/frontend test`（vitest）。
- 完成前检查：控制台无新增错误、主要交互可用、键盘焦点合理、窄屏不溢出、lint 0 error。
