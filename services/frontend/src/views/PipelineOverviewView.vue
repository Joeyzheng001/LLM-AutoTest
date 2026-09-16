<script setup lang="ts">
// 流水线总览页（07 页 02 · 真实数据）：job 列表（list_jobs）+ 9 节点状态（get_job_status，
// 运行中每 3s 轮询，离开页面即停）+ 自动化率卡片（metrics.json 后端汇总，前端不计算）。
// C-16：节点卡可点击跳转对应页面（携带 jobId）；接口还原清单移除（独立页「源码扫描与接口还原」已有）。
import { computed, onUnmounted, ref, watch } from 'vue'
import { getJobStatus, getMetrics, listJobs, rerunJob } from '../api/client'
import type { JobListItem, JobStatus, MetricsPayload, NodeState } from '../api/types'
import { NODE_LABELS, STATUS_LABELS } from '../api/labels'
import { navigateTo } from '../api/navigation'

const POLL_INTERVAL_MS = 3000
const LIST_REFRESH_MS = 10000

const jobs = ref<JobListItem[]>([])
const listError = ref('')
const selectedId = ref('')

const status = ref<JobStatus | null>(null)
const statusError = ref('')
const metrics = ref<MetricsPayload | null>(null)

let pollTimer: ReturnType<typeof setInterval> | null = null

/* ---- 重跑（C-14） ---- */

const rerunning = ref(false)
const rerunError = ref('')
const rerunFromNode = ref('req_parse')

// 重跑起始节点选项（req_parse = 全量；其余从该节点起级联失效下游）
const rerunOptions = Object.entries(NODE_LABELS).map(([name, label]) => ({
  name,
  label: name === 'req_parse' ? '全量（从需求解析起）' : `从「${label}」起`,
}))

async function handleRerun(): Promise<void> {
  if (!selectedId.value || rerunning.value || status.value?.running) return
  rerunning.value = true
  rerunError.value = ''
  try {
    const from = rerunFromNode.value || 'req_parse'
    await rerunJob(selectedId.value, from === 'req_parse' ? undefined : from)
    // 入队后立即刷新：级联失效 → 节点状态翻转为运行中 → watch 接管轮询
    await refreshDetail()
  } catch (err) {
    rerunError.value = err instanceof Error ? err.message : '重跑失败'
  } finally {
    rerunning.value = false
  }
}

async function refreshList(keepSelection: boolean): Promise<void> {
  listError.value = ''
  try {
    const result = await listJobs()
    jobs.value = result.jobs
    if (!keepSelection || !jobs.value.some((job) => job.job_id === selectedId.value)) {
      selectedId.value = jobs.value[0]?.job_id ?? ''
    }
  } catch (err) {
    listError.value = err instanceof Error ? err.message : 'job 清单加载失败'
  }
}

async function refreshDetail(): Promise<void> {
  if (!selectedId.value) {
    status.value = null
    metrics.value = null
    return
  }
  statusError.value = ''
  try {
    status.value = await getJobStatus(selectedId.value)
  } catch (err) {
    status.value = null
    statusError.value = err instanceof Error ? err.message : 'job 状态加载失败'
  }
  // 派生产物独立加载：节点未跑到时如实提示，不阻塞状态展示
  try {
    metrics.value = await getMetrics(selectedId.value)
  } catch {
    metrics.value = null
  }
}

function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

let listTimer: ReturnType<typeof setInterval> | null = null

function stopListRefresh(): void {
  if (listTimer !== null) {
    clearInterval(listTimer)
    listTimer = null
  }
}

function syncPolling(): void {
  stopPolling()
  // 仅运行中的 job 轮询；空闲 job 静态展示（轮询类逻辑离开/失效即停）
  if (status.value?.running) {
    pollTimer = setInterval(() => void refreshDetail(), POLL_INTERVAL_MS)
  }
}

watch(selectedId, () => void refreshDetail())
watch(
  () => status.value?.running,
  () => syncPolling(),
)
void refreshList(false)
// 低频刷新清单：捕获新提交的 job（不影响选中，keepSelection=true）
listTimer = setInterval(() => void refreshList(true), LIST_REFRESH_MS)
onUnmounted(() => {
  stopPolling()
  stopListRefresh()
})

/* 九节点按固定顺序渲染（R-MTR-01），state.nodes 顺序不假定 */
interface OrderedNode {
  name: string
  label: string
  node: NodeState | undefined
}

// C-16：节点 → 目标页映射（code_scan/interface_reconstruct 同在「源码扫描与接口还原」页；
// auto_execute 结果经 execution.json 在测试报告页展示）
const NODE_VIEW: Record<string, string> = {
  req_parse: 'requirements',
  code_scan: 'scan',
  interface_reconstruct: 'scan',
  code_review: 'review',
  testcase_generate: 'cases',
  auto_execute: 'report',
  test_report: 'report',
  defect_list: 'defects',
  retest_regression: 'regression',
}

const nodesInOrder = computed<OrderedNode[]>(() => {
  const byName = new Map((status.value?.nodes ?? []).map((node) => [node.name, node]))
  return Object.entries(NODE_LABELS).map(([name, label]) => ({
    name,
    label,
    node: byName.get(name),
  }))
})

/* C-16：节点卡跳转——已完成/降级节点可点（有产物可看），pending/running 灰置 */
function nodeClickable(entry: OrderedNode): boolean {
  const statusName = entry.node?.status
  return (
    selectedId.value !== '' && (statusName === 'completed' || statusName === 'manual_intervention')
  )
}

function handleNodeClick(entry: OrderedNode): void {
  if (!nodeClickable(entry) || !selectedId.value) return
  const view = NODE_VIEW[entry.name]
  if (view) navigateTo(view, selectedId.value)
}

function formatRate(value: number | null): string {
  // 展示口径：后端已汇总的值仅做格式化（百分比），null = 数据不完整（X-MTR-02）
  return value === null ? '—' : `${Math.round(value * 100)}%`
}

function formatTime(iso: string | null): string {
  return iso ? iso.replace('T', ' ').replace('Z', ' UTC').slice(0, 23) : '—'
}
</script>

<template>
  <div class="overview">
    <!-- 左：job 列表 -->
    <aside class="job-list">
      <header class="panel-title">任务列表</header>
      <p v-if="listError" class="list-error" role="alert">{{ listError }}</p>
      <p v-if="!jobs.length && !listError" class="list-error">暂无任务（先在「任务配置」页提交）</p>
      <button
        v-for="job in jobs"
        :key="job.job_id"
        class="job-item"
        :class="{ active: job.job_id === selectedId }"
        type="button"
        @click="selectedId = job.job_id"
      >
        <span class="job-title mono">{{ job.job_id }}</span>
        <span class="job-meta">
          <span :class="job.running ? 'running-dot' : 'idle-dot'" aria-hidden="true" />
          {{ job.running ? '运行中' : `${job.completed}/${9} 节点` }}
          <template v-if="job.degraded"> · {{ job.degraded }} 降级</template>
          <template v-if="job.has_report"> · 有报告</template>
        </span>
      </button>
    </aside>

    <!-- 右：选中 job 详情 -->
    <section v-if="status" class="detail">
      <header class="detail-head">
        <h2 class="mono">{{ status.job_id }}</h2>
        <span class="mono detail-meta">{{ formatTime(status.updated_at) }}</span>
        <div class="rerun-controls">
          <select v-model="rerunFromNode" class="mono rerun-select" aria-label="重跑起始节点">
            <option v-for="option in rerunOptions" :key="option.name" :value="option.name">
              {{ option.label }}
            </option>
          </select>
          <button
            class="rerun-btn"
            type="button"
            :disabled="rerunning || status.running"
            @click="handleRerun"
          >
            {{ rerunning ? '重跑中…' : '重跑' }}
          </button>
        </div>
      </header>

      <p v-if="rerunError" class="state-error" role="alert">{{ rerunError }}</p>
      <p class="rerun-note">
        重跑会将目标节点及其全部下游旧产物归档（带时间戳防覆盖）并重置后重跑（C-14，避免新旧数据混用）。
      </p>

      <p v-if="statusError" class="state-error" role="alert">{{ statusError }}</p>

      <!-- 自动化率（数值来自后端 metrics.json 程序化汇总） -->
      <div class="rate-row">
        <div v-if="metrics" class="rate-block">
          <span class="rate-value mono">{{ formatRate(metrics.automation_rate.value) }}</span>
          <span class="rate-label">
            自动化率 ·
            <span class="mono"
              >{{ metrics.automation_rate.numerator }}/{{
                metrics.automation_rate.denominator
              }}</span
            >
            （分母固定 9）
          </span>
        </div>
        <div v-else class="rate-block rate-pending">
          <span class="rate-value mono">—</span>
          <span class="rate-label">自动化率 · metrics 尚未生成（任务未完整跑完）</span>
        </div>
        <div v-if="metrics && metrics.automation_rate.value === null" class="hint hint-warning">
          数据不完整，不输出误导数值（X-MTR-02）
        </div>
      </div>

      <!-- 降级清单（D1：如实标注，分母不缩减） -->
      <div v-for="record in status.degraded" :key="record.node" class="hint hint-warning">
        降级 · {{ NODE_LABELS[record.node] ?? record.node }}：{{ record.reason }}（按人工介入计入）
      </div>

      <!-- 9 节点流水线（R-MTR-01 固定口径；C-16 点击跳转对应页面） -->
      <div class="pipeline">
        <div v-for="(entry, index) in nodesInOrder" :key="index" class="node-wrap">
          <div
            v-if="entry.node"
            class="node"
            :data-status="entry.node.status"
            :class="{ 'node-link': nodeClickable(entry) }"
            :role="nodeClickable(entry) ? 'button' : undefined"
            :title="nodeClickable(entry) ? `查看${entry.label}详情` : undefined"
            :tabindex="nodeClickable(entry) ? 0 : undefined"
            @click="handleNodeClick(entry)"
            @keydown.enter="handleNodeClick(entry)"
          >
            <span class="node-index mono">{{ String(index + 1).padStart(2, '0') }}</span>
            <span class="node-label">{{ entry.label }}</span>
            <span class="node-status">{{
              STATUS_LABELS[entry.node.status] ?? entry.node.status
            }}</span>
            <span v-if="entry.node.error" class="node-error">{{ entry.node.error.message }}</span>
          </div>
        </div>
      </div>
    </section>

    <section v-else class="detail">
      <p class="state-line">{{ statusError || '请选择任务查看详情。' }}</p>
    </section>
  </div>
</template>

<style scoped>
.overview {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: var(--space-4);
  height: 100%;
}

@media (max-width: 960px) {
  .overview {
    grid-template-columns: 1fr;
    height: auto;
  }
}

/* 左列 job 列表 */
.job-list {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-3);
  overflow-y: auto;
}

.panel-title {
  color: var(--text-3);
  font-size: var(--text-sm);
  letter-spacing: 0.08em;
  padding: var(--space-2) var(--space-2) var(--space-3);
}

.list-error {
  padding: var(--space-2);
  font-size: var(--text-xs);
  color: var(--text-3);
}

.job-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  width: 100%;
  padding: var(--space-3);
  margin-bottom: var(--space-2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  color: var(--text-2);
  cursor: pointer;
  text-align: left;
  transition:
    background 0.15s,
    border-color 0.15s;
}

.job-item:hover {
  background: var(--surface-2);
}

.job-item.active {
  background: var(--accent-dim);
  border-color: var(--accent-glow);
  color: var(--text-1);
}

.job-title {
  font-size: var(--text-xs);
  font-weight: 500;
  word-break: break-all;
}

.job-meta {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  font-size: var(--text-xs);
  color: var(--text-3);
}

.running-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  animation: breathe-dot 1.8s ease-in-out infinite;
}

.idle-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-3);
}

@keyframes breathe-dot {
  0%,
  100% {
    box-shadow: 0 0 0 0 transparent;
  }
  50% {
    box-shadow: 0 0 6px 0 var(--accent-glow);
  }
}

/* 右列详情 */
.detail {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-5);
  overflow-y: auto;
}

.detail-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-bottom: var(--space-5);
}

.detail-head h2 {
  font-size: var(--text-lg);
  font-weight: 600;
  word-break: break-all;
}

.detail-meta {
  color: var(--text-3);
  font-size: var(--text-sm);
}

.state-line {
  padding: var(--space-3);
  color: var(--text-3);
  font-size: var(--text-sm);
}

.state-error {
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  color: var(--warning);
}

/* 重跑（C-14） */
.rerun-controls {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.rerun-select {
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--text-2);
  font-size: var(--text-xs);
  max-width: 220px;
}

.rerun-btn {
  padding: var(--space-1) var(--space-3);
  border: 1px solid var(--accent);
  border-radius: var(--radius-md);
  background: var(--accent-dim);
  color: var(--accent-strong);
  font-size: var(--text-sm);
  cursor: pointer;
  white-space: nowrap;
}

.rerun-btn:hover:not(:disabled) {
  background: var(--accent);
  color: #fff;
}

.rerun-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.rerun-note {
  margin: calc(-1 * var(--space-3)) 0 var(--space-3);
  font-size: var(--text-xs);
  color: var(--text-3);
}

/* 自动化率 */
.rate-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}

.rate-block {
  padding: var(--space-3) var(--space-5);
  background: var(--accent-dim);
  border: 1px solid var(--accent-glow);
  border-radius: var(--radius-md);
}

.rate-pending {
  background: var(--surface-2);
  border-color: var(--border);
}

.rate-value {
  display: block;
  font-size: 32px;
  font-weight: 600;
  color: var(--accent-strong);
  line-height: 1.2;
}

.rate-pending .rate-value {
  color: var(--text-3);
}

.rate-label {
  font-size: var(--text-xs);
  color: var(--text-3);
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

/* 9 节点流水线 */
.pipeline {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4) var(--space-2);
  margin-top: var(--space-4);
}

.node-wrap {
  display: flex;
  align-items: center;
}

.node {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 104px;
  max-width: 132px;
  padding: var(--space-3);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

/* C-16：可跳转节点卡（hover 提示可点） */
.node-link {
  cursor: pointer;
}

.node-link:hover,
.node-link:focus-visible {
  border-color: var(--accent);
  outline: none;
}

.node-index {
  font-size: var(--text-xs);
  color: var(--text-3);
}

.node-label {
  font-size: var(--text-md);
  font-weight: 500;
  color: var(--text-2);
}

.node-status {
  font-size: var(--text-xs);
  color: var(--text-3);
}

.node-error {
  font-size: 10px;
  color: var(--warning);
  word-break: break-all;
}

.node[data-status='completed'] {
  border-color: var(--accent-glow);
  background: var(--accent-dim);
}

.node[data-status='completed'] .node-label,
.node[data-status='completed'] .node-status {
  color: var(--accent-strong);
}

.node[data-status='running'] {
  border-color: var(--accent-glow);
  animation: breathe 1.8s ease-in-out infinite;
}

.node[data-status='running'] .node-status {
  color: var(--accent);
}

.node[data-status='manual_intervention'] {
  border-color: color-mix(in srgb, var(--warning) 40%, transparent);
  background: var(--warning-dim);
}

.node[data-status='manual_intervention'] .node-status {
  color: var(--warning);
}

@keyframes breathe {
  0%,
  100% {
    box-shadow: 0 0 0 0 transparent;
  }
  50% {
    box-shadow: 0 0 10px 0 var(--accent-glow);
  }
}

@media (prefers-reduced-motion: reduce) {
  .node[data-status='running'],
  .running-dot {
    animation: none;
  }
}
</style>
