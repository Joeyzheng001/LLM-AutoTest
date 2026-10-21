<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import JobPicker from '../components/JobPicker.vue'
import BrowserEvidence from '../components/BrowserEvidence.vue'
import {
  ApiError,
  getE2eStatus,
  getE2eDesign,
  getE2eExecution,
  getE2eDefects,
  getE2eRegression,
  runE2e,
  exportJobResults,
  saveE2eConfiguration,
} from '../api/client'
import type { E2eDesign, E2eExecution, E2eOperationStatus, DefectEntry } from '../api/types'
import { navigateTo } from '../api/navigation'
const props = defineProps<{ initialJobId?: string; viewKey?: string }>()
const jobId = ref(props.initialJobId || '')
const picker = ref<InstanceType<typeof JobPicker> | null>(null)
const active = ref('cases')
const design = ref<E2eDesign | null>(null),
  execution = ref<E2eExecution | null>(null),
  operation = ref<E2eOperationStatus | null>(null)
const defects = ref<DefectEntry[]>([])
const regression = ref<Awaited<ReturnType<typeof getE2eRegression>>['entries']>([])
const frontUrl = ref('')
const error = ref(''),
  search = ref(''),
  type = ref('all'),
  outcome = ref('all'),
  working = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined,
  sequence = 0
const labels: Record<string, string> = {
  normal: '正常流程',
  boundary: '边界场景',
  exception: '异常分支',
  permission: '权限',
  state_transition: '状态流转',
  consistency: '数据一致性',
}
const outcomes: Record<string, string> = {
  not_executed: '未执行',
  running: '执行中',
  passed: '通过',
  assertion_failed: '业务断言失败',
  environment_failed: '环境失败',
  automation_error: '自动化问题',
  blocked: '前置条件未满足',
  unjudged: '待判定',
}
const cases = computed(() =>
  (design.value?.cases || []).filter(
    (c) =>
      (type.value === 'all' || c.scenario_type === type.value) &&
      `${c.title} ${c.requirement_id} ${c.id}`.includes(search.value),
  ),
)
const results = computed(() =>
  (execution.value?.results || []).filter(
    (r) =>
      (type.value === 'all' || r.scenario_type === type.value) &&
      (outcome.value === 'all' || r.outcome === outcome.value) &&
      `${r.title} ${r.case_id}`.includes(search.value),
  ),
)
const busy = computed(() => working.value || Boolean(operation.value?.running))
watch(
  () => props.initialJobId,
  (v) => {
    if (v) jobId.value = v
  },
)
watch(
  () => props.viewKey,
  (v) => {
    active.value =
      v === 'e2e-execution'
        ? 'report'
        : v === 'e2e-defects'
          ? 'bugs'
          : v === 'e2e-regression'
            ? 'regression'
            : 'cases'
  },
  { immediate: true },
)
async function optional<T>(call: Promise<T>): Promise<T | null> {
  try {
    return await call
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }
}
async function load(): Promise<void> {
  if (!jobId.value) return
  const current = ++sequence,
    id = jobId.value
  if (timer) clearTimeout(timer)
  try {
    const [status, d, r, b, g] = await Promise.all([
      getE2eStatus(id),
      optional(getE2eDesign(id)),
      optional(getE2eExecution(id)),
      optional(getE2eDefects(id)),
      optional(getE2eRegression(id)),
    ])
    if (current !== sequence) return
    operation.value = status
    frontUrl.value = status.configuration?.base_url || frontUrl.value
    design.value = d
    execution.value = r
    defects.value = b?.defects || []
    regression.value = g?.entries || []
    error.value = ''
    if (status.running) timer = setTimeout(() => void load(), 2500)
    else void picker.value?.refresh()
  } catch (e) {
    if (current === sequence) error.value = String(e)
  }
}
watch(
  jobId,
  () => {
    design.value = null
    execution.value = null
    void load()
  },
  { immediate: true },
)
onBeforeUnmount(() => {
  sequence++
  if (timer) clearTimeout(timer)
})
async function start(
  action: 'generate' | 'execute' | 'run' | 'retest' | 'regress',
  options: {
    resume?: boolean
    case_ids?: string[]
    defect_ids?: string[]
    fingerprints?: string[]
  } = {},
): Promise<void> {
  working.value = true
  error.value = ''
  try {
    await runE2e(jobId.value, action, options)
    await load()
  } catch (e) {
    error.value = String(e)
  } finally {
    working.value = false
  }
}
async function exportReport(): Promise<void> {
  try {
    await exportJobResults(jobId.value)
  } catch (e) {
    error.value = String(e)
  }
}
async function saveConfiguration(): Promise<void> {
  try {
    await saveE2eConfiguration(jobId.value, {
      ...operation.value?.configuration,
      base_url: frontUrl.value.trim(),
      generate_cases: true,
      execute_cases: true,
    })
    await load()
  } catch (e) {
    error.value = String(e)
  }
}

function json(value: unknown): string {
  return JSON.stringify(value, null, 2) || '—'
}
</script>

<template>
  <main class="workspace">
    <JobPicker ref="picker" v-model="jobId" :show-node-count="false" />
    <header class="head">
      <div>
        <h1>E2E 业务测试</h1>
        <p>从需求场景到业务断言、逐步截图和缺陷复测</p>
      </div>
      <button :disabled="!jobId" @click="load">刷新</button>
    </header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="operation?.operation?.error" role="alert" class="error">
      {{ operation.operation.error }}
    </p>
    <section v-if="jobId" class="toolbar">
      <input v-model="frontUrl" placeholder="被测前端地址" aria-label="被测前端地址" />
      <button :disabled="busy || !frontUrl.startsWith('http')" @click="saveConfiguration">
        保存 E2E 地址
      </button>
      <button
        :disabled="busy || !operation?.configuration?.base_url"
        @click="start('generate', { resume: false })"
      >
        生成业务用例
      </button>
      <button
        v-if="design?.status === 'incomplete' || design?.status === 'running'"
        :disabled="busy"
        @click="start('generate', { resume: true })"
      >
        继续生成
      </button>
      <button :disabled="busy || !design?.cases?.length" @click="start('execute')">
        执行全部用例
      </button>
      <span v-if="busy" role="status"
        >正在运行 ·
        {{
          operation?.operation?.progress?.intent ||
          design?.progress?.intent ||
          operation?.operation?.operation ||
          '任务处理中'
        }}</span
      >
      <span v-if="busy && design?.progress?.pages_total"
        >{{ design.progress.pages_read }} / {{ design.progress.pages_total }} 页</span
      >
      <span v-else-if="!busy && !operation?.configuration?.base_url"
        >先在<button class="link" @click="navigateTo('config', jobId)">任务配置</button
        >填写被测前端地址</span
      >
    </section>
    <nav class="tabs">
      <button
        v-for="tab in [
          { id: 'cases', label: '业务用例' },
          { id: 'report', label: '执行报告' },
          { id: 'bugs', label: 'Bug 记录' },
          { id: 'regression', label: '回归资产' },
        ]"
        :key="tab.id"
        :class="{ selected: active === tab.id }"
        @click="active = tab.id"
      >
        {{ tab.label }}
      </button>
    </nav>
    <section class="stats">
      <div>
        <strong>{{ design?.coverage?.rules ?? design?.rules?.length ?? '—' }}</strong
        ><span>需求规则</span>
      </div>
      <div>
        <strong>{{ design?.coverage?.scenarios ?? design?.scenarios?.length ?? '—' }}</strong
        ><span>计划场景</span>
      </div>
      <div>
        <strong>{{ design?.cases?.length ?? '—' }}</strong
        ><span>有效用例</span>
      </div>
      <div>
        <strong>{{ execution?.planned_cases ?? execution?.summary?.total ?? '—' }}</strong
        ><span>本次计划用例</span>
      </div>
      <div>
        <strong>{{ execution?.summary?.executed_cases ?? '—' }}</strong
        ><span>已执行用例</span>
      </div>
      <div>
        <strong>{{ execution?.summary?.passed ?? '—' }}</strong
        ><span>通过</span>
      </div>
      <div>
        <strong>{{ execution?.summary?.failed_assertion ?? '—' }}</strong
        ><span>业务失败</span>
      </div>
      <div>
        <strong>{{ execution?.summary?.blocked ?? '—' }}</strong
        ><span>前置阻断</span>
      </div>
      <div>
        <strong>{{ execution?.summary?.pending ?? '—' }}</strong
        ><span>等待执行</span>
      </div>
      <div>
        <strong>{{ execution?.summary?.unjudged ?? '—' }}</strong
        ><span>待判定</span>
      </div>
      <div>
        <strong>{{ execution?.summary?.failed_environment ?? '—' }}</strong
        ><span>环境 / 自动化</span>
      </div>
    </section>
    <section class="filters">
      <input v-model="search" placeholder="搜索功能、需求或用例" /><select
        v-model="type"
        aria-label="场景类型"
      >
        <option value="all">全部场景</option>
        <option v-for="(label, key) in labels" :key="key" :value="key">{{ label }}</option></select
      ><select v-if="active === 'report'" v-model="outcome" aria-label="执行结果">
        <option value="all">全部结果</option>
        <option v-for="(label, key) in outcomes" :key="key" :value="key">{{ label }}</option>
      </select>
    </section>
    <template v-if="active === 'cases'">
      <p v-if="!design">尚未生成业务用例。需求解析完成后可独立生成。</p>
      <details v-if="design?.gaps?.length || design?.exploration_pending?.length" class="gaps">
        <summary>
          尚未完成的设计与探索 ·
          {{ (design?.gaps?.length || 0) + (design?.exploration_pending?.length || 0) }} 项
        </summary>
        <ul>
          <li v-for="(gap, i) in design.gaps" :key="`g${i}`">{{ gap.rule_id }} {{ gap.reason }}</li>
          <li v-for="(gap, i) in design.exploration_pending" :key="`p${i}`">
            {{ gap.url || gap.state }} {{ gap.reason }}
          </li>
        </ul>
      </details>
      <details v-for="item in cases" :key="item.id" :class="['case', item.scenario_type]">
        <summary>
          <span class="tag">{{ labels[item.scenario_type || 'normal'] }}</span
          ><span>{{ item.id }} · {{ item.title }}</span
          ><small>{{ item.priority }} · {{ item.requirement_id }}</small>
        </summary>
        <div class="body">
          <p>目标：{{ item.expected }}</p>
          <p>身份：{{ item.role || 'default' }}</p>
          <p v-if="item.execution_blockers?.length" class="error">
            {{ item.execution_blockers.join('；') }}
          </p>
          <section
            v-for="phase in [
              { key: 'setup', label: '准备', steps: item.setup },
              { key: 'preconditions', label: '前置核对', steps: item.precondition_checks },
              { key: 'steps', label: '业务步骤与断言', steps: item.steps },
              { key: 'cleanup', label: '清理', steps: item.cleanup },
            ]"
            :key="phase.key"
          >
            <h3 v-if="phase.steps?.length">{{ phase.label }}</h3>
            <ol>
              <li v-for="(step, i) in phase.steps" :key="i">
                <strong>{{ step.intent || step.action }}</strong>
                <details>
                  <summary>操作与检查明细</summary>
                  <pre>{{ json(step) }}</pre>
                </details>
              </li>
            </ol>
          </section>
          <button :disabled="busy" @click="start('execute', { case_ids: [item.id] })">
            执行此用例
          </button>
        </div>
      </details>
    </template>
    <template v-if="active === 'report'">
      <p v-if="!execution">尚无 E2E 执行记录。</p>
      <template v-else
        ><p>
          {{ execution.scope === 'selected' ? '本次为选定用例执行' : '本次为全部用例执行' }} · 操作
          {{ execution.summary.steps }} 步 · 断言 {{ execution.summary.assertions }} 条 · 网络请求
          {{ execution.summary.actual_requests }} 次 · 证据不完整
          {{ execution.summary.evidence_incomplete || 0 }} 条
          <button @click="exportReport">导出报告与完整证据包</button>
        </p>
        <details
          v-for="item in results"
          :key="`${item.run_id}-${item.case_id}`"
          :open="item.outcome === 'assertion_failed'"
          :class="['case', item.scenario_type]"
        >
          <summary>
            <span class="result" :class="item.outcome">{{
              outcomes[item.outcome || 'unjudged']
            }}</span
            ><span>{{ item.case_id }} · {{ item.title }}</span
            ><small>{{ labels[item.scenario_type || 'normal'] }} · {{ item.duration_ms }} ms</small>
          </summary>
          <BrowserEvidence :job-id="jobId" :result="item" /></details
      ></template>
    </template>
    <template v-if="active === 'bugs'"
      ><p v-if="!defects.length">当前没有已记录的业务断言缺陷。</p>
      <article v-for="bug in defects" :key="bug.id" class="bug">
        <h2>{{ bug.id }} · {{ bug.testcase?.title || bug.failure_reason }}</h2>
        <p>{{ bug.requirement_id }} · {{ bug.status }} · {{ bug.case_id }}</p>
        <p>{{ bug.failure_reason }}</p>
        <button :disabled="busy" @click="start('retest', { defect_ids: [bug.id] })">
          按原始用例复测
        </button>
        <details>
          <summary>复现步骤与证据</summary>
          <BrowserEvidence
            v-if="bug.execution_snapshot"
            :job-id="jobId"
            :result="bug.execution_snapshot"
          />
          <pre>{{ json(bug.reproduce) }}</pre>
          <pre>{{ json(bug.actual) }}</pre>
        </details>
        <details>
          <summary>处理历史</summary>
          <pre>{{ json(bug.history) }}</pre>
        </details>
      </article></template
    >
    <template v-if="active === 'regression'"
      ><p>同一用例版本至少两次执行通过且证据完整后保存回归资产。</p>
      <p v-if="!regression.length">当前没有满足条件的回归资产。</p>
      <article v-for="entry in regression" :key="entry.case_fingerprint" class="bug">
        <h2>{{ entry.case_snapshot.title }}</h2>
        <button
          :disabled="busy"
          @click="start('regress', { fingerprints: [entry.case_fingerprint] })"
        >
          执行此回归版本
        </button>
        <p>
          {{ entry.case_id }} · {{ entry.successful_runs.length }} 次通过 · 版本 {{ entry.version }}
        </p>
        <details>
          <summary>用例快照与准备策略</summary>
          <pre>{{ json(entry.case_snapshot) }}</pre>
        </details>
      </article></template
    >
  </main>
</template>

<style scoped>
.workspace {
  max-width: 1320px;
  margin: auto;
}
.head,
.toolbar,
.tabs,
.filters {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: 18px 0;
}
.head {
  justify-content: space-between;
}
h1 {
  margin: 0;
  font-size: 24px;
}
.head p {
  color: var(--text-muted);
  margin: 6px 0;
}
.stats {
  display: grid;
  grid-template-columns: repeat(8, minmax(0, 1fr));
  gap: 10px;
  margin: 20px 0;
}
.stats div {
  display: flex;
  flex-direction: column;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 9px;
  padding: 16px;
}
.stats strong {
  font-size: 24px;
}
.stats span {
  font-size: 12px;
  color: var(--text-muted);
}
button,
select,
input {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 9px 13px;
  background: var(--surface);
  color: inherit;
}
button {
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
.selected {
  background: #e7efff;
  color: #245aa3;
  border-color: #7395c3;
}
.case,
.bug,
.gaps {
  border: 1px solid var(--border);
  border-radius: 9px;
  margin: 14px 0;
  background: var(--surface);
  overflow: hidden;
}
.case {
  border-left: 4px solid #3d79bb;
}
.case.boundary {
  border-left-color: #bd8a24;
}
.case.exception,
.case.permission {
  border-left-color: #9253ae;
}
.case.state_transition,
.case.consistency {
  border-left-color: #397e80;
}
.case > summary,
.gaps > summary {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px;
  cursor: pointer;
  background: var(--surface-2);
}
summary small {
  margin-left: auto;
}
.body,
.bug {
  padding: 18px;
}
.tag,
.result {
  padding: 3px 8px;
  border-radius: 5px;
  background: #eaf0f8;
  font-size: 12px;
}
.passed {
  color: #237a53;
}
.assertion_failed {
  color: #b52f39;
}
.error {
  color: #a83333;
  background: #fff0ed;
  padding: 10px;
  border-radius: 6px;
}
.filters input {
  min-width: 250px;
}
.link {
  padding: 3px;
  border: 0;
  color: #245aa3;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 600px;
  overflow: auto;
}
li {
  padding: 6px;
}
h2 {
  font-size: 17px;
}
h3 {
  font-size: 14px;
}
@media (max-width: 1050px) {
  .stats {
    grid-template-columns: repeat(4, 1fr);
  }
}
@media (max-width: 650px) {
  .stats {
    grid-template-columns: repeat(2, 1fr);
  }
  .case > summary {
    flex-wrap: wrap;
  }
  summary small {
    margin-left: 0;
  }
}
</style>
