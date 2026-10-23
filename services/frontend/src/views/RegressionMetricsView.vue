<script setup lang="ts">
// 回归沉淀与度量页（07 §3.6）：左栏资产树（regression_index，按协议分组 R-REG-02 +
// 溯源 F-REG-02）+ 回归套件重跑（F-REG-03 复用全量复测端点）+ 度量报告
// （metrics.json：自动化率大数 + 分节点分解 + 瓶颈分析 F-MTR-03/04）。
// 红线：自动化率/分子分母全部来自后端 metrics 程序化汇总；data_complete=false → 如实显示「—」。
import { computed, ref, watch } from 'vue'
import JobPicker from '../components/JobPicker.vue'
import { getMetrics, getRegressionIndex, retestFull } from '../api/client'
import type { MetricsPayload, RegressionIndexPayload } from '../api/types'
import { NODE_LABELS } from '../api/labels'
import { navigation } from '../api/navigation'

const _jobId = ref('')

// C-16：总览页节点卡跳转携带 jobId（App.vue 动态组件下发）
const props = defineProps<{ initialJobId?: string; viewKey?: string }>()

const index = ref<RegressionIndexPayload | null>(null)
const metrics = ref<MetricsPayload | null>(null)
const loading = ref(false)
const error = ref('')

const rerunning = ref(false)
const rerunError = ref('')
const rerunQueued = ref(false)
const regressionType = computed<'api' | 'e2e' | null>(() => {
  if (props.viewKey === 'e2e-regression') return 'e2e'
  if (props.viewKey === 'interface-regression') return 'api'
  return null
})
const visibleSuites = computed(() => {
  if (!index.value) return []
  if (regressionType.value === 'e2e')
    return index.value.suites.filter((suite) => suite.protocol === 'e2e')
  if (regressionType.value === 'api')
    return index.value.suites.filter((suite) => suite.protocol !== 'e2e')
  return index.value.suites
})

async function load(): Promise<void> {
  if (!_jobId.value) return
  loading.value = true
  error.value = ''
  try {
    // 两个产物独立加载：回归索引可能早于 metrics 生成（或反之），分别降级不互相阻塞
    const errors: string[] = []
    try {
      index.value = await getRegressionIndex(_jobId.value)
    } catch (err) {
      index.value = null
      errors.push(err instanceof Error ? err.message : '回归索引加载失败')
    }
    try {
      metrics.value = await getMetrics(_jobId.value)
    } catch (err) {
      metrics.value = null
      errors.push(err instanceof Error ? err.message : '度量产物加载失败')
    }
    error.value = errors.join('；')
  } finally {
    loading.value = false
  }
}

// C-16：总览页节点卡跳转（immediate 同步赋值早于 watch(_jobId) 注册，须显式触发 load；
// 且须在 load/refs 之后——immediate 执行时函数体引用的 const 已初始化，免 TDZ）
watch(
  () => props.initialJobId,
  (v) => {
    if (v) {
      _jobId.value = v
      void load()
    }
  },
  { immediate: true },
)
watch(_jobId, (value) => {
  navigation.jobId = value
  void load()
})

async function rerunSuite(): Promise<void> {
  if (!_jobId.value || rerunning.value) return
  rerunning.value = true
  rerunError.value = ''
  rerunQueued.value = false
  try {
    await retestFull(_jobId.value)
    rerunQueued.value = true
  } catch (err) {
    rerunError.value = err instanceof Error ? err.message : '回归重跑请求失败'
  } finally {
    rerunning.value = false
  }
}

function formatRate(value: number | null): string {
  // 展示口径：后端已汇总的值仅做格式化（百分比）；null = 数据不完整（X-MTR-02）
  return value === null ? '—' : `${Math.round(value * 100)}%`
}

function nodeLabel(name: string): string {
  return NODE_LABELS[name] ?? name
}
</script>

<template>
  <div class="regression">
    <JobPicker v-model="_jobId" />

    <p v-if="loading" class="state-line">加载沉淀与度量产物中…</p>
    <p v-else-if="error" class="state-line state-error" role="alert">{{ error }}</p>
    <p v-else-if="!_jobId" class="state-line">请先选择任务。</p>

    <div class="view-title">
      {{
        regressionType === 'e2e'
          ? 'E2E 回归资产'
          : regressionType === 'api'
            ? '接口回归资产'
            : '回归资产与度量'
      }}
      <span v-if="regressionType" class="tag tag-accent">已按类型分开展示</span>
    </div>
    <div class="layout">
      <!-- 左栏：资产树（regression_index 为唯一事实源） -->
      <div class="card asset-card">
        <header class="card-title">
          回归资产树（R-REG-02 按协议组织 · 归档为移动非复制 R-REG-01）
        </header>
        <p v-if="!index || !visibleSuites.length" class="state-line">
          {{
            index?.empty_reason ??
            '暂无符合沉淀原则的资产：需存在执行通过或缺陷复测通过，且具备可加载脚本与需求溯源。'
          }}
        </p>
        <template v-else>
          <div v-for="suite in visibleSuites" :key="suite.protocol" class="suite">
            <header class="suite-head">
              <span class="tag tag-accent mono">{{ suite.protocol }}</span>
              <span class="suite-count">{{ suite.scripts.length }} 个脚本</span>
            </header>
            <ul class="script-list">
              <li v-for="script in suite.scripts" :key="script.case_id" class="script-item">
                <div class="script-line">
                  <span class="mono script-case">{{ script.case_id }}</span>
                  <span v-if="script.version" class="mono script-version"
                    >v{{ script.version }}</span
                  >
                </div>
                <div class="script-trace mono">
                  {{ script.requirement_id }} → {{ script.interface_id }}
                </div>
                <div class="script-path mono">{{ script.script_path }}</div>
              </li>
            </ul>
          </div>
          <div
            v-for="drift in index.drift_warnings ?? []"
            :key="drift.case_id"
            class="hint hint-warning"
          >
            回归漂移 · {{ drift.case_id }}：{{ drift.detail }}（交人工判断，不自动删除 · X-REG-02）
          </div>
        </template>
      </div>

      <!-- 右栏：回归套件 + 度量报告 -->
      <div class="right-col">
        <div class="card">
          <header class="card-title">回归套件（F-REG-03 一键重跑全量）</header>
          <div class="action-row">
            <button
              class="primary-btn"
              type="button"
              :disabled="!_jobId || rerunning"
              @click="rerunSuite"
            >
              {{ rerunning ? '提交中…' : '重跑回归套件' }}
            </button>
            <span v-if="rerunQueued" class="action-result" role="status">
              已入队（后台重跑回归沉淀节点，进度见「流水线总览」）
            </span>
          </div>
          <p v-if="rerunError" class="state-line state-error" role="alert">{{ rerunError }}</p>
        </div>

        <div v-if="metrics" class="card metrics-card">
          <header class="card-title">度量报告（数值程序化汇总，不经 LLM · F-MTR-03）</header>

          <!-- 自动化率大数 -->
          <div class="rate-block">
            <span class="rate-value mono">{{ formatRate(metrics.automation_rate.value) }}</span>
            <span class="rate-label">
              自动化率 ·
              <span class="mono"
                >{{ metrics.automation_rate.numerator }}/{{
                  metrics.automation_rate.denominator
                }}</span
              >
              （分母固定 9 不缩减 · X-MTR-01）
            </span>
          </div>
          <div v-if="metrics.automation_rate.value === null" class="hint hint-warning">
            数据不完整，不输出误导数值（X-MTR-02）
          </div>

          <!-- 分节点分解 + 瓶颈分析（F-MTR-04：介入原因 + 可自动化路径建议） -->
          <table class="metrics-table">
            <thead>
              <tr>
                <th scope="col">节点</th>
                <th scope="col">状态</th>
                <th scope="col">介入原因 / 降级原因</th>
                <th scope="col">可自动化路径</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="node in metrics.nodes" :key="node.name">
                <td>{{ nodeLabel(node.name) }}</td>
                <td>
                  <span class="tag" :data-status="node.status">
                    {{ node.status === 'completed' ? '已完成' : '人工介入' }}
                  </span>
                </td>
                <td>{{ node.intervention_reason ?? node.degrade_reason ?? '—' }}</td>
                <td>{{ node.automation_path ?? '—' }}</td>
              </tr>
            </tbody>
          </table>

          <!-- 口径声明（R-MTR-04 / D3） -->
          <p v-if="metrics.caveat" class="caveat">{{ metrics.caveat }}</p>
          <p v-if="metrics.measured_at" class="measured-at mono">
            实测时间：{{ metrics.measured_at.replace('T', ' ').slice(0, 19) }}
            UTC（X-MTR-03：仅正式验收数值作为基线）
          </p>
        </div>

        <p v-else class="state-line">metrics.json 尚未生成（需任务完整跑完）。</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.regression {
  max-width: 1280px;
  margin: 0 auto;
}

.state-line {
  padding: var(--space-3);
  color: var(--text-3);
  font-size: var(--text-sm);
}

.state-error {
  color: var(--warning);
}

.view-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
  color: var(--text-1);
  font-size: var(--text-lg);
  font-weight: 600;
}

.layout {
  display: grid;
  grid-template-columns: 1fr 1.2fr;
  gap: var(--space-4);
  align-items: start;
}

@media (max-width: 1000px) {
  .layout {
    grid-template-columns: 1fr;
  }
}

.right-col {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
}

.card-title {
  font-size: var(--text-sm);
  font-weight: 600;
  margin-bottom: var(--space-3);
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--border);
}

.tag {
  display: inline-block;
  padding: 2px var(--space-2);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  border: 1px solid var(--border);
  background: var(--surface-2);
  color: var(--text-2);
}

.tag-accent {
  background: var(--accent-dim);
  border-color: var(--accent-glow);
  color: var(--accent-strong);
}

.tag[data-status='completed'] {
  background: var(--accent-dim);
  border-color: var(--accent-glow);
  color: var(--accent-strong);
}

.tag[data-status='manual_intervention'] {
  background: var(--warning-dim);
  border-color: color-mix(in srgb, var(--warning) 30%, transparent);
  color: var(--warning);
}

/* 资产树 */
.suite {
  margin-bottom: var(--space-4);
}

.suite-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}

.suite-count {
  font-size: var(--text-xs);
  color: var(--text-3);
}

.script-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.script-item {
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface-2);
  margin-bottom: var(--space-2);
}

.script-line {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.script-case {
  color: var(--accent-strong);
  font-size: var(--text-sm);
  font-weight: 600;
}

.script-version {
  font-size: var(--text-xs);
  color: var(--text-3);
}

.script-trace {
  font-size: var(--text-xs);
  color: var(--text-2);
  margin-top: 2px;
}

.script-path {
  font-size: var(--text-xs);
  color: var(--text-3);
  margin-top: 2px;
  word-break: break-all;
}

/* 提示条 */
.hint {
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  margin-bottom: var(--space-2);
}

.hint-warning {
  background: var(--warning-dim);
  color: var(--warning);
  border: 1px solid color-mix(in srgb, var(--warning) 30%, transparent);
}

/* 套件重跑 */
.action-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-3);
}

.action-result {
  font-size: var(--text-xs);
  color: var(--accent-strong);
}

.primary-btn {
  padding: var(--space-2) var(--space-4);
  background: var(--accent);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  color: #ffffff;
  font-size: var(--text-md);
  cursor: pointer;
}

.primary-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

/* 度量 */
.rate-block {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-3) var(--space-5);
  background: var(--accent-dim);
  border: 1px solid var(--accent-glow);
  border-radius: var(--radius-md);
  margin-bottom: var(--space-3);
}

.rate-value {
  font-size: 32px;
  font-weight: 600;
  color: var(--accent-strong);
  line-height: 1.2;
}

.rate-label {
  font-size: var(--text-xs);
  color: var(--text-3);
}

.metrics-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
  margin-top: var(--space-3);
}

.metrics-table th {
  text-align: left;
  font-weight: 500;
  font-size: var(--text-xs);
  color: var(--text-3);
  padding: var(--space-2);
  border-bottom: 1px solid var(--border-strong);
}

.metrics-table td {
  padding: var(--space-2);
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}

.caveat {
  margin: var(--space-3) 0 0;
  padding: var(--space-2) var(--space-3);
  background: var(--surface-2);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  color: var(--text-2);
}

.measured-at {
  margin: var(--space-2) 0 0;
  font-size: var(--text-xs);
  color: var(--text-3);
}
</style>
