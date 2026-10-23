<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import JobPicker from '../components/JobPicker.vue'
import { exportJobRun, getJobRuns, getRunExecution } from '../api/client'
import { navigation, navigateTo } from '../api/navigation'
import type {
  ExecResult,
  JobRun,
  UnitCoverageModule,
  UnitTestCoverage,
  UnitTestOutcome,
} from '../api/types'

const props = defineProps<{ initialJobId?: string; initialRunId?: string }>()
type Filter = 'all' | 'passed' | 'failed' | 'not_run' | 'generation_failed' | 'deferred' | 'inconsistent'
type Row = UnitTestOutcome & {
  repo: string
  sourceSha?: string
  module?: UnitCoverageModule
  result?: ExecResult
}
type GapGroupKey = 'not_executed' | 'observed_unjudged' | 'executed_failed' | 'reused_response' | 'executed_passed' | 'no_record'
const gapGroupOrder: Array<{ key: GapGroupKey; title: string; description: string }> = [
  { key: 'not_executed', title: '接口测试未执行', description: '优先查看已有通过单测的接口缺口；单测结论仅覆盖对应组件行为。' },
  { key: 'observed_unjudged', title: '接口已请求，结果待判定', description: '已有实际请求，但当前缺少可判读的接口结论。' },
  { key: 'executed_failed', title: '接口已执行，未通过', description: '单测可帮助定位组件行为，仍需单独分析接口失败原因。' },
  { key: 'reused_response', title: '接口复用历史响应', description: '本轮未直接请求接口，使用已有响应完成核对。' },
  { key: 'executed_passed', title: '接口已执行并通过', description: '单测提供更细的组件或分支验证。' },
  { key: 'no_record', title: '本轮无接口结果', description: '关联关系已建立，但本轮没有对应的接口执行记录。' },
]

const jobId = ref('')
const runs = ref<JobRun[]>([])
const selectedRunId = ref('')
const coverage = ref<UnitTestCoverage>()
const results = ref<ExecResult[]>([])
const loading = ref(false)
const error = ref('')
const filter = ref<Filter>('all')
const expanded = ref(new Set<string>())
const downloading = ref(false)
let runsVersion = 0
let reportVersion = 0

const selectedRun = computed(() => runs.value.find((run) => run.id === selectedRunId.value))
const rows = computed<Row[]>(() => {
  const byId = new Map(results.value.map((result) => [result.case_id, result]))
  return (coverage.value?.repositories ?? []).flatMap((repo) => {
    const modules = new Map((repo.modules ?? []).map((module) => [module.case_id, module]))
    return (repo.cases ?? []).map((item) => ({
      ...item,
      repo: repo.repo,
      sourceSha: repo.source_sha,
      module: modules.get(item.case_id),
      result: byId.get(item.case_id),
    }))
  })
})
const classCoverageRows = computed(() => (coverage.value?.repositories ?? []).flatMap((repo) =>
  (repo.suite_coverage?.modules ?? []).flatMap((module) =>
    (module.classes ?? []).map((entry) => ({ ...entry, repo: repo.repo,
      module: module.module, suiteStatus: module.status })),
  ),
).sort((left, right) => (left.line_coverage?.rate ?? 1) - (right.line_coverage?.rate ?? 1)))
const suiteModules = computed(() => (coverage.value?.repositories ?? []).flatMap((repo) =>
  (repo.suite_coverage?.modules ?? []).map((module) => ({ ...module, repo: repo.repo })),
))
const existingSuites = computed(() => (coverage.value?.repositories ?? []).map((repo) => ({
  ...repo.existing_suite, repo: repo.repo,
})).filter((suite) => suite.status))
const gapMappingRows = computed(() => {
  const linked = new Map<string, Row[]>()
  const byId = new Map(results.value.map((result) => [result.case_id, result]))
  for (const row of rows.value) {
    for (const caseId of row.linked_interface_case_ids ?? []) {
      if (!linked.has(caseId)) linked.set(caseId, [])
      linked.get(caseId)?.push(row)
    }
  }
  return [...linked.entries()].map(([caseId, tests]) => {
    const result = byId.get(caseId)
    const category: GapGroupKey = !result ? 'no_record'
      : result.execution_state === 'not_executed' ? 'not_executed'
      : result.execution_state === 'observed_unjudged' ? 'observed_unjudged'
      : result.execution_state === 'reused_response' ? 'reused_response'
      : result.passed ? 'executed_passed' : 'executed_failed'
    return { caseId, tests, category, result, verified: tests.some((test) => test.status === 'passed') }
  }).sort((left, right) => Number(right.verified) - Number(left.verified)
    || left.caseId.localeCompare(right.caseId))
})
const gapGroups = computed(() => gapGroupOrder.map((group) => {
  const items = gapMappingRows.value.filter((row) => row.category === group.key)
  return { ...group, items, verified: items.filter((row) => row.verified).length }
}).filter((group) => group.items.length))
const gapVerified = computed(() => gapMappingRows.value.filter((row) => row.verified).length)
const inconsistentRows = computed(() => rows.value.filter((row) => row.consistency_warning))
const metrics = computed(() => ({
  total: coverage.value?.cases_total ?? 0,
  passed: rows.value.filter((row) => row.status === 'passed').length,
  executed: rows.value.filter((row) => (row.tests ?? 0) > (row.skipped ?? 0)).length,
  generationFailed: rows.value.filter((row) =>
    row.status === 'generation_failed' || row.status === 'fixture_failed' || row.status === 'unsupported').length,
  deferred: rows.value.filter((row) => row.status === 'generation_deferred' || row.status === 'repair_deferred').length,
  notRun: rows.value.filter((row) =>
    row.status === 'not_run' || row.status === 'build_failed' || row.status === 'timed_out').length,
  failed: rows.value.filter((row) =>
    row.status === 'failed' || row.status === 'source_not_covered').length,
}))
const filters = computed<Array<{ key: Filter; label: string; count: number }>>(() => [
  { key: 'all', label: '全部', count: rows.value.length },
  { key: 'passed', label: '通过', count: metrics.value.passed },
  { key: 'failed', label: '未通过', count: metrics.value.failed },
  { key: 'not_run', label: '未运行/超时', count: metrics.value.notRun },
  { key: 'generation_failed', label: '生成/夹具问题', count: metrics.value.generationFailed },
  { key: 'deferred', label: '模型连接待恢复', count: metrics.value.deferred },
  { key: 'inconsistent', label: '结果不一致', count: inconsistentRows.value.length },
])
const visibleRows = computed(() => rows.value.filter((row) => {
  if (filter.value === 'all') return true
  if (filter.value === 'failed')
    return row.status === 'failed' || row.status === 'source_not_covered'
  if (filter.value === 'not_run')
    return row.status === 'not_run' || row.status === 'build_failed' || row.status === 'timed_out'
  if (filter.value === 'generation_failed')
    return row.status === 'generation_failed' || row.status === 'fixture_failed' || row.status === 'unsupported'
  if (filter.value === 'deferred')
    return row.status === 'generation_deferred' || row.status === 'repair_deferred'
  if (filter.value === 'inconsistent') return Boolean(row.consistency_warning)
  return row.status === 'passed'
}))

function statusLabel(status: UnitTestOutcome['status']): string {
  return {
    passed: '通过',
    failed: '断言失败',
    not_run: '未运行',
    build_failed: '构建失败',
    generation_failed: '生成失败',
    generation_deferred: '模型连接失败，待生成',
    repair_deferred: '模型连接失败，待修复',
    fixture_failed: '测试夹具错误',
    unsupported: '暂不支持',
    source_not_covered: '未命中源码证据',
    timed_out: '运行超时',
  }[status]
}

function statusTone(status: UnitTestOutcome['status']): string {
  if (status === 'passed') return 'success'
  if (status === 'failed' || status === 'source_not_covered') return 'danger'
  return 'warning'
}

function meaningfulCoverage(row: Row): boolean {
  return (row.tests ?? 0) > 0 && row.status !== 'fixture_failed' && row.module != null
}

function linkedHttpStatus(caseId: string): string {
  const result = results.value.find((item) => item.case_id === caseId)
  if (!result) return '本轮无记录'
  if (result.execution_state === 'not_executed') return '接口未执行'
  if (result.execution_state === 'observed_unjudged') return '接口已观察，未判定'
  if (result.execution_state === 'reused_response')
    return result.passed ? '复用响应符合' : '复用响应不符合'
  return result.passed ? '接口通过' : '接口未通过'
}

function gapLabel(kind?: UnitTestOutcome['gap_kind']): string {
  return kind === 'mock_or_config' ? '需要 Mock 或配置控制'
    : kind === 'component_detail' ? '补充组件内部断言'
    : '接口用例尚未覆盖'
}

function rate(value: { rate: number | null } | null | undefined): string {
  return value?.rate == null ? '—' : `${(value.rate * 100).toFixed(1)}%`
}

function fraction(value: { covered: number; missed: number } | null | undefined): string {
  return value ? `${value.covered}/${value.covered + value.missed}` : '—'
}

function runLabel(run: JobRun, index: number): string {
  const time = (value: string | null) => value
    ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '时间未记录'
  if (run.is_current) return `当前轮次 · ${time(run.executed_at)}`
  const historical = runs.value.filter((item) => !item.is_current).length
  return `历史轮次 ${historical - index + (runs.value[0]?.is_current ? 1 : 0)} · ${time(run.executed_at)}`
}

async function loadRuns(keepSelection = false): Promise<void> {
  const version = ++runsVersion
  ++reportVersion
  const currentJob = jobId.value
  const previous = keepSelection ? selectedRunId.value : ''
  runs.value = []
  selectedRunId.value = ''
  coverage.value = undefined
  results.value = []
  expanded.value = new Set()
  error.value = ''
  if (!currentJob) return
  loading.value = true
  try {
    const response = await getJobRuns(currentJob)
    if (version !== runsVersion || currentJob !== jobId.value) return
    runs.value = response.runs
    const preferred = previous || props.initialRunId || ''
    selectedRunId.value = response.runs.some((run) => run.id === preferred)
      ? preferred : response.runs[0]?.id ?? ''
    if (!selectedRunId.value) {
      error.value = '这个任务还没有执行记录。'
      loading.value = false
    }
  } catch (err) {
    if (version !== runsVersion || currentJob !== jobId.value) return
    error.value = err instanceof Error ? err.message : '运行轮次加载失败'
    loading.value = false
  }
}

async function loadReport(): Promise<void> {
  const version = ++reportVersion
  const currentJob = jobId.value
  const runId = selectedRunId.value
  coverage.value = undefined
  results.value = []
  expanded.value = new Set()
  if (!currentJob || !runId) return
  loading.value = true
  error.value = ''
  try {
    const execution = await getRunExecution(currentJob, runId)
    if (version !== reportVersion || currentJob !== jobId.value || runId !== selectedRunId.value) return
    coverage.value = execution.unit_test_coverage
    results.value = execution.results
  } catch (err) {
    if (version !== reportVersion || currentJob !== jobId.value || runId !== selectedRunId.value) return
    error.value = err instanceof Error ? err.message : '单元测试报告加载失败'
  } finally {
    if (version === reportVersion) loading.value = false
  }
}

function toggle(caseId: string): void {
  const next = new Set(expanded.value)
  if (next.has(caseId)) next.delete(caseId)
  else next.add(caseId)
  expanded.value = next
}

async function downloadRun(): Promise<void> {
  if (!jobId.value || !selectedRunId.value || downloading.value) return
  downloading.value = true
  error.value = ''
  try {
    await exportJobRun(jobId.value, selectedRunId.value)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '轮次下载失败'
  } finally {
    downloading.value = false
  }
}

watch(jobId, (value) => {
  navigation.jobId = value
  void loadRuns()
})
watch(selectedRunId, () => void loadReport())
watch(() => props.initialJobId, (value) => {
  if (value) jobId.value = value
}, { immediate: true })
</script>

<template>
  <div class="unit-report">
    <JobPicker v-model="jobId" />
    <div class="page-heading">
      <div>
        <p class="eyebrow">验证与交付</p>
        <h2>单元测试报告</h2>
        <p>查看源码驱动单测的执行结果、与接口测试的补缺口关系，以及 JaCoCo 类级行和分支覆盖率。</p>
      </div>
      <button v-if="jobId" class="secondary-button" type="button"
        @click="navigateTo('interface-report', jobId, '', selectedRunId)">
        接口测试报告 →
      </button>
    </div>

    <section v-if="runs.length" class="run-bar" aria-label="运行轮次">
      <label>运行轮次
        <select v-model="selectedRunId" aria-label="选择单测报告轮次">
          <option v-for="(run, index) in runs" :key="run.id" :value="run.id">
            {{ runLabel(run, index) }}
          </option>
        </select>
      </label>
      <button type="button" @click="loadRuns(true)">刷新轮次</button>
      <button type="button" :disabled="downloading" @click="downloadRun">
        {{ downloading ? '下载中…' : '下载此轮' }}
      </button>
      <span>{{ selectedRun?.is_current ? '当前结果' : '历史快照' }}</span>
    </section>

    <p v-if="loading" class="state-line" role="status">加载单元测试报告中…</p>
    <p v-else-if="error" class="state-line error" role="alert">{{ error }}</p>
    <section v-else-if="!coverage" class="empty-state">
      <strong>此轮没有单元测试数据</strong>
      <p>在任务配置中启用单元测试后，从「用例生成」节点重跑，再执行单测。</p>
    </section>
    <template v-else>
      <div class="metric-grid" aria-label="单元测试结果汇总">
        <div class="metric"><strong>{{ metrics.total }}</strong><span>候选用例</span></div>
        <div class="metric"><strong>{{ metrics.executed }}</strong><span>实际运行</span></div>
        <div class="metric success"><strong>{{ metrics.passed }}</strong><span>验证通过</span></div>
        <div class="metric warning"><strong>{{ metrics.generationFailed }}</strong><span>生成或夹具问题</span></div>
        <div class="metric warning"><strong>{{ metrics.notRun }}</strong><span>未运行、超时或构建失败</span></div>
        <div v-if="metrics.deferred" class="metric warning"><strong>{{ metrics.deferred }}</strong><span>模型连接待恢复</span></div>
      </div>
      <section v-if="existingSuites.length" class="class-coverage" aria-label="项目现有单元测试基线">
        <h3>项目现有单元测试基线</h3>
        <p class="detail-note">这里运行与目标类同名的现有测试文件，和平台生成的候选用例分开统计。工作区未提交的测试及 pom 改动会标明来源；覆盖率只对该基线有效。</p>
        <div v-for="suite in existingSuites" :key="suite.repo">
          <p v-if="suite.status === 'unavailable'">{{ suite.repo }}：{{ suite.reason || '现有测试不可用' }}</p>
          <template v-else>
            <p><strong>{{ suite.repo }}</strong>：实际执行 {{ suite.tests_executed ?? 0 }} 条，失败 {{ suite.tests_failed ?? 0 }} 条。</p>
            <div v-for="module in suite.modules ?? []" :key="module.module">
              <p>{{ module.module }} · {{ module.status }} · {{ module.tests_executed ?? 0 }} 条，失败 {{ module.tests_failed ?? 0 }} 条<span v-if="module.pom_provenance === 'working_tree_uncommitted'"> · 使用工作区未提交的 pom</span></p>
              <p v-if="module.reason" class="detail-note">{{ module.reason }}</p>
              <table v-if="module.tests?.length">
                <thead><tr><th>现有测试类</th><th>来源</th><th>目标类行覆盖</th><th>分支覆盖</th></tr></thead>
                <tbody>
                  <tr v-for="test in module.tests" :key="test.test_file">
                    <td>{{ test.test_class }}</td>
                    <td>{{ test.provenance === 'working_tree_uncommitted' ? '工作区未提交' : '目标提交' }}</td>
                    <td>{{ fraction(module.classes?.find((item) => item.name.endsWith(`.${test.test_class.replace(/Test$/, '')}`))?.line_coverage) }} · {{ rate(module.classes?.find((item) => item.name.endsWith(`.${test.test_class.replace(/Test$/, '')}`))?.line_coverage) }}</td>
                    <td>{{ fraction(module.classes?.find((item) => item.name.endsWith(`.${test.test_class.replace(/Test$/, '')}`))?.branch_coverage) }} · {{ rate(module.classes?.find((item) => item.name.endsWith(`.${test.test_class.replace(/Test$/, '')}`))?.branch_coverage) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </template>
        </div>
      </section>
      <p class="scope-note">
        单测通过表示断言通过且 JaCoCo 命中源码证据行；类级覆盖率来自同一模块的生成单测合跑，
        仅统计本轮生成的测试。接口 HTTP 执行结论单独统计。
      </p>
      <p v-if="coverage.reason" class="notice">{{ coverage.reason }}</p>
      <p v-if="metrics.total > 0 && metrics.executed === 0" class="notice">
        本轮没有单测实际运行。JaCoCo 生成的 0% 暂不作为源码覆盖结论。
      </p>
      <div v-for="repo in coverage.repositories" :key="repo.repo" class="repo-line">
        <strong>{{ repo.repo }}</strong>
        <span>源码提交 {{ repo.source_sha?.slice(0, 12) || '未记录' }}</span>
        <span v-if="repo.reason">{{ repo.reason }}</span>
      </div>

      <section v-if="gapMappingRows.length" class="class-coverage" aria-label="单测与接口测试缺口映射">
        <h3>单测补充了哪些接口测试缺口</h3>
        <p class="detail-note">{{ gapVerified }}/{{ gapMappingRows.length }} 个关联接口用例已有通过的补充单测。此结果只证明对应组件行为，不替代 HTTP 执行结论。</p>
        <div v-for="group in gapGroups" :key="group.key" class="gap-group">
          <div class="gap-group-heading">
            <h4>{{ group.title }} <span>{{ group.items.length }}</span></h4>
            <span>{{ group.verified }}/{{ group.items.length }} 已有通过单测</span>
          </div>
          <p class="detail-note">{{ group.description }}</p>
          <table>
            <thead><tr><th>接口用例及本轮结果</th><th>补缺口状态</th><th>关联单测</th><th>目标组件与补充内容</th></tr></thead>
            <tbody>
              <tr v-for="item in group.items" :key="item.caseId">
                <td>{{ item.caseId }}（{{ linkedHttpStatus(item.caseId) }}）
                  <small v-if="item.category === 'not_executed'">
                    {{ item.result?.execution_blockers?.join('；') || item.result?.skipped_reason || '未记录未执行原因' }}
                  </small>
                </td>
                <td><span class="status" :class="item.verified ? 'success' : 'warning'">
                  {{ item.verified ? '已有通过单测' : '尚未补齐' }}
                </span></td>
                <td><span v-for="test in item.tests" :key="test.case_id">
                  {{ test.case_id }}（{{ statusLabel(test.status) }}）<br>
                </span></td>
                <td><span v-for="test in item.tests" :key="test.case_id">
                  {{ test.target_class || test.target_source || '目标组件未记录' }} · {{ gapLabel(test.gap_kind) }}<br>
                </span></td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="class-coverage" aria-label="目标类 JaCoCo 覆盖率">
        <h3>目标类覆盖率</h3>
        <p v-for="item in suiteModules.filter((module) => module.line_coverage)"
          :key="`${item.repo}-${item.module}-total`" class="detail-note">
          {{ item.repo }} · {{ item.module }}：目标类合计行覆盖 {{ rate(item.line_coverage) }}，
          分支覆盖 {{ rate(item.branch_coverage) }}；合跑 {{ item.tests_executed ?? 0 }} 项，失败 {{ item.tests_failed ?? 0 }} 项。
        </p>
        <p v-if="!suiteModules.length" class="detail-note">本轮尚无生成单测合跑记录，不能给出类级覆盖率。</p>
        <p v-for="item in suiteModules.filter((module) => module.status === 'unavailable')"
          :key="`${item.repo}-${item.module}`" class="detail-note">
          {{ item.repo }} · {{ item.module }}：{{ item.reason || '合跑覆盖率不可用' }}
        </p>
        <table v-if="classCoverageRows.length">
          <thead><tr><th>目标类</th><th>模块</th><th>行覆盖</th><th>分支覆盖</th><th>关联单测</th></tr></thead>
          <tbody>
            <tr v-for="item in classCoverageRows" :key="`${item.repo}-${item.name}`">
              <td>{{ item.name }}<small v-if="item.suiteStatus === 'partial'">部分测试失败</small>
                <details v-if="item.missed_lines?.length"><summary>未覆盖行</summary>{{ item.missed_lines.join('、') }}</details>
              </td>
              <td>{{ item.module }}</td>
              <td>{{ item.line_coverage ? `${item.line_coverage.covered}/${item.line_coverage.covered + item.line_coverage.missed}` : '—' }}
                · {{ rate(item.line_coverage) }}</td>
              <td>{{ item.branch_coverage ? `${item.branch_coverage.covered}/${item.branch_coverage.covered + item.branch_coverage.missed}` : '—' }}
                · {{ rate(item.branch_coverage) }}</td>
              <td>{{ item.case_ids.join('、') }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section v-if="inconsistentRows.length" class="notice" aria-label="逐条与合跑结果不一致">
        <strong>{{ inconsistentRows.length }} 条单测的逐条与合跑结论不一致</strong>
        <p>这些用例需要核对线程时序、共享状态或断言稳定性；逐条结果暂不因合跑通过而改写。</p>
        <p>{{ inconsistentRows.map((row) => row.case_id).join('、') }}</p>
      </section>

      <div class="filter-bar" aria-label="筛选单元测试结果">
        <button v-for="item in filters" :key="item.key" type="button"
          :class="{ active: filter === item.key }" :aria-pressed="filter === item.key"
          @click="filter = item.key">
          {{ item.label }} <span>{{ item.count }}</span>
        </button>
      </div>
      <p v-if="!rows.length" class="empty-state">此轮没有需要隔离单测的候选用例。</p>
      <p v-else-if="!visibleRows.length" class="empty-state">当前筛选下没有用例。</p>
      <div v-else class="case-list">
        <article v-for="row in visibleRows" :key="`${row.repo}-${row.case_id}`" class="case-card">
          <div class="case-main">
            <div class="case-title">
              <span class="case-id">{{ row.case_id }}</span>
              <span v-if="row.interface_id || row.result?.interface_id" class="interface-id">{{ row.interface_id || row.result?.interface_id }}</span>
              <strong>{{ row.title || row.result?.title || row.test_class || '单元测试候选用例' }}</strong>
            </div>
            <div class="case-outcome">
              <span class="status" :class="statusTone(row.status)">
                {{ row.status === 'failed' && (row.errors ?? 0) > 0 ? '测试运行异常' : statusLabel(row.status) }}
              </span>
              <span v-if="row.consistency_warning" class="status warning">逐条与合跑不一致</span>
              <span v-if="meaningfulCoverage(row)" class="coverage">单次运行已记录 JaCoCo</span>
              <span v-else class="coverage unavailable">单次运行无有效覆盖率</span>
              <button type="button" @click="toggle(row.case_id)">
                {{ expanded.has(row.case_id) ? '收起详情' : '查看详情' }}
              </button>
            </div>
          </div>
          <div v-if="expanded.has(row.case_id)" class="case-detail">
            <p><strong>结果原因：</strong>{{ row.reason || '单测断言通过，且命中目标源码证据行。' }}</p>
            <p v-if="row.suite_status"><strong>合跑结果：</strong>{{ statusLabel(row.suite_status) }}<span v-if="row.suite_reason">；{{ row.suite_reason }}</span></p>
            <p v-if="row.consistency_warning" class="detail-note"><strong>结果差异：</strong>{{ row.consistency_warning }}</p>
            <p v-if="row.test_class"><strong>测试类：</strong>{{ row.test_class }}</p>
            <p v-if="row.target_class"><strong>目标组件：</strong>{{ row.target_class }}<span v-if="row.target_method">#{{ row.target_method }}</span></p>
            <p v-if="row.gap_kind"><strong>补充的接口缺口：</strong>{{ gapLabel(row.gap_kind) }}。{{ row.gap_reason }}</p>
            <p v-if="row.linked_interface_case_ids?.length">
              <strong>关联接口用例：</strong>
              <span v-for="caseId in row.linked_interface_case_ids" :key="caseId">{{ caseId }}（{{ linkedHttpStatus(caseId) }}） </span>
            </p>
            <p v-else-if="row.source_kind === 'source_branch'"><strong>关联接口用例：</strong>暂无直接对应项。</p>
            <p v-if="row.branch_ids?.length"><strong>目标分支：</strong>{{ row.branch_ids.join('、') }}</p>
            <details v-if="row.request_params || row.mock_conditions || row.expected" class="source-detail">
              <summary>查看输入、桩数据和预期</summary>
              <pre>{{ JSON.stringify({ input: row.request_params, mocks: row.mock_conditions, expected: row.expected }, null, 2) }}</pre>
            </details>
            <p v-if="row.reused_verified_source"><strong>来源：</strong>复用同一源码提交下已验证通过的测试代码，本轮重新执行。</p>
            <p v-if="row.module"><strong>Maven 模块：</strong>{{ row.module.module }}</p>
            <p v-if="row.tests != null">
              <strong>测试记录：</strong>{{ row.tests }} 项，失败 {{ row.failures ?? 0 }}，错误 {{ row.errors ?? 0 }}，跳过 {{ row.skipped ?? 0 }}。
            </p>
            <p v-if="row.covered_evidence_lines"><strong>源码证据：</strong>命中 {{ row.covered_evidence_lines }} 行。</p>
            <p v-if="row.module && !meaningfulCoverage(row)" class="detail-note">
              JaCoCo 曾生成 {{ row.module.line_coverage.covered }}/{{ row.module.line_coverage.covered + row.module.line_coverage.missed }} 行记录，
              但没有对应的实际测试记录，因此不计入有效覆盖率。
            </p>
            <details v-if="row.source" class="source-detail">
              <summary>查看生成的 Java 单测</summary>
              <pre>{{ row.source }}</pre>
            </details>
          </div>
        </article>
      </div>
    </template>
  </div>
</template>

<style scoped>
.unit-report { max-width: 1200px; margin: 0 auto; color: var(--text-1); }
.page-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--space-4); margin-bottom: var(--space-4); }
.page-heading h2 { margin: 0; font-size: 24px; font-weight: 650; }
.page-heading p { margin: var(--space-2) 0 0; color: var(--text-2); }
.page-heading .eyebrow { margin: 0 0 var(--space-2); color: var(--accent); font-size: var(--text-sm); }
.secondary-button, .run-bar button, .case-outcome button { font: inherit; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); padding: var(--space-2) var(--space-3); background: var(--bg-elev); color: var(--accent-strong); cursor: pointer; }
.secondary-button:hover, .run-bar button:hover, .case-outcome button:hover { border-color: var(--accent); }
.run-bar { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3); padding: var(--space-3); margin-bottom: var(--space-4); background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-md); }
.run-bar label { display: flex; align-items: center; gap: var(--space-2); color: var(--text-2); }
.run-bar select { max-width: min(34rem, 70vw); min-width: 15rem; padding: var(--space-2); border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--bg-elev); color: var(--text-1); font: inherit; }
.run-bar > span { color: var(--text-3); }
.metric-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: var(--space-3); }
.metric { display: flex; flex-direction: column; gap: var(--space-1); padding: var(--space-4); background: var(--bg-elev); border: 1px solid var(--border); border-radius: var(--radius-md); }
.metric strong { font-family: var(--font-mono); font-size: 24px; font-weight: 600; }
.metric span { color: var(--text-2); font-size: var(--text-sm); }
.metric.success strong { color: var(--success); }
.metric.warning strong { color: var(--warning); }
.scope-note { color: var(--text-2); margin: var(--space-3) 0; }
.notice { padding: var(--space-3); margin: var(--space-3) 0; background: var(--warning-dim); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-2); }
.repo-line { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; margin: var(--space-4) 0; color: var(--text-2); }
.repo-line strong { color: var(--text-1); }
.repo-line span + span::before { content: '·'; margin-right: var(--space-2); }
.filter-bar { display: flex; flex-wrap: wrap; gap: var(--space-2); margin: var(--space-5) 0 var(--space-3); }
.filter-bar button { font: inherit; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: var(--space-2) var(--space-3); background: var(--bg-elev); color: var(--text-2); cursor: pointer; }
.filter-bar button.active { border-color: var(--accent); color: var(--accent-strong); background: var(--accent-dim); }
.filter-bar span { margin-left: var(--space-1); font-family: var(--font-mono); }
.case-list { display: grid; gap: var(--space-3); }
.case-card { background: var(--bg-elev); border: 1px solid var(--border); border-radius: var(--radius-md); overflow: hidden; }
.case-main { display: flex; justify-content: space-between; gap: var(--space-3); padding: var(--space-4); align-items: center; }
.case-title { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2); min-width: 0; }
.case-id, .interface-id { font-family: var(--font-mono); font-size: var(--text-sm); white-space: nowrap; }
.case-id { color: var(--accent-strong); }
.interface-id { color: var(--text-3); }
.case-title strong { font-weight: 600; }
.case-outcome { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: var(--space-2); white-space: nowrap; }
.status { padding: 4px 8px; border-radius: var(--radius-sm); font-size: var(--text-sm); }
.status.success { color: var(--success); background: var(--success-dim); }
.status.danger { color: var(--danger); background: var(--danger-dim); }
.status.warning { color: var(--warning); background: var(--warning-dim); }
.coverage { color: var(--text-1); font-family: var(--font-mono); font-size: var(--text-sm); }
.coverage.unavailable { color: var(--text-3); font-family: var(--font-sans); }
.class-coverage { margin: var(--space-5) 0; padding: var(--space-4); background: var(--bg-elev); border: 1px solid var(--border); border-radius: var(--radius-md); overflow-x: auto; }
.class-coverage h3 { margin: 0 0 var(--space-3); font-size: 18px; }
.class-coverage table { width: 100%; border-collapse: collapse; text-align: left; font-size: var(--text-sm); }
.class-coverage th, .class-coverage td { padding: var(--space-2); border-bottom: 1px solid var(--border); vertical-align: top; }
.class-coverage th { color: var(--text-2); white-space: nowrap; }
.class-coverage td:first-child { overflow-wrap: anywhere; }
.class-coverage small { display: block; color: var(--text-2); }
.gap-group { margin-top: var(--space-5); }
.gap-group + .gap-group { padding-top: var(--space-4); border-top: 1px solid var(--border); }
.gap-group-heading { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: var(--space-2); }
.gap-group-heading h4 { margin: 0; font-size: var(--text-base); }
.gap-group-heading h4 span { margin-left: var(--space-1); color: var(--text-3); font-family: var(--font-mono); font-weight: 500; }
.gap-group-heading > span { color: var(--text-2); font-size: var(--text-sm); }
.case-detail { border-top: 1px solid var(--border); padding: var(--space-4); background: var(--surface-2); }
.case-detail p { margin: 0 0 var(--space-2); color: var(--text-2); }
.case-detail p strong { color: var(--text-1); }
.case-detail .detail-note { color: var(--warning); }
.source-detail { margin-top: var(--space-3); }
.source-detail summary { cursor: pointer; color: var(--accent-strong); }
.source-detail pre { max-height: 28rem; overflow: auto; padding: var(--space-3); background: var(--bg-elev); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-1); font-family: var(--font-mono); font-size: var(--text-sm); white-space: pre; }
.state-line, .empty-state { padding: var(--space-5); color: var(--text-2); background: var(--bg-elev); border: 1px solid var(--border); border-radius: var(--radius-md); }
.state-line.error { color: var(--danger); }
.empty-state p { margin-bottom: 0; }
@media (max-width: 900px) { .page-heading { flex-direction: column; } .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .case-main { align-items: flex-start; flex-direction: column; } .case-outcome { justify-content: flex-start; white-space: normal; } }
@media (max-width: 560px) { .metric-grid { grid-template-columns: 1fr 1fr; } .run-bar label { flex-direction: column; align-items: flex-start; } }
</style>
