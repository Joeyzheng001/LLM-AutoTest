<script setup lang="ts">
// 应用外壳：左侧九页导航（07 §3.1–3.6 六屏 + C-10 三产物展示屏）+ 顶栏；视图切换（无路由依赖）。
// 全部页面接 orchestrator REST API（数值来自后端，前端不计算）。
// C-16：navigation 共享状态——总览页节点卡跳转携带 jobId 切页（目标页经 initialJobId prop 接收）。
import { computed, onMounted, ref, watch, type Component } from 'vue'
import CodeReviewView from './views/CodeReviewView.vue'
import CodeScanView from './views/CodeScanView.vue'
import DefectLoopView from './views/DefectLoopView.vue'
import PipelineOverviewView from './views/PipelineOverviewView.vue'
import RegressionMetricsView from './views/RegressionMetricsView.vue'
import RequirementsView from './views/RequirementsView.vue'
import TaskConfigView from './views/TaskConfigView.vue'
import TestCasesView from './views/TestCasesView.vue'
import TestReportView from './views/TestReportView.vue'
import {
  clearAccessContext,
  getAccessContext,
  getAccessMode,
  setAccessContext,
} from './api/client'
import { navigation } from './api/navigation'

interface NavView {
  key: string
  label: string
  component: Component
  section: '工作台' | '分析与设计' | '验证与交付'
  child?: boolean
}

const coreViews: NavView[] = [
  { key: 'config', label: '任务配置', component: TaskConfigView, section: '工作台' },
  { key: 'overview', label: '流水线总览', component: PipelineOverviewView, section: '工作台' },
  // C-10：三个产物独立展示屏（需求解析 / 源码扫描与接口还原 / 用例生成）
  { key: 'requirements', label: '需求解析', component: RequirementsView, section: '分析与设计' },
  { key: 'scan', label: '源码扫描与接口还原', component: CodeScanView, section: '分析与设计' },
  { key: 'review', label: 'Code Review', component: CodeReviewView, section: '分析与设计' },
  { key: 'cases', label: '测试用例', component: TestCasesView, section: '分析与设计' },
]

// E2E 是任务级可选子流程：只有当前查看的任务启用时才加入导航，且不改变 9 个核心节点。
const views = computed<NavView[]>(() => {
  const result = [...coreViews]
  const optional: NavView[] = []
  optional.push({
    key: 'interface-cases',
    label: '接口用例',
    component: TestCasesView,
    section: '分析与设计',
    child: true,
  })
  if (navigation.e2e?.generateCases) {
    optional.push({
      key: 'e2e-cases',
      label: 'E2E 用例',
      component: TestCasesView,
      section: '分析与设计',
      child: true,
    })
  }
  const validation: NavView[] = [
    { key: 'report', label: '测试报告', component: TestReportView, section: '验证与交付' as const },
    {
      key: 'interface-report',
      label: '接口测试报告',
      component: TestReportView,
      section: '验证与交付' as const,
      child: true,
    },
    { key: 'defects', label: '缺陷', component: DefectLoopView, section: '验证与交付' as const },
    {
      key: 'interface-defects',
      label: '接口缺陷',
      component: DefectLoopView,
      section: '验证与交付' as const,
      child: true,
    },
    {
      key: 'regression',
      label: '回归资产与度量',
      component: RegressionMetricsView,
      section: '验证与交付' as const,
    },
    {
      key: 'interface-regression',
      label: '接口回归资产',
      component: RegressionMetricsView,
      section: '验证与交付' as const,
      child: true,
    },
  ]
  if (navigation.e2e?.generateCases && navigation.e2e.executeCases) {
    validation.splice(2, 0, {
      key: 'e2e-execution',
      label: 'E2E 测试报告',
      component: TestReportView,
      section: '验证与交付',
      child: true,
    })
    validation.splice(5, 0, {
      key: 'e2e-defects',
      label: 'E2E 缺陷',
      component: DefectLoopView,
      section: '验证与交付',
      child: true,
    })
    validation.push({
      key: 'e2e-regression',
      label: 'E2E 回归资产',
      component: RegressionMetricsView,
      section: '验证与交付',
      child: true,
    })
  }
  result.push(...optional.filter((v) => v.section === '分析与设计'))
  result.push(...validation)
  return result
})
const navSections = computed(() =>
  (['工作台', '分析与设计', '验证与交付'] as const).map((title) => ({
    title,
    views: views.value.filter((view) => view.section === title),
  })),
)

const activeKey = ref('overview')
const active = computed(() => views.value.find((view) => view.key === activeKey.value))

// 部门共享访问上下文：只存当前浏览器会话，不进入 localStorage、任务配置或导出产物。
const initialAccess = getAccessContext()
const accessToken = ref(initialAccess.token)
const accessProjectId = ref(initialAccess.projectId)
const accessMessage = ref('')
const accessConfigured = ref(Boolean(initialAccess.token && initialAccess.projectId))
const accessMode = ref<'loading' | 'single_user' | 'token'>('loading')
const accessUser = ref('admin')

onMounted(async () => {
  try {
    const mode = await getAccessMode()
    accessMode.value = mode.mode
    accessUser.value = mode.user || 'admin'
    if (mode.mode === 'single_user') clearAccess()
  } catch {
    // 旧服务端不提供模式端点时，保留既有令牌入口，避免误把多用户部署降级为匿名。
    accessMode.value = 'token'
  }
})

function saveAccess(): void {
  try {
    setAccessContext({ token: accessToken.value, projectId: accessProjectId.value })
    accessConfigured.value = true
    accessMessage.value = '共享访问已连接'
    window.dispatchEvent(new Event('llm-autotest-access-changed'))
  } catch (error) {
    accessMessage.value = error instanceof Error ? error.message : '共享访问配置失败'
  }
}

function clearAccess(): void {
  clearAccessContext()
  accessToken.value = ''
  accessProjectId.value = ''
  accessConfigured.value = false
  accessMessage.value = '已清除共享访问'
  window.dispatchEvent(new Event('llm-autotest-access-changed'))
}

/** 左侧“任务配置”始终打开新建模式；只有总览的编辑按钮会携带任务进入编辑模式。 */
function selectView(key: string): void {
  if (key === 'config') navigation.jobId = ''
  activeKey.value = key
}

// C-16：总览页节点卡跳转（navigation.view 变化 → 切页；jobId 随 prop 下发）
watch(
  () => navigation.view,
  (view) => {
    if (view && views.value.some((v) => v.key === view)) activeKey.value = view
  },
)
watch(views, (next) => {
  if (!next.some((view) => view.key === activeKey.value)) activeKey.value = 'overview'
})
</script>

<template>
  <div class="shell">
    <!-- 侧边导航 -->
    <nav class="side">
      <header class="brand">
        <span class="brand-mark mono">LA</span>
        <span class="brand-name">LLM-AutoTest</span>
      </header>
      <div class="nav-list">
        <section v-for="section in navSections" :key="section.title" class="nav-section">
          <h2>{{ section.title }}</h2>
          <ul>
            <li v-for="view in section.views" :key="view.key">
              <button
                type="button"
                class="nav-item"
                :class="{ active: view.key === activeKey, child: view.child }"
                @click="selectView(view.key)"
              >
                <span class="nav-dot" aria-hidden="true" />
                {{ view.label }}
              </button>
            </li>
          </ul>
        </section>
      </div>
      <footer class="side-foot">
        <span class="mono version">v0.1.0</span>
        <span class="env">dev</span>
      </footer>
    </nav>

    <!-- 主区 -->
    <div class="main">
      <header class="topbar">
        <span class="topbar-title">{{ active?.label }}</span>
        <span class="topbar-badge mono">实时数据 · orchestrator :8000</span>
        <div
          v-if="accessMode === 'token'"
          class="access-bar"
          title="共享部署需要 Bearer Token 与项目标识；仅保存在当前浏览器会话"
        >
          <input
            v-model="accessProjectId"
            class="access-project"
            type="text"
            placeholder="项目"
            aria-label="共享项目标识"
          />
          <input
            v-model="accessToken"
            class="access-token"
            type="password"
            placeholder="访问令牌（可选）"
            aria-label="共享访问令牌"
          />
          <button type="button" class="access-btn" @click="saveAccess">
            {{ accessConfigured ? '更新' : '连接' }}
          </button>
          <button
            v-if="accessConfigured"
            type="button"
            class="access-btn muted"
            @click="clearAccess"
          >
            断开
          </button>
          <span v-if="accessMessage" class="access-message">{{ accessMessage }}</span>
        </div>
        <span v-else-if="accessMode === 'single_user'" class="single-user-badge">
          部门单用户 · {{ accessUser }}
        </span>
      </header>
      <main class="content">
        <component
          :is="active?.component"
          :initial-job-id="navigation.jobId"
          :view-key="active?.key"
        />
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
  background: linear-gradient(180deg, var(--bg-elev), var(--bg));
  border-right: 1px solid var(--border);
}

.brand {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-5) var(--space-4);
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
  padding: var(--space-4) var(--space-2);
  flex: 1;
  overflow-y: auto;
}

.nav-section {
  margin-bottom: var(--space-4);
}

.nav-section h2 {
  margin: 0 0 var(--space-2);
  padding: 0 var(--space-3);
  color: var(--text-3);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.12em;
}

.nav-section ul {
  margin: 0;
  padding: 0;
  list-style: none;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  width: 100%;
  padding: var(--space-2) var(--space-3);
  margin-bottom: 2px;
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

.nav-item.child {
  padding-left: calc(var(--space-3) + 18px);
  font-size: var(--text-sm);
  color: var(--text-3);
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

.access-bar {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-left: auto;
}

.access-bar input {
  height: 28px;
  padding: 0 var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-1);
  font-size: var(--text-xs);
}

.access-project {
  width: 92px;
}

.access-token {
  width: 150px;
}

.access-btn {
  height: 28px;
  padding: 0 var(--space-3);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  background: var(--accent-dim);
  color: var(--accent-strong);
  font-size: var(--text-xs);
  cursor: pointer;
}

.access-btn.muted {
  border-color: var(--border);
  background: transparent;
  color: var(--text-3);
}

.access-message {
  max-width: 150px;
  overflow: hidden;
  color: var(--text-3);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.single-user-badge {
  margin-left: auto;
  color: var(--text-3);
  font-size: var(--text-xs);
}

.content {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-4);
}

@media (max-width: 980px) {
  .topbar {
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .access-bar {
    width: 100%;
    margin-left: 0;
  }

  .access-token {
    flex: 1;
  }
}
</style>
