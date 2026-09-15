<script setup lang="ts">
// 应用外壳：左侧六页导航（07 方向）+ 顶栏；视图切换（无路由依赖，M1 接正式路由）。
import { computed, ref, type Component } from 'vue'
import PipelineOverviewView from './views/PipelineOverviewView.vue'
import PlaceholderView from './views/PlaceholderView.vue'
import TaskConfigView from './views/TaskConfigView.vue'

interface NavView {
  key: string
  label: string
  component: Component
  props?: Record<string, string>
}

const views: NavView[] = [
  { key: 'config', label: '任务配置', component: TaskConfigView },
  { key: 'overview', label: '流水线总览', component: PipelineOverviewView },
  {
    key: 'review',
    label: 'Code Review 三视图',
    component: PlaceholderView,
    props: { title: 'Code Review 三视图', spec: 'F-REVW-01~04' },
  },
  {
    key: 'report',
    label: '测试报告',
    component: PlaceholderView,
    props: { title: '测试报告', spec: 'F-RPT-01~03' },
  },
  {
    key: 'defects',
    label: '缺陷闭环',
    component: PlaceholderView,
    props: { title: '缺陷闭环', spec: 'F-DEF-01~04' },
  },
  {
    key: 'regression',
    label: '回归沉淀与度量',
    component: PlaceholderView,
    props: { title: '回归沉淀与度量', spec: 'F-REG-01~04 / F-MTR-01~03' },
  },
]

const activeKey = ref('overview')
const active = computed(() => views.find((view) => view.key === activeKey.value))
</script>

<template>
  <div class="shell">
    <!-- 侧边导航 -->
    <nav class="side">
      <header class="brand">
        <span class="brand-mark mono">LA</span>
        <span class="brand-name">LLM-AutoTest</span>
      </header>
      <ul class="nav-list">
        <li v-for="view in views" :key="view.key">
          <button
            type="button"
            class="nav-item"
            :class="{ active: view.key === activeKey }"
            @click="activeKey = view.key"
          >
            <span class="nav-dot" aria-hidden="true" />
            {{ view.label }}
          </button>
        </li>
      </ul>
      <footer class="side-foot">
        <span class="mono version">v0.1.0</span>
        <span class="env">dev</span>
      </footer>
    </nav>

    <!-- 主区 -->
    <div class="main">
      <header class="topbar">
        <span class="topbar-title">{{ active?.label }}</span>
        <span class="topbar-badge mono">mock 数据</span>
      </header>
      <main class="content">
        <component :is="active?.component" v-bind="active?.props" />
      </main>
    </div>
  </div>
</template>

<style scoped>
.shell {
  display: grid;
  grid-template-columns: 216px 1fr;
  height: 100%;
}

/* 侧边栏 */
.side {
  display: flex;
  flex-direction: column;
  background: var(--bg-elev);
  border-right: 1px solid var(--border);
}

.brand {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-4) var(--space-4);
  border-bottom: 1px solid var(--border);
}

.brand-mark {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--accent-dim);
  border: 1px solid var(--accent-glow);
  border-radius: var(--radius-sm);
  color: var(--accent-strong);
  font-size: var(--text-xs);
  font-weight: 600;
}

.brand-name {
  font-size: var(--text-md);
  font-weight: 600;
  letter-spacing: 0.02em;
}

.nav-list {
  list-style: none;
  padding: var(--space-3) var(--space-2);
  flex: 1;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  width: 100%;
  padding: var(--space-2) var(--space-3);
  margin-bottom: var(--space-1);
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  color: var(--text-2);
  font-size: var(--text-md);
  cursor: pointer;
  text-align: left;
  transition:
    background 0.15s,
    color 0.15s;
}

.nav-item:hover {
  background: var(--surface-2);
  color: var(--text-1);
}

.nav-item.active {
  background: var(--accent-dim);
  color: var(--accent-strong);
}

.nav-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--text-3);
}

.nav-item.active .nav-dot {
  background: var(--accent);
  box-shadow: 0 0 6px var(--accent-glow);
}

.side-foot {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--border);
  font-size: var(--text-xs);
  color: var(--text-3);
}

.env {
  padding: 1px var(--space-2);
  background: var(--warning-dim);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--warning);
}

/* 主区 */
.main {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-3) var(--space-5);
  border-bottom: 1px solid var(--border);
  background: var(--bg);
}

.topbar-title {
  font-size: var(--text-lg);
  font-weight: 600;
}

.topbar-badge {
  padding: 2px var(--space-2);
  background: var(--accent-dim);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-3);
  font-size: var(--text-xs);
}

.content {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-4);
}
</style>
