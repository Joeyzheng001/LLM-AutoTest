<script setup lang="ts">
// 汇总来自后端执行记录；通过、失败、待判定和未执行均可筛选，响应复用单独说明。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import JobPicker from '../components/JobPicker.vue'
import ExecutionResultDetail from '../components/ExecutionResultDetail.vue'
import {
  exportJobRun,
  getEvidenceBlob,
  getJobRuns,
  getRunExecution,
  getRunReportMd,
  getRunInterfaces,
  getRunTestcases,
} from '../api/client'
import type { ExecResult, ExecSummary, InterfaceDef, JobRun, TestCaseEntry } from '../api/types'
import { navigateTo } from '../api/navigation'

const props = defineProps<{ initialJobId?: string; initialRunId?: string; viewKey?: string }>()
type ResultFilter =
  'all' | 'passed' | 'assertion' | 'environment' | 'pending' | 'skipped' | 'reused'
const _jobId = ref('')
const runs = ref<JobRun[]>([])
const selectedRunId = ref('')
const runRefreshError = ref('')
const downloadPending = ref(false)
const selectedRun = computed(() => runs.value.find((run) => run.id === selectedRunId.value))
const historyCount = computed(() => runs.value.filter((run) => !run.is_current).length)
const summary = ref<ExecSummary | null>(null)
const results = ref<ExecResult[]>([])
const interfaceDefinitions = ref<InterfaceDef[]>([])
const caseDefinitions = ref<TestCaseEntry[]>([])
type FlowFilter = 'all' | 'normal' | 'boundary' | 'exception' | 'other'
const flowFilter = ref<FlowFilter>('all')
const degradedProtocols = ref<Array<{ protocol: string; mode: string; reason?: string }>>([])
const unitCoverage = ref<Awaited<ReturnType<typeof getRunExecution>>['unit_test_coverage']>()
const reportMd = ref('')
const evidenceUrls = ref<Record<string, string>>({})
const loading = ref(false)
const error = ref('')
const resultFilter = ref<ResultFilter>('all')
const search = ref('')
const interfaceFilter = ref('')
const expandedCases = ref(new Set<string>())
let loadSequence = 0
let runListSequence = 0

function runLabel(run: JobRun, index: number): string {
  const time = (value: string | null) =>
    value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '时间未记录'
  if (run.is_current) return `当前轮次 · 执行于 ${time(run.executed_at)}`
  const number = historyCount.value - index + (runs.value[0]?.is_current ? 1 : 0)
  return `历史轮次 ${number} · 执行于 ${time(run.executed_at)} · 归档于 ${time(run.archived_at)}`
}

async function loadRuns(): Promise<void> {
  const sequence = ++runListSequence
  ++loadSequence
  runs.value = []
  selectedRunId.value = ''
  summary.value = null
  results.value = []
  interfaceDefinitions.value = []
  caseDefinitions.value = []
  degradedProtocols.value = []
  unitCoverage.value = undefined
  reportMd.value = ''
  for (const url of Object.values(evidenceUrls.value)) URL.revokeObjectURL(url)
  evidenceUrls.value = {}
  error.value = ''
  runRefreshError.value = ''
  if (!_jobId.value) {
    loading.value = false
    return
  }
  loading.value = true
  try {
    const response = await getJobRuns(_jobId.value)
    if (sequence !== runListSequence) return
    runs.value = response.runs
    selectedRunId.value = response.runs.some((run) => run.id === props.initialRunId)
      ? props.initialRunId! : response.runs[0]?.id ?? ''
    if (!selectedRunId.value) {
      error.value = '这个任务尚无执行记录；重跑生成报告后可在这里查看。'
      loading.value = false
    }
  } catch (err) {
    if (sequence !== runListSequence) return
    error.value = err instanceof Error ? err.message : '运行轮次加载失败'
    loading.value = false
  }
}

async function refreshRuns(): Promise<void> {
  if (!_jobId.value) return
  const jobId = _jobId.value
  const sequence = ++runListSequence
  const previous = selectedRunId.value
  runRefreshError.value = ''
  try {
    const response = await getJobRuns(jobId)
    if (sequence !== runListSequence || jobId !== _jobId.value) return
    runs.value = response.runs
    const next = response.runs.some((run) => run.id === previous)
      ? previous
      : (response.runs[0]?.id ?? '')
    selectedRunId.value = next
    if (next === previous && next) await load()
    if (!next) error.value = '这个任务尚无执行记录。'
  } catch (err) {
    if (sequence !== runListSequence || jobId !== _jobId.value) return
    runRefreshError.value = err instanceof Error ? err.message : '刷新轮次失败'
  }
}

async function downloadSelectedRun(): Promise<void> {
  if (!_jobId.value || !selectedRunId.value || downloadPending.value) return
  const jobId = _jobId.value
  const runId = selectedRunId.value
  downloadPending.value = true
  runRefreshError.value = ''
  try {
    await exportJobRun(jobId, runId)
  } catch (err) {
    if (jobId === _jobId.value && runId === selectedRunId.value)
      runRefreshError.value = err instanceof Error ? err.message : '轮次下载失败'
  } finally {
    downloadPending.value = false
  }
}

const reportType = computed<'api' | 'e2e' | null>(() => {
  if (props.viewKey === 'e2e-execution') return 'e2e'
  if (props.viewKey === 'interface-report') return 'api'
  return null
})
const visibleResults = computed(() =>
  reportType.value === null
    ? results.value
    : results.value.filter((result) =>
        reportType.value === 'e2e' ? result.case_type === 'e2e' : result.case_type !== 'e2e',
      ),
)
const resultById = computed(() => new Map(results.value.map((result) => [result.case_id, result])))
const displaySummary = computed<ExecSummary | null>(() => {
  if (!summary.value) return null
  if (reportType.value && summary.value.by_type?.[reportType.value])
    return summary.value.by_type[reportType.value]
  if (reportType.value === 'e2e' && !results.value.some((result) => result.case_type === 'e2e')) {
    return {
      total: 0,
      passed: 0,
      failed_assertion: 0,
      failed_environment: 0,
      actual_requests: 0,
      observed_unjudged: 0,
      not_executed: 0,
      reused_response: 0,
    }
  }
  return summary.value
})
const statusOnlyResults = computed(() =>
  visibleResults.value.filter((result) => result.assertion_scope === 'status_only'),
)
const structureResults = computed(() =>
  visibleResults.value.filter((result) => result.assertion_scope === 'response_structure'),
)
const interfaces = computed(() =>
  [
    ...new Set([
      ...visibleResults.value.map((result) => result.interface_id),
      ...(reportType.value === 'e2e' ? [] : interfaceDefinitions.value.map((item) => item.id)),
    ]),
  ].sort(),
)
const caseById = computed(() => new Map(caseDefinitions.value.map((item) => [item.id, item])))
function flow(result: ExecResult): FlowFilter {
  if (result.category === 'normal') return 'normal'
  if (result.category === 'boundary') return 'boundary'
  if (result.category === 'exception') return 'exception'
  return 'other'
}
function flowLabel(result: ExecResult): string {
  if (flow(result) === 'normal')
    return caseById.value.get(result.case_id)?.oracle_basis === 'integration_entry'
      ? '正常入口'
      : '正常分支'
  return {
    normal: '正常分支',
    boundary: '边界场景',
    exception: '异常分支',
    other: result.case_type === 'e2e' ? '页面流程' : '未分类',
    all: '',
  }[flow(result)]
}
const flowOptions = computed(() =>
  [
    { key: 'all', label: '全部场景', count: visibleResults.value.length },
    {
      key: 'normal',
      label: '正常流程',
      count: visibleResults.value.filter((item) => flow(item) === 'normal').length,
    },
    {
      key: 'boundary',
      label: '边界场景',
      count: visibleResults.value.filter((item) => flow(item) === 'boundary').length,
    },
    {
      key: 'exception',
      label: '异常分支',
      count: visibleResults.value.filter((item) => flow(item) === 'exception').length,
    },
    {
      key: 'other',
      label: reportType.value === 'e2e' ? '页面流程' : '其他 / 未分类',
      count: visibleResults.value.filter((item) => flow(item) === 'other').length,
    },
  ].filter((item) => item.key !== 'other' || item.count > 0),
)

const summaryCards = computed<
  Array<{ key: ResultFilter; label: string; value: number; tone: string }>
>(() => [
  { key: 'all', label: '用例总数', value: displaySummary.value?.total ?? 0, tone: 'all' },
  { key: 'passed', label: '断言符合', value: displaySummary.value?.passed ?? 0, tone: 'pass' },
  {
    key: 'assertion',
    label: '断言失败',
    value: displaySummary.value?.failed_assertion ?? 0,
    tone: 'fail',
  },
  {
    key: 'environment',
    label: '环境失败',
    value: displaySummary.value?.failed_environment ?? 0,
    tone: 'env',
  },
  {
    key: 'pending',
    label: '已请求待判定',
    value: displaySummary.value?.observed_unjudged ?? 0,
    tone: 'pending',
  },
  {
    key: 'skipped',
    label: '未执行',
    value: displaySummary.value?.not_executed ?? displaySummary.value?.skipped ?? 0,
    tone: 'skipped',
  },
])
const filterOptions: Array<{ key: ResultFilter; label: string }> = [
  { key: 'all', label: '全部结果' },
  { key: 'passed', label: '断言通过' },
  { key: 'assertion', label: '断言失败' },
  { key: 'environment', label: '环境失败' },
  { key: 'pending', label: '已请求待判定' },
  { key: 'skipped', label: '未执行' },
  { key: 'reused', label: '复用响应' },
]
function isPending(result: ExecResult): boolean {
  return (
    result.execution_state === 'observed_unjudged' ||
    (result.execution_state == null &&
      result.skipped_reason != null &&
      result.request?.attempted === true)
  )
}
function isSkipped(result: ExecResult): boolean {
  return (
    result.execution_state === 'not_executed' ||
    (result.execution_state == null && result.skipped_reason != null && !result.request?.attempted)
  )
}
function matchesFilter(result: ExecResult): boolean {
  switch (resultFilter.value) {
    case 'passed':
      return result.passed
    case 'assertion':
      return result.failure_kind === 'assertion'
    case 'environment':
      return result.failure_kind === 'environment'
    case 'pending':
      return isPending(result)
    case 'skipped':
      return isSkipped(result)
    case 'reused':
      return result.execution_state === 'reused_response'
    default:
      return true
  }
}
const filteredResults = computed(() => {
  const keyword = search.value.trim().toLocaleLowerCase()
  return visibleResults.value.filter(
    (result) =>
      matchesFilter(result) &&
      (flowFilter.value === 'all' || flow(result) === flowFilter.value) &&
      (!interfaceFilter.value || result.interface_id === interfaceFilter.value) &&
      (!keyword ||
        [
          result.case_id,
          result.interface_id,
          result.title,
          result.request?.url,
          result.failure_reason,
          result.source_case_id,
        ]
          .filter(Boolean)
          .join(' ')
          .toLocaleLowerCase()
          .includes(keyword)),
  )
})
const interfaceGroups = computed(() =>
  interfaces.value
    .map((id) => {
      const definition = interfaceDefinitions.value.find((item) => item.id === id)
      const all = visibleResults.value.filter((item) => item.interface_id === id)
      const matched = filteredResults.value.filter((item) => item.interface_id === id)
      return {
        id,
        all,
        matched,
        endpoint: [definition?.signature.method, definition?.signature.path]
          .filter(Boolean)
          .join(' '),
        title: definition?.signature.summary,
        lanes: [
          {
            key: 'normal',
            label: '正常流程',
            items: matched.filter((item) => flow(item) === 'normal'),
          },
          {
            key: 'boundary',
            label: '边界场景',
            items: matched.filter((item) => flow(item) === 'boundary'),
          },
          {
            key: 'exception',
            label: '异常分支',
            items: matched.filter((item) => flow(item) === 'exception'),
          },
          {
            key: 'other',
            label: reportType.value === 'e2e' ? '页面流程' : '其他 / 未分类',
            items: matched.filter((item) => flow(item) === 'other'),
          },
        ].filter((lane) => lane.items.length),
      }
    })
    .filter((group) => {
      if (interfaceFilter.value && group.id !== interfaceFilter.value) return false
      if (group.matched.length) return true
      return (
        !group.all.length &&
        resultFilter.value === 'all' &&
        flowFilter.value === 'all' &&
        !search.value.trim()
      )
    }),
)
function verdict(result: ExecResult): string {
  if (isSkipped(result)) return '未执行'
  if (isPending(result)) return '待判定'
  if (result.execution_state === 'reused_response')
    return result.passed ? '复用 · 字段符合' : '复用 · 字段不符'
  if (result.passed && result.expectation_source === 'local_dependency_bytecode')
    return '依赖契约符合'
  if (result.passed) {
    const labels: Record<string, string> = {
      status_only: 'HTTP 符合',
      response_structure: '结构符合',
      business: '业务断言符合',
    }
    return labels[result.assertion_scope ?? ''] ?? '断言符合'
  }
  return result.failure_kind === 'environment'
    ? '环境失败'
    : result.failure_kind === 'assertion'
      ? '断言失败'
      : '待确认'
}
function tone(result: ExecResult): string {
  if (isSkipped(result)) return 'skipped'
  if (isPending(result)) return 'pending'
  return result.passed ? 'pass' : result.failure_kind === 'assertion' ? 'fail' : 'env'
}
function scopeText(result: ExecResult): string {
  if (!result.assertion_scope && result.oracle_status === 'verified') return '源码预期已确认'
  return {
    business: '业务断言',
    response_structure: '结构 / 回显',
    status_only: 'HTTP 状态',
    framework_boundary: '框架边界',
    ui_steps: '页面步骤',
    unverified: '预期待确认',
  }[result.assertion_scope ?? 'unverified']
}
function businessErrorText(result: ExecResult): string {
  if (!result.passed || result.execution_state === 'reused_response' ||
      !['status_only', 'response_structure'].includes(result.assertion_scope ?? '')) return ''
  let body: unknown = result.actual_body
  if (typeof body === 'string') {
    try { body = JSON.parse(body) } catch { return '' }
  }
  if (!body || typeof body !== 'object' || Array.isArray(body)) return ''
  const payload = body as Record<string, unknown>
  const errors: string[] = []
  for (const field of ['returnCode', 'resultCode']) {
    const value = payload[field]
    if (value != null && !['0', '0000', '200'].includes(String(value)))
      errors.push(`${field}=${value}`)
  }
  for (const [name, field] of [['icmTrialObj', 'errCode'], ['o32TrialObj', 'ErrorCode']]) {
    const nested = payload[name]
    if (!nested || typeof nested !== 'object' || Array.isArray(nested)) continue
    const value = (nested as Record<string, unknown>)[field]
    if (value != null && !['0', '0000'].includes(String(value)))
      errors.push(`${name}.${field}=${value}`)
  }
  return errors.join('、')
}
function traceText(result: ExecResult): string {
  return (
    [result.requirement_id, result.claim_id, result.testpoint_id].filter(Boolean).join(' · ') ||
    '未关联需求测试点'
  )
}
function toggleCase(caseId: string, event: Event): void {
  const next = new Set(expandedCases.value)
  if ((event.target as HTMLDetailsElement).open) next.add(caseId)
  else next.delete(caseId)
  expandedCases.value = next
}
function expandVisible(): void {
  expandedCases.value = new Set([
    ...expandedCases.value,
    ...filteredResults.value.map((result) => result.case_id),
  ])
}
function collapseVisible(): void {
  const visible = new Set(filteredResults.value.map((result) => result.case_id))
  expandedCases.value = new Set([...expandedCases.value].filter((id) => !visible.has(id)))
}
function clearFilters(): void {
  resultFilter.value = 'all'
  interfaceFilter.value = ''
  flowFilter.value = 'all'
  search.value = ''
}
async function load(): Promise<void> {
  if (!_jobId.value || !selectedRunId.value) return
  const sequence = ++loadSequence
  const jobId = _jobId.value
  const runId = selectedRunId.value
  loading.value = true
  error.value = ''
  clearFilters()
  expandedCases.value = new Set()
  for (const url of Object.values(evidenceUrls.value)) URL.revokeObjectURL(url)
  evidenceUrls.value = {}
  try {
    const [execution, markdown, definitions, cases] = await Promise.all([
      getRunExecution(jobId, runId),
      getRunReportMd(jobId, runId).catch(() => ''),
      getRunInterfaces(jobId, runId).catch(() => null),
      getRunTestcases(jobId, runId).catch(() => null),
    ])
    if (sequence !== loadSequence) return
    summary.value = execution.summary
    results.value = execution.results
    interfaceDefinitions.value = definitions?.interfaces ?? []
    caseDefinitions.value = cases?.cases ?? []
    degradedProtocols.value = execution.degraded_protocols ?? []
    unitCoverage.value = execution.unit_test_coverage
    reportMd.value = markdown
    const entries = await Promise.all(
      (runId === 'current' ? execution.results : [])
        .filter((result) => result.evidence?.screenshot_ref)
        .map(async (result) => {
          try {
            const blob = await getEvidenceBlob(jobId, result.evidence!.screenshot_ref!)
            return [result.case_id, URL.createObjectURL(blob)] as const
          } catch {
            return null
          }
        }),
    )
    const urls = Object.fromEntries(entries.filter((entry) => entry !== null))
    if (sequence === loadSequence) evidenceUrls.value = urls
    else for (const url of Object.values(urls)) URL.revokeObjectURL(url)
  } catch (err) {
    if (sequence !== loadSequence) return
    error.value = err instanceof Error ? err.message : '执行产物加载失败'
    summary.value = null
    results.value = []
    interfaceDefinitions.value = []
    caseDefinitions.value = []
    degradedProtocols.value = []
    unitCoverage.value = undefined
    reportMd.value = ''
  } finally {
    if (sequence === loadSequence) loading.value = false
  }
}
onBeforeUnmount(() => {
  loadSequence++
  runListSequence++
  for (const url of Object.values(evidenceUrls.value)) URL.revokeObjectURL(url)
})
watch(_jobId, () => void loadRuns())
watch(selectedRunId, () => void load())
watch(
  () => props.initialJobId,
  (value) => {
    if (value) _jobId.value = value
  },
  { immediate: true },
)
</script>

<template>
  <div class="report">
    <JobPicker v-model="_jobId" />
    <section v-if="runs.length" class="run-history" aria-label="运行轮次">
      <div>
        <strong>运行轮次</strong>
        <span>保留 {{ historyCount }} 轮历史记录；重跑期间仍可查看旧报告。</span>
      </div>
      <label>
        <span>查看</span>
        <select v-model="selectedRunId" aria-label="选择运行轮次">
          <option v-for="(run, index) in runs" :key="run.id" :value="run.id">
            {{ runLabel(run, index) }}
          </option>
        </select>
      </label>
      <button type="button" @click="refreshRuns">刷新轮次</button>
      <button type="button" :disabled="downloadPending" @click="downloadSelectedRun">
        {{ downloadPending ? '下载中…' : '下载此轮' }}
      </button>
      <button
        v-if="runs[0]?.id !== selectedRunId"
        type="button"
        @click="selectedRunId = runs[0].id"
      >
        查看最新
      </button>
      <p v-if="selectedRun" class="run-summary">
        {{ selectedRun.is_current ? '当前结果' : '历史快照' }} ·
        {{ selectedRun.summary.total }} 条用例 ·
        {{ selectedRun.summary.passed }} 条断言符合 ·
        {{ selectedRun.summary.failed_assertion }} 条断言失败
        <template v-if="!selectedRun.has_report"> · 完整报告尚未生成</template>
      </p>
      <p v-if="runRefreshError" class="run-refresh-error" role="alert">{{ runRefreshError }}</p>
    </section>
    <p v-if="loading" class="state-line" role="status">加载执行记录中…</p>
    <p v-else-if="error" class="state-line state-error" role="alert">{{ error }}</p>
    <p v-else-if="!_jobId" class="state-line">请先选择任务。</p>
    <template v-else-if="summary">
      <div class="view-title">
        <h2>
          {{
            reportType === 'e2e'
              ? 'E2E 测试报告'
              : reportType === 'api'
                ? '接口测试报告'
                : '测试报告'
          }}
        </h2>
        <span v-if="reportType" class="tag">已按用例类型筛选</span>
      </div>
      <p class="overview-note">点击汇总卡片筛选下方用例，展开任意用例查看请求、响应与断言。</p>
      <div class="kpi-row" aria-label="执行结果汇总">
        <button
          v-for="card in summaryCards"
          :key="card.key"
          type="button"
          class="kpi-card"
          :class="[`tone-${card.tone}`, { selected: resultFilter === card.key }]"
          :aria-pressed="resultFilter === card.key"
          @click="resultFilter = card.key"
        >
          <span class="kpi-value mono">{{ card.value }}</span
          ><span class="kpi-label">{{ card.label }}</span>
        </button>
      </div>
      <p class="total-equation">
        总数 {{ displaySummary?.total ?? 0 }} = 断言符合 {{ displaySummary?.passed ?? 0 }} + 断言失败
        {{ displaySummary?.failed_assertion ?? 0 }} + 环境失败
        {{ displaySummary?.failed_environment ?? 0 }} + 待判定
        {{ displaySummary?.observed_unjudged ?? 0 }} + 未执行
        {{ displaySummary?.not_executed ?? displaySummary?.skipped ?? 0 }}
      </p>
      <div class="request-metrics">
        <div>
          <strong class="mono">{{ displaySummary?.actual_requests ?? '—' }}</strong
          ><span>实际发送请求</span>
        </div>
        <button
          type="button"
          :class="{ selected: resultFilter === 'reused' }"
          @click="resultFilter = 'reused'"
        >
          <strong class="mono">{{ displaySummary?.reused_response ?? 0 }}</strong
          ><span>复用真实响应核对 · 查看</span>
        </button>
        <p>断言符合数包含复用响应的字段核对；复用场景没有另发请求。</p>
      </div>
      <section v-if="reportType !== 'e2e' && unitCoverage" class="hint hint-notice">
        <strong>隔离单元测试与 JaCoCo 行覆盖率</strong>
        <p>
          {{ unitCoverage.cases_passed }}/{{ unitCoverage.cases_total }} 条候选单测通过。
          单测结果单独统计，不改变接口用例的执行结论。
        </p>
        <p v-if="unitCoverage.reason">{{ unitCoverage.reason }}</p>
        <button type="button" @click="navigateTo('unit-report', _jobId, '', selectedRunId)">
          查看单元测试报告 →
        </button>
      </section>
      <div v-for="item in degradedProtocols" :key="item.protocol" class="hint hint-warning">
        降级 · {{ item.protocol }}：{{ item.mode
        }}<template v-if="item.reason"> · {{ item.reason }}</template>
      </div>
      <div v-if="displaySummary?.not_executed || displaySummary?.skipped" class="hint hint-notice">
        {{ displaySummary.not_executed ?? displaySummary.skipped }}
        条用例未执行，具体缺少条件见下方明细。
      </div>
      <div v-if="displaySummary?.actual_requests != null" class="hint hint-notice">
        实际发送 {{ displaySummary.actual_requests }} 次请求；{{
          displaySummary.observed_unjudged ?? 0
        }}
        条已请求待判定；{{ displaySummary.reused_response ?? 0 }} 条复用真实响应核对。
      </div>
      <div
        v-if="reportType !== 'e2e' && reportMd.includes('**正常业务验证未完成**')"
        class="hint hint-warning"
      >
        正常业务验证未完成：部分接口尚无通过的正常业务断言。请结合下方响应和报告中的正常场景验证情况判断。
      </div>
      <div v-if="structureResults.length" class="hint hint-warning">
        {{ structureResults.length }}
        条用例仅配置响应结构或编号回显检查，不能作为业务规则通过的结论。
      </div>
      <div v-if="statusOnlyResults.length" class="hint hint-warning">
        {{ statusOnlyResults.length }} 条用例仅配置 HTTP 状态检查，不能据此认定业务功能通过。
      </div>

      <section class="result-section" aria-label="用例执行明细">
        <header class="section-header">
          <h3>
            用例明细
            <span class="result-count"
              >{{ filteredResults.length }} / {{ visibleResults.length }}</span
            >
          </h3>
          <div class="expand-actions">
            <button type="button" @click="expandVisible">展开当前列表</button
            ><button type="button" @click="collapseVisible">收起当前列表</button>
          </div>
        </header>
        <div class="filters">
          <label class="search-label"
            ><span>搜索</span
            ><input v-model="search" type="search" placeholder="用例编号、名称、接口或地址"
          /></label>
          <label
            ><span>结果</span
            ><select v-model="resultFilter">
              <option v-for="option in filterOptions" :key="option.key" :value="option.key">
                {{ option.label }}
              </option>
            </select></label
          >
          <label
            ><span>接口</span
            ><select v-model="interfaceFilter">
              <option value="">全部接口</option>
              <option v-for="id in interfaces" :key="id" :value="id">{{ id }}</option>
            </select></label
          >
          <button type="button" class="clear-button" @click="clearFilters">清除筛选</button>
        </div>
        <div class="flow-tabs" aria-label="场景类型筛选">
          <button
            v-for="option in flowOptions"
            :key="option.key"
            type="button"
            :class="[`flow-${option.key}`, { active: flowFilter === option.key }]"
            :aria-pressed="flowFilter === option.key"
            @click="flowFilter = option.key as FlowFilter"
          >
            {{ option.label }} <strong class="mono">{{ option.count }}</strong>
          </button>
        </div>
        <p class="flow-note">
          场景颜色区分正常、边界和异常设计；左侧结果标识表示是否通过。异常场景也可以通过。正常入口检查不代表内部业务分支已覆盖。
        </p>
        <p v-if="!interfaceGroups.length" class="state-line">当前筛选下没有用例。</p>
        <section
          v-for="group in interfaceGroups"
          :key="group.id"
          class="interface-group"
          :aria-label="group.id"
        >
          <header class="interface-heading">
            <div>
              <h3 class="mono">{{ group.id }}</h3>
              <p v-if="group.endpoint" class="mono">{{ group.endpoint }}</p>
              <p v-if="group.title">{{ group.title }}</p>
            </div>
            <span class="group-count"
              >{{ group.matched.length }} / {{ group.all.length }} 条 · 通过
              {{ group.all.filter((item) => item.passed).length }}</span
            >
          </header>
          <p v-if="!group.all.length" class="coverage-gap">
            该接口暂无用例执行记录，请查看 Code Review 的设计缺口与用例清单。
          </p>
          <p v-else-if="!group.all.some((item) => flow(item) === 'normal')" class="coverage-gap">
            该接口暂无正常流程用例，当前结果不能说明正常业务已验证。
          </p>
          <section v-for="lane in group.lanes" :key="lane.key" class="flow-lane">
            <h4 :class="`flow-${lane.key}`">
              {{ lane.label }} <span class="mono">{{ lane.items.length }}</span>
            </h4>
            <details
              v-for="result in lane.items"
              :key="`${_jobId}-${result.case_id}`"
              class="case-result"
              :class="`flow-border-${flow(result)}`"
              :open="expandedCases.has(result.case_id)"
              @toggle="toggleCase(result.case_id, $event)"
            >
              <summary>
                <span class="result-badge" :class="`tone-${tone(result)}`">{{
                  verdict(result)
                }}</span>
                <span class="case-main"
                  ><strong
                    ><span class="mono">{{ result.case_id }}</span>
                    {{ result.title || result.interface_id }}</strong
                  ><span class="case-subtitle"
                    ><span class="flow-badge" :class="`flow-${flow(result)}`">{{
                      flowLabel(result)
                    }}</span>
                    · {{ result.interface_id }} ·
                    {{ result.case_type === 'e2e' ? '前端 E2E' : '接口' }} ·
                    {{ scopeText(result) }}
                    <span v-if="result.expectation_source === 'local_dependency_bytecode'">
                      · 本地依赖字节码，部署版本待核对
                    </span>
                    <span v-if="businessErrorText(result)" class="case-business-error">
                      · 业务返回错误：{{ businessErrorText(result) }}
                    </span></span
                  ></span
                >
                <span class="case-http mono"
                  ><template v-if="result.actual_status !== undefined"
                    >HTTP {{ result.actual_status }}</template
                  ><template v-else>—</template
                  ><small v-if="result.execution_state === 'reused_response'"
                    >来自 {{ result.source_case_id }}</small
                  ><small v-else-if="result.duration_ms !== undefined"
                    >{{ result.duration_ms }} ms</small
                  ></span
                >
                <span class="case-action">{{
                  expandedCases.has(result.case_id)
                    ? '收起详情'
                    : result.execution_state === 'not_executed'
                      ? '查看所缺条件'
                      : '查看请求与响应'
                }}</span>
              </summary>
              <template v-if="expandedCases.has(result.case_id)">
                <div class="trace-line">{{ traceText(result) }}</div>
                <ExecutionResultDetail
                  :job-id="selectedRunId === 'current' ? _jobId : undefined"
                  :result="result"
                  :source-result="
                    result.source_case_id ? resultById.get(result.source_case_id) : undefined
                  "
                  :screenshot-url="evidenceUrls[result.case_id]"
                />
              </template>
            </details>
          </section>
        </section>
      </section>
      <details v-if="reportMd" class="raw-report">
        <summary>测试报告原文 · test_report.md</summary>
        <pre class="report-md mono">{{ reportMd }}</pre>
      </details>
    </template>
    <p v-else class="state-line">执行产物尚未生成，需任务运行到自动执行节点之后。</p>
  </div>
</template>

<style scoped>
.report {
  max-width: 1200px;
  margin: 0 auto;
}
.run-history {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
  margin: var(--space-3) 0;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface-2);
}
.run-history > div {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  min-width: 15rem;
}
.run-history > div span,
.run-summary {
  color: var(--text-3);
  font-size: var(--text-sm);
}
.run-history label {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.run-history select {
  max-width: min(34rem, 70vw);
}
.run-summary {
  flex-basis: 100%;
  margin: 0;
}
.run-refresh-error {
  flex-basis: 100%;
  margin: 0;
  color: var(--warning);
}
.state-line {
  padding: var(--space-3);
  color: var(--text-3);
  font-size: var(--text-sm);
}
.state-error {
  color: var(--warning);
}
.view-title,
.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  flex-wrap: wrap;
}
h2 {
  margin: 0;
  font-size: var(--text-lg);
}
h3 {
  margin: 0;
  font-size: var(--text-sm);
}
.tag,
.result-badge {
  border: 1px solid var(--border);
  background: var(--surface-2);
  border-radius: var(--radius-sm);
  padding: 3px 8px;
  font-size: var(--text-xs);
}
.overview-note {
  margin: var(--space-2) 0 var(--space-3);
  color: var(--text-3);
  font-size: var(--text-sm);
}
.kpi-row {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: var(--space-2);
}
.kpi-card {
  display: flex;
  align-items: flex-start;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  cursor: pointer;
  text-align: left;
}
.kpi-card:hover,
.kpi-card.selected {
  border-color: var(--accent-strong);
  background: var(--accent-dim);
}
.kpi-value {
  font-size: 28px;
  font-weight: 600;
  line-height: 1.2;
}
.kpi-label {
  color: var(--text-2);
  font-size: var(--text-xs);
}
.tone-all,
.tone-env,
.tone-skipped {
  color: var(--text-2);
}
.tone-pass {
  color: var(--accent-strong);
}
.tone-fail,
.tone-pending {
  color: var(--warning);
}
.total-equation {
  color: var(--text-3);
  font-size: var(--text-xs);
  margin: var(--space-2) 0 var(--space-3);
  line-height: 1.7;
}
.request-metrics {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-3);
  padding: var(--space-3);
  margin-bottom: var(--space-3);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}
.request-metrics > div,
.request-metrics > button {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.request-metrics strong {
  font-size: 23px;
  color: var(--text-1);
}
.request-metrics span,
.request-metrics p {
  font-size: var(--text-xs);
  color: var(--text-2);
}
.request-metrics p {
  flex: 1 1 240px;
  margin: 0;
  line-height: 1.7;
}
.request-metrics button {
  background: var(--surface);
}
button {
  font: inherit;
  color: var(--text-2);
}
button:focus-visible,
summary:focus-visible {
  outline: 2px solid var(--accent-strong);
  outline-offset: 3px;
}
.hint {
  padding: var(--space-2) var(--space-3);
  margin: 0 0 var(--space-2);
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  font-size: var(--text-sm);
  line-height: 1.6;
}
.hint-warning {
  background: var(--warning-dim);
  color: var(--warning);
}
.hint-notice {
  background: var(--accent-dim);
  color: var(--text-2);
}
.hint-notice button {
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  background: var(--bg-elev);
  color: var(--accent-strong);
  font: inherit;
  cursor: pointer;
}
.flow-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 12px 0;
}
.flow-tabs button {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 7px 10px;
  background: var(--surface);
  cursor: pointer;
}
.flow-tabs button.active {
  outline: 2px solid currentColor;
  outline-offset: 1px;
}
.flow-tabs strong {
  margin-left: 5px;
}
.flow-note {
  color: var(--text-3);
  font-size: var(--text-xs);
  line-height: 1.7;
}
.flow-normal {
  color: #2563b9;
  background: #eaf2ff;
}
.flow-boundary {
  color: #80600b;
  background: #fff5d8;
}
.flow-exception {
  color: #7941a7;
  background: #f4ebff;
}
.flow-other {
  color: var(--text-2);
  background: var(--surface-2);
}
.flow-badge {
  display: inline-block;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: var(--text-xs);
}
.interface-group {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  margin: var(--space-3) 0;
}
.interface-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}
.interface-heading p {
  margin: 4px 0;
  color: var(--text-2);
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
}
.group-count {
  font-size: var(--text-xs);
  color: var(--text-3);
}
.flow-lane h4 {
  display: inline-block;
  font-size: var(--text-xs);
  margin: 14px 0 8px;
  padding: 5px 8px;
  border-radius: 4px;
}
.flow-lane h4 span {
  margin-left: 8px;
}
.coverage-gap {
  font-size: var(--text-xs);
  padding: 10px;
  color: var(--warning);
  background: var(--warning-dim);
  line-height: 1.7;
  border-radius: 4px;
}
.case-result.flow-border-normal {
  border-left: 3px solid #5b8fd4;
}
.case-result.flow-border-boundary {
  border-left: 3px solid #c29a32;
}
.case-result.flow-border-exception {
  border-left: 3px solid #a16dca;
}
.result-section {
  margin-top: var(--space-4);
}
.section-header {
  margin-bottom: var(--space-3);
}
.result-count {
  color: var(--text-3);
  font-weight: normal;
  margin-left: var(--space-2);
}
.expand-actions {
  display: flex;
  gap: var(--space-2);
}
.expand-actions button,
.clear-button,
.request-metrics button {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  padding: 6px 10px;
  cursor: pointer;
  font-size: var(--text-xs);
}
.request-metrics button.selected {
  border-color: var(--accent-strong);
}
.filters {
  display: flex;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}
.filters label {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  color: var(--text-3);
  font-size: var(--text-xs);
}
.search-label {
  flex: 1 1 240px;
}
.filters input,
.filters select {
  box-sizing: border-box;
  width: 100%;
  min-height: 34px;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-1);
  background: var(--surface);
  font: inherit;
  font-size: var(--text-sm);
}
.case-result {
  margin-bottom: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
}
.case-result[open] {
  border-color: var(--accent-strong);
}
.case-result > summary {
  display: grid;
  grid-template-columns: 90px minmax(0, 1fr) 90px 130px;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3);
  cursor: pointer;
  list-style: none;
}
.case-result > summary::-webkit-details-marker {
  display: none;
}
.result-badge {
  text-align: center;
}
.case-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.case-main strong {
  font-size: var(--text-sm);
  font-weight: 500;
  overflow-wrap: anywhere;
}
.case-subtitle,
.case-http {
  font-size: var(--text-xs);
  color: var(--text-3);
}
.case-business-error {
  color: var(--warning);
}
.case-http {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.case-action {
  color: var(--accent-strong);
  text-align: right;
  font-size: var(--text-xs);
}
.trace-line {
  padding: 0 var(--space-4) var(--space-2);
  color: var(--text-3);
  font-size: var(--text-xs);
}
.raw-report {
  margin-top: var(--space-4);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}
.raw-report > summary {
  cursor: pointer;
  color: var(--text-2);
  font-size: var(--text-sm);
}
.report-md {
  margin: var(--space-3) 0 0;
  padding: var(--space-3);
  background: var(--surface-2);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 600px;
  overflow: auto;
  font-size: var(--text-xs);
  line-height: 1.7;
}
@media (max-width: 900px) {
  .kpi-row {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .case-result > summary {
    grid-template-columns: 80px minmax(0, 1fr) 80px;
  }
  .case-action {
    grid-column: 2 / -1;
  }
}
@media (max-width: 520px) {
  .kpi-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .case-result > summary {
    gap: var(--space-2);
  }
}
</style>
