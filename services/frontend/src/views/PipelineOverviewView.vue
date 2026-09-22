<script setup lang="ts">
// 流水线总览页（07 页 02 · 真实数据）：job 列表（list_jobs）+ 9 节点状态（get_job_status，
// 运行中每 3s 轮询，离开页面即停）+ 自动化率卡片（metrics.json 后端汇总，前端不计算）。
// C-16：节点卡可点击跳转对应页面（携带 jobId）；接口还原清单移除（独立页「源码扫描与接口还原」已有）。
import { computed, onUnmounted, ref, watch } from 'vue'
import {
  exportJobResults,
  getJobStatus,
  getLlmCallLogs,
  getMetrics,
  listJobs,
  rerunJob,
  stopJob,
} from '../api/client'
import type { JobListItem, JobStatus, LlmCallLog, MetricsPayload, NodeState } from '../api/types'
import { NODE_LABELS, STATUS_LABELS } from '../api/labels'
import { navigateTo, navigation } from '../api/navigation'

const POLL_INTERVAL_MS = 3000
const LIST_REFRESH_MS = 10000

const jobs = ref<JobListItem[]>([])
const listError = ref('')
const selectedId = ref('')

const status = ref<JobStatus | null>(null)
const statusError = ref('')
const metrics = ref<MetricsPayload | null>(null)
const llmLogs = ref<LlmCallLog[]>([])

let pollTimer: ReturnType<typeof setInterval> | null = null

/* ---- 重跑（C-14） ---- */

const rerunning = ref(false)
const stopping = ref(false)
const rerunError = ref('')
const rerunFromNode = ref('req_parse')
const rerunApiKey = ref('')
const rerunModel = ref('MiniMax-M3')
const rerunRestUrl = ref('')
const rerunE2eUrl = ref('')
const rerunE2eInsecureTls = ref(true)
let configForJobId = ''

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
    await rerunJob(selectedId.value, {
      ...(from === 'req_parse' ? {} : { from_node: from }),
      ...(rerunRestUrl.value.trim() ? { rest_url: rerunRestUrl.value.trim() } : {}),
      ...(status.value?.e2e && rerunE2eUrl.value.trim()
        ? {
            e2e: {
              base_url: rerunE2eUrl.value.trim(),
              generate_cases: status.value.e2e.generate_cases,
              execute_cases: status.value.e2e.execute_cases,
              insecure_tls: rerunE2eInsecureTls.value,
            },
          }
        : {}),
      ...(rerunApiKey.value
        ? {
            llm: {
              api_key: rerunApiKey.value,
              model: rerunModel.value,
              base_url: 'https://api.minimaxi.com/v1',
              max_tokens: 16384,
            },
          }
        : {}),
    })
    // 入队后立即刷新：级联失效 → 节点状态翻转为运行中 → watch 接管轮询
    await refreshDetail()
  } catch (err) {
    rerunError.value = err instanceof Error ? err.message : '重跑失败'
  } finally {
    rerunning.value = false
  }
}

async function handleStop(): Promise<void> {
  if (!selectedId.value || stopping.value || !status.value?.running) return
  stopping.value = true
  rerunError.value = ''
  try {
    await stopJob(selectedId.value)
    await Promise.all([refreshDetail(), refreshList(true)])
  } catch (err) {
    rerunError.value = err instanceof Error ? err.message : '停止任务失败'
  } finally {
    stopping.value = false
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
    navigation.e2e = null
    return
  }
  statusError.value = ''
  try {
    status.value = await getJobStatus(selectedId.value)
    if (configForJobId !== selectedId.value) {
      configForJobId = selectedId.value
      rerunRestUrl.value = status.value.rest_url ?? ''
      rerunE2eUrl.value = status.value.e2e?.base_url ?? ''
      rerunE2eInsecureTls.value = status.value.e2e?.insecure_tls ?? true
    }
    navigation.e2e = status.value.e2e
      ? {
          generateCases: status.value.e2e.generate_cases,
          executeCases: status.value.e2e.execute_cases,
        }
      : null
  } catch (err) {
    status.value = null
    navigation.e2e = null
    statusError.value = err instanceof Error ? err.message : 'job 状态加载失败'
  }
  // 派生产物独立加载：节点未跑到时如实提示，不阻塞状态展示
  try {
    metrics.value = await getMetrics(selectedId.value)
  } catch {
    metrics.value = null
  }
  llmLogs.value = await getLlmCallLogs(selectedId.value)
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

function refreshAfterAccessChanged(): void {
  void refreshList(false)
  void refreshDetail()
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
window.addEventListener('llm-autotest-access-changed', refreshAfterAccessChanged)
onUnmounted(() => {
  stopPolling()
  stopListRefresh()
  window.removeEventListener('llm-autotest-access-changed', refreshAfterAccessChanged)
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
  e2e_case_generate: 'e2e-cases',
  e2e_execute: 'e2e-execution',
}

const nodesInOrder = computed<OrderedNode[]>(() => {
  const byName = new Map((status.value?.nodes ?? []).map((node) => [node.name, node]))
  const result: OrderedNode[] = []
  for (const [name, label] of Object.entries(NODE_LABELS)) {
    const node = byName.get(name)
    result.push({ name, label, node })
    // 可选子节点继承父节点的生命周期展示，但不写入 state.json/自动化率分母。
    if (name === 'testcase_generate' && status.value?.e2e?.generate_cases) {
      result.push({ name: 'e2e_case_generate', label: '└ E2E 用例生成（可选）', node })
    }
    if (
      name === 'auto_execute' &&
      status.value?.e2e?.generate_cases &&
      status.value.e2e.execute_cases
    ) {
      result.push({ name: 'e2e_execute', label: '└ E2E 浏览器执行（可选）', node })
    }
  }
  return result
})

const activeStage = computed(() => status.value?.nodes.find((node) => node.status === 'running'))
const completedStages = computed(
  () => status.value?.nodes.filter((node) => node.status === 'completed').length ?? 0,
)
const latestRunEvent = computed(() => llmLogs.value.at(-1))
const latestRunId = computed(() => {
  const started = llmLogs.value.filter((entry) => entry.event === 'run_started' && entry.run_id)
  if (started.length) return started.at(-1)?.run_id ?? null
  return llmLogs.value.filter((entry) => entry.run_id).at(-1)?.run_id ?? null
})
const recentLogCount = computed(() =>
  latestRunId.value
    ? llmLogs.value.filter((entry) => entry.run_id === latestRunId.value).length
    : 0,
)
const historyLogCount = computed(() => Math.max(0, llmLogs.value.length - recentLogCount.value))
const stageProgress = computed(() => {
  const event = latestRunEvent.value
  const index = event?.event === 'e2e_case_started' ? event.index : event?.batch?.index
  const total = event?.event === 'e2e_case_started' ? event.total : event?.batch?.total
  if (typeof index === 'number' && typeof total === 'number' && total > 0) {
    return {
      known: true,
      value: Math.min(100, Math.round((index / total) * 100)),
      label: `${index}/${total}`,
    }
  }
  return { known: false, value: 0, label: activeStage.value ? '处理中' : '—' }
})

function stageDetail(): string {
  const event = latestRunEvent.value
  if (event?.event === 'e2e_case_started')
    return `E2E ${event.index}/${event.total} · ${event.case_id} 执行中`
  if (event?.event === 'started')
    return `第 ${event.batch?.index}/${event.batch?.total} 批模型调用中`
  if (
    event?.event === 'node_started' ||
    event?.event === 'node_error' ||
    event?.event === 'node_finished'
  ) {
    return event.message ?? `${NODE_LABELS[event.node] ?? event.node} 正在处理`
  }
  if (event?.event === 'node_skipped') return event.message ?? '断点续跑跳过已完成节点'
  if (event?.event === 'run_started') return event.message ?? '流水线已启动'
  return activeStage.value
    ? `${NODE_LABELS[activeStage.value.name] ?? activeStage.value.name} 正在处理`
    : '等待任务开始'
}

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

function formatTime(iso: string | null): string {
  if (!iso) return '—'
  const value = new Date(iso)
  return Number.isNaN(value.getTime())
    ? iso
    : value
        .toLocaleString('zh-CN', {
          timeZone: 'Asia/Shanghai',
          hour12: false,
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        })
        .replaceAll('/', '-')
}

function formatLogMessage(entry: LlmCallLog): string {
  if (entry.event === 'started') {
    return `第 ${entry.batch?.index}/${entry.batch?.total} 批已发起（${entry.batch?.requirements} 条需求）`
  }
  if (entry.event === 'e2e_started')
    return entry.message ?? `开始执行 ${entry.total ?? 0} 条前端 E2E 用例`
  if (entry.event === 'e2e_case_started') {
    return `E2E ${entry.index}/${entry.total} · ${entry.case_id ?? '用例'} 开始执行`
  }
  if (entry.event === 'e2e_case_completed') {
    const outcome = entry.ok ? '通过' : `失败 · ${entry.message ?? '未知原因'}`
    return `E2E ${entry.index}/${entry.total} · ${entry.case_id ?? '用例'} ${outcome}${entry.duration_ms ? ` · ${entry.duration_ms}ms` : ''}`
  }
  if (
    entry.event === 'run_started' ||
    entry.event === 'node_started' ||
    entry.event === 'node_finished' ||
    entry.event === 'node_skipped'
  ) {
    return entry.message ?? '节点状态已更新'
  }
  if (entry.event === 'node_error') {
    return `${entry.message ?? '节点处理失败'}${entry.code ? ` · ${entry.code}` : entry.error ? ` · ${entry.error}` : ''}`
  }
  return entry.ok
    ? `模型调用完成 · ${entry.duration_ms ?? 0}ms${entry.tokens ? ` · ${entry.tokens} tokens` : ''}`
    : `调用失败 · ${entry.error ?? '未知错误'}`
}

function logRunLabel(entry: LlmCallLog): string {
  if (entry.run_id && entry.run_id === latestRunId.value) return '最近一轮'
  if (entry.run_scope === 'archive') return '历史归档'
  return '此前记录'
}

function runtimeErrorMessage(value: JobStatus['runtime_error']): string {
  if (!value) return ''
  if (typeof value === 'string') return value
  return [value.code, value.message].filter(Boolean).join('：') || '任务运行异常'
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
        <div>
          <h2 class="mono">{{ status.job_id }}</h2>
          <p class="detail-subtitle">
            {{
              status.running
                ? '任务正在运行，状态与日志会自动刷新'
                : '任务已停止，可查看结果或从指定节点重跑'
            }}
          </p>
        </div>
        <span class="mono detail-meta">更新于 {{ formatTime(status.updated_at) }}</span>
      </header>
      <div class="action-bar">
        <div class="rerun-controls">
          <button
            class="rerun-btn secondary"
            type="button"
            :disabled="status.running"
            @click="navigateTo('config', selectedId)"
          >
            编辑完整配置
          </button>
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
          <button
            v-if="status.running"
            class="rerun-btn danger"
            type="button"
            :disabled="stopping"
            @click="handleStop"
          >
            {{ stopping ? '停止中…' : '停止任务' }}
          </button>
          <button
            class="rerun-btn secondary"
            type="button"
            :disabled="!selectedId"
            @click="exportJobResults(selectedId)"
          >
            导出结果
          </button>
        </div>
        <div class="model-control">
          <input
            v-model="rerunApiKey"
            class="rerun-key"
            type="password"
            autocomplete="off"
            placeholder="可选：临时覆盖 API Key（留空复用已保存配置）"
            aria-label="重跑 LLM API Key"
          />
          <select v-model="rerunModel" class="mono rerun-select" aria-label="重跑语言模型">
            <option>MiniMax-M3</option>
            <option>MiniMax-M2.7</option>
            <option>MiniMax-M2.5</option>
          </select>
        </div>
        <div class="rerun-targets">
          <label title="填写服务根地址，不要填写具体接口（例如 /api/auth/login）"
            >接口目标
            <input
              v-model="rerunRestUrl"
              class="rerun-url mono"
              type="url"
              placeholder="https://host:port"
              aria-label="重跑接口目标地址"
            />
          </label>
          <label v-if="status.e2e"
            >E2E 目标
            <input
              v-model="rerunE2eUrl"
              class="rerun-url mono"
              type="url"
              placeholder="https://host:port"
              aria-label="重跑 E2E 目标地址"
            />
          </label>
          <label v-if="status.e2e" class="rerun-check"
            ><input v-model="rerunE2eInsecureTls" type="checkbox" /> 跳过 E2E 证书校验</label
          >
        </div>
      </div>

      <p v-if="rerunError" class="state-error" role="alert">{{ rerunError }}</p>
      <p class="rerun-note">
        仅修改执行地址时可从「自动执行」起重跑；需求、仓库、鉴权、模型或 E2E
        开关等完整参数请使用「编辑完整配置」，保存后会从需求解析起全量重跑。
      </p>

      <p v-if="statusError" class="state-error" role="alert">{{ statusError }}</p>
      <p v-if="status.runtime_error" class="state-error runtime-error" role="alert">
        运行异常：{{
          runtimeErrorMessage(status.runtime_error)
        }}。已保留当前节点状态，可从对应节点重跑。
      </p>

      <section class="stage-banner" aria-live="polite">
        <div>
          <span class="stage-kicker">当前阶段</span>
          <strong>{{
            activeStage ? (NODE_LABELS[activeStage.name] ?? activeStage.name) : '任务已结束'
          }}</strong>
          <span>{{ stageDetail() }}</span>
        </div>
        <div class="stage-progress">
          <span class="mono">{{ stageProgress.label }}</span>
          <div
            class="stage-progress-track"
            :class="{ indeterminate: !stageProgress.known && !!activeStage }"
            aria-label="当前阶段进度"
          >
            <i :style="stageProgress.known ? { width: `${stageProgress.value}%` } : undefined" />
          </div>
          <span class="mono">节点 {{ completedStages }}/9</span>
        </div>
      </section>

      <!-- 降级清单（D1：如实标注，分母不缩减） -->
      <div v-for="record in status.degraded" :key="record.node" class="hint hint-warning">
        降级 · {{ NODE_LABELS[record.node] ?? record.node }}：{{ record.reason }}（按人工介入计入）
      </div>

      <div class="flow-layout">
        <section class="flow-panel">
          <header class="flow-title">执行流程 <span>9 个核心节点 · E2E 按配置显示</span></header>
          <div class="pipeline">
            <div
              v-for="(entry, index) in nodesInOrder"
              :key="index"
              class="node-wrap"
              :class="{ optional: entry.name.startsWith('e2e_') }"
            >
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
                <span class="node-index mono">{{
                  entry.name.startsWith('e2e_') ? '·' : String(index + 1).padStart(2, '0')
                }}</span>
                <span class="node-main"
                  ><span class="node-label">{{ entry.label }}</span
                  ><span v-if="entry.node.error" class="node-error">{{
                    entry.node.error.message
                  }}</span></span
                >
                <span class="node-status"
                  ><i class="status-dot" aria-hidden="true" />{{
                    STATUS_LABELS[entry.node.status] ?? entry.node.status
                  }}</span
                >
              </div>
            </div>
          </div>
        </section>
        <aside class="progress-panel log-panel">
          <section class="run-log run-log-side" aria-live="polite">
            <div class="run-log-head">
              <h3>运行日志</h3>
              <span
                >{{ status.running ? '刷新中' : '已停止' }} · 最近一轮 {{ recentLogCount }} · 历史
                {{ historyLogCount }}</span
              >
            </div>
            <p v-if="!llmLogs.length" class="run-log-empty">
              {{ status.running ? '等待首个运行事件…' : '暂无运行记录。' }}
            </p>
            <ol v-else class="run-log-list">
              <li
                v-for="(entry, index) in llmLogs.slice().reverse()"
                :key="entry.call_id ?? `${entry.timestamp}-${index}`"
              >
                <span class="run-log-meta"
                  ><span class="mono">{{ formatTime(entry.timestamp) }}</span
                  ><em>{{ logRunLabel(entry) }}</em></span
                >
                <b>{{ NODE_LABELS[entry.node] ?? entry.node }}</b>
                <span>{{ formatLogMessage(entry) }}</span>
              </li>
            </ol>
          </section>
        </aside>
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

.detail-subtitle {
  margin: var(--space-1) 0 0;
  color: var(--text-3);
  font-size: var(--text-sm);
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

.runtime-error {
  margin: 0 0 var(--space-3);
  border: 1px solid color-mix(in srgb, var(--warning) 35%, transparent);
  border-radius: var(--radius-md);
  background: var(--warning-dim);
  overflow-wrap: anywhere;
}

/* 重跑（C-14） */
.rerun-controls {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.action-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-3);
  padding: var(--space-3);
  margin: 0 0 var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface-2);
}

.model-control {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}

.rerun-targets {
  display: flex;
  align-items: center;
  flex: 1 1 100%;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px solid var(--border);
}

.rerun-targets label {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  color: var(--text-3);
  font-size: var(--text-xs);
}

.rerun-url {
  width: min(280px, 32vw);
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--text-2);
  font-size: var(--text-xs);
}

.rerun-check {
  white-space: nowrap;
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

.rerun-key {
  min-width: 0;
  width: min(260px, 34vw);
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text-2);
  background: var(--surface);
  font-size: var(--text-xs);
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

.rerun-btn.secondary {
  border-color: var(--border-strong);
  background: var(--surface);
  color: var(--text-2);
}

.rerun-btn.danger {
  border-color: #d95c5c;
  background: #fff1f1;
  color: #b33f3f;
}

.rerun-btn.danger:hover:not(:disabled) {
  border-color: #c34545;
  background: #c34545;
  color: #fff;
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

.run-log {
  margin: 0 0 var(--space-4);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
}

.run-log-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-2);
}
.run-log-head h3 {
  margin: 0;
  font-size: var(--text-sm);
}
.run-log-head span,
.run-log-empty {
  color: var(--text-3);
  font-size: var(--text-xs);
}
.run-log-empty {
  margin: var(--space-2) 0 0;
}
.run-log-list {
  display: grid;
  gap: var(--space-2);
  margin: var(--space-2) 0 0;
  padding: 0;
  list-style: none;
}
.run-log-list li {
  display: grid;
  grid-template-columns: 150px 130px 1fr;
  gap: var(--space-2);
  color: var(--text-2);
  font-size: var(--text-xs);
}
.run-log-meta {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  color: var(--text-3);
}
.run-log-meta em {
  padding: 1px 5px;
  border-radius: 999px;
  background: var(--accent-dim);
  color: var(--accent-strong);
  font-style: normal;
  white-space: nowrap;
}
.run-log-side {
  margin: 0;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
}
.run-log-side .run-log-list {
  max-height: 280px;
  overflow-y: auto;
  padding-right: var(--space-1);
}
.run-log-side .run-log-list li {
  grid-template-columns: 1fr;
  gap: 2px;
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--border);
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

/* 当前阶段是流程和日志共享的唯一进度入口：有批次时展示真实比例，没有粒度时明确显示处理中。 */
.stage-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  margin: var(--space-4) 0 0;
  border: 1px solid var(--accent-glow);
  border-radius: var(--radius-lg);
  background: linear-gradient(90deg, var(--accent-dim), var(--surface));
}

.stage-banner > div:first-child {
  display: grid;
  min-width: min(100%, 300px);
  gap: 3px;
}

.stage-kicker {
  color: var(--text-3);
  font-size: var(--text-xs);
  letter-spacing: 0.08em;
}

.stage-banner strong {
  color: var(--text-1);
  font-size: var(--text-md);
  font-weight: 600;
}

.stage-banner strong + span {
  color: var(--text-3);
  font-size: var(--text-xs);
}

.stage-progress {
  display: grid;
  grid-template-columns: auto minmax(100px, 160px) auto;
  align-items: center;
  gap: var(--space-2);
  color: var(--text-3);
  font-size: var(--text-xs);
}

.stage-progress-track {
  height: 6px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--border);
}

.stage-progress-track i {
  display: block;
  width: 0;
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
  transition: width 0.3s ease;
}

.stage-progress-track.indeterminate i {
  width: 38%;
  animation: stage-loading 1.25s ease-in-out infinite;
}

@keyframes stage-loading {
  0% {
    transform: translateX(-115%);
  }
  100% {
    transform: translateX(300%);
  }
}

/* 9 核心节点 + 按配置显示的 E2E 子流程 */
.flow-layout {
  display: grid;
  grid-template-columns: minmax(380px, 1.1fr) minmax(320px, 0.9fr);
  gap: var(--space-4);
  align-items: start;
  margin: var(--space-4) 0;
}

.flow-panel,
.progress-panel {
  min-width: 0;
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
}

.flow-title {
  display: flex;
  justify-content: space-between;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
  font-size: var(--text-md);
  font-weight: 600;
}

.flow-title span {
  color: var(--text-3);
  font-size: var(--text-xs);
  font-weight: 400;
}

.pipeline {
  display: grid;
  gap: var(--space-2);
  /* 节点较多时在流程卡片内滚动，避免把紧随其后的实时日志推到首屏之外。 */
  max-height: 430px;
  padding-right: var(--space-1);
  overflow-y: auto;
}

.node-wrap {
  min-width: 0;
}

.node-wrap.optional {
  margin-left: var(--space-4);
  padding-left: var(--space-3);
  border-left: 1px dashed var(--border-strong);
}

.node {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--space-2);
  min-height: 46px;
  padding: 9px var(--space-3);
  background: var(--surface);
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

.node-main {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.node-label {
  font-size: var(--text-md);
  font-weight: 500;
  color: var(--text-2);
}

.node-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-xs);
  color: var(--text-3);
  white-space: nowrap;
}

.node-error {
  display: block;
  max-width: 100%;
  max-height: 34px;
  overflow: auto;
  font-size: var(--text-xs);
  color: var(--warning);
  overflow-wrap: anywhere;
  line-height: 1.35;
}

.status-dot {
  width: 6px;
  height: 6px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--text-3);
}

.node[data-status='completed'] .status-dot {
  background: var(--accent-strong);
}
.node[data-status='running'] .status-dot {
  background: var(--accent);
  box-shadow: 0 0 0 4px var(--accent-dim);
}
.node[data-status='manual_intervention'] .status-dot {
  background: var(--warning);
}

.progress-panel .rate-value {
  margin-top: var(--space-4);
}

.progress-panel .hint {
  margin-top: var(--space-3);
}

.progress-divider {
  height: 1px;
  margin: var(--space-4) 0 var(--space-3);
  background: var(--border);
}

.progress-summary {
  display: flex;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--border);
  color: var(--text-3);
  font-size: var(--text-xs);
}

.progress-summary strong {
  color: var(--text-2);
  font-weight: 500;
  text-align: right;
}

.progress-note {
  margin: var(--space-3) 0 0;
  color: var(--text-3);
  font-size: var(--text-xs);
  line-height: 1.6;
}

.node[data-status='completed'] {
  border-color: var(--border);
  border-left: 3px solid var(--accent);
  background: var(--surface);
}

.node[data-status='completed'] .node-label,
.node[data-status='completed'] .node-status {
  color: var(--text-2);
}

.node[data-status='running'] {
  border-color: var(--accent-glow);
  border-left: 3px solid var(--accent);
  background: var(--accent-dim);
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
  .running-dot,
  .stage-progress-track.indeterminate i {
    animation: none;
  }
}

@media (max-width: 960px) {
  .flow-layout {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 720px) {
  .action-bar,
  .model-control,
  .rerun-controls {
    align-items: stretch;
  }
  .action-bar,
  .model-control {
    flex-direction: column;
  }
  .rerun-controls {
    flex-wrap: wrap;
  }
  .rerun-key {
    width: 100%;
  }
  .node {
    grid-template-columns: 24px minmax(0, 1fr);
  }
  .node-status {
    grid-column: 2;
  }
  .run-log-list li {
    grid-template-columns: 1fr;
    gap: 2px;
  }
  .stage-progress {
    width: 100%;
    grid-template-columns: auto 1fr auto;
  }
}
</style>
