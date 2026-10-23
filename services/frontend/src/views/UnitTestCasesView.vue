<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import JobPicker from '../components/JobPicker.vue'
import { ApiError, getExecution, getTestcases } from '../api/client'
import { navigation, navigateTo } from '../api/navigation'
import type { TestCaseEntry, UnitTestCasePlan, UnitTestOutcome } from '../api/types'

type UnitRow = {
  id: string
  interface_id: string
  title: string
  plan?: UnitTestCasePlan
  interfaceCase?: TestCaseEntry
}

const props = defineProps<{ initialJobId?: string }>()
const jobId = ref(props.initialJobId || '')
const cases = ref<TestCaseEntry[]>([])
const plans = ref<UnitTestCasePlan[] | null>(null)
const outcomes = ref<UnitTestOutcome[]>([])
const loading = ref(false)
const error = ref('')
const planError = ref('')
const search = ref('')
let loadVersion = 0

const byCaseId = computed(() => new Map(outcomes.value.map((item) => [item.case_id, item])))
const candidates = computed<UnitRow[]>(() => {
  const byId = new Map(cases.value.map((item) => [item.id, item]))
  if (plans.value !== null) return plans.value.map((plan) => ({
    id: plan.case_id,
    interface_id: plan.interface_id,
    title: plan.title || byId.get(plan.case_id)?.title || '单元测试场景',
    plan,
    interfaceCase: byId.get(plan.case_id),
  }))
  return cases.value.filter((item) => byCaseId.value.has(item.id) ||
    (item.oracle_status === 'verified' &&
      (Boolean(item.oracle_control_kinds?.length) ||
        item.execution_readiness === 'conditional' ||
        item.state_conditions?.some((condition) => condition.mode === 'controlled'))))
    .map((item) => ({ id: item.id, interface_id: item.interface_id,
      title: item.title, interfaceCase: item }))
})
const visibleCases = computed(() =>
  candidates.value.filter((item) =>
    `${item.id} ${item.interface_id} ${item.title} ${item.plan?.target_class || ''}`
      .toLowerCase()
      .includes(search.value.toLowerCase()),
  ),
)
const generatedCount = computed(() => plans.value === null
  ? outcomes.value.filter((item) => item.source).length
  : plans.value.filter((item) => item.status === 'generated').length)
const passedCount = computed(() => outcomes.value.filter((item) => item.status === 'passed').length)

function statusLabel(outcome?: UnitTestOutcome, plan?: UnitTestCasePlan): string {
  if (!outcome) return plan?.status === 'generated' ? '已生成 · 未运行'
    : plan?.status === 'generation_failed' ? '生成失败'
    : plan?.status === 'generation_deferred' ? '模型连接失败，待生成'
    : plan?.status === 'unsupported' ? '暂不支持' : '等待生成'
  return {
    passed: '通过',
    failed: '未通过',
    not_run: '未运行',
    build_failed: '编译失败',
    generation_failed: '生成失败',
    generation_deferred: '模型连接失败，待生成',
    repair_deferred: '模型连接失败，待修复',
    fixture_failed: '夹具错误',
    unsupported: '暂不支持',
    source_not_covered: '未命中目标源码',
    timed_out: '运行超时',
  }[outcome.status]
}

function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2) || '—'
}

async function load(): Promise<void> {
  const version = ++loadVersion
  cases.value = []
  plans.value = null
  outcomes.value = []
  error.value = ''
  planError.value = ''
  if (!jobId.value) return
  loading.value = true
  try {
    const [testcases, execution] = await Promise.all([
      getTestcases(jobId.value),
      getExecution(jobId.value).catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 404) return null
        throw err
      }),
    ])
    if (version !== loadVersion) return
    cases.value = testcases.cases
    plans.value = testcases.unit_cases ?? null
    planError.value = testcases.unit_generation_error ?? ''
    const plansById = new Map((testcases.unit_cases ?? []).map((plan) => [plan.case_id, plan]))
    outcomes.value = (execution?.unit_test_coverage?.repositories ?? []).flatMap(
      (repo) => repo.cases,
    ).filter((outcome) => {
      const plan = plansById.get(outcome.case_id)
      return !plan?.plan_fingerprint || plan.plan_fingerprint === outcome.plan_fingerprint
    })
  } catch (err) {
    if (version === loadVersion)
      error.value = err instanceof Error ? err.message : '单元测试用例加载失败'
  } finally {
    if (version === loadVersion) loading.value = false
  }
}

watch(
  () => props.initialJobId,
  (value) => {
    if (value && value !== jobId.value) jobId.value = value
  },
)
watch(
  jobId,
  (value) => {
    navigation.jobId = value
    void load()
  },
  { immediate: true },
)
</script>

<template>
  <div class="unit-cases">
    <JobPicker v-model="jobId" />
    <header class="page-head">
      <div>
        <p class="eyebrow">单元测试 · 用例</p>
        <h2>隔离验证的候选场景</h2>
        <p>
          按目标组件查看源码驱动单测，以及它关联的接口用例和补充的验证缺口。执行结果与类级
          JaCoCo 覆盖率在报告中查看；旧任务仍显示历史候选场景。
        </p>
      </div>
      <button v-if="jobId" type="button" @click="navigateTo('unit-report', jobId)">
        查看执行与覆盖率 →
      </button>
    </header>

    <p v-if="loading" class="state" role="status">加载单元测试用例中…</p>
    <p v-else-if="error" class="state error" role="alert">{{ error }}</p>
    <p v-else-if="!jobId" class="state">请先选择任务。</p>
    <template v-else>
      <div class="metrics">
        <div>
          <strong>{{ candidates.length }}</strong
          ><span>候选场景</span>
        </div>
        <div>
          <strong>{{ generatedCount }}</strong
          ><span>已生成 Java 单测</span>
        </div>
        <div>
          <strong>{{ passedCount }}</strong
          ><span>验证通过</span>
        </div>
      </div>
      <div class="toolbar">
        <input
          v-model="search"
          type="search"
          placeholder="搜索用例、接口或场景"
          aria-label="搜索单元测试用例"
        />
        <button type="button" @click="load">刷新</button>
      </div>
      <p v-if="planError" class="state error" role="alert">{{ planError }}</p>
      <p v-if="!candidates.length && !planError" class="state">
        当前任务没有单测候选场景。请确认已启用单元测试，并从「用例生成」节点运行。
      </p>
      <p v-else-if="!visibleCases.length" class="state">没有匹配的用例。</p>
      <div v-else class="case-list">
        <article v-for="item in visibleCases" :key="item.id" class="case-card">
          <div class="case-heading">
            <div>
              <span class="case-id">{{ item.id }} · {{ item.interface_id }}</span>
              <h3>{{ item.title }}</h3>
            </div>
            <span class="status" :class="byCaseId.get(item.id)?.status === 'passed' ? 'passed' : ''">
              {{ statusLabel(byCaseId.get(item.id), item.plan) }}
            </span>
          </div>
          <p v-if="item.interfaceCase?.oracle_scenario" class="description">{{ item.interfaceCase.oracle_scenario }}</p>
          <p v-if="item.plan?.test_class" class="description">
            <strong>测试类：</strong>{{ item.plan.test_class }} · {{ item.plan.module }}
          </p>
          <p v-if="item.plan?.compile_verified" class="description">
            <strong>生成校验：</strong>Java 编译通过<span v-if="item.plan.reused_verified_source"> · 复用同一场景已通过的单测</span>
          </p>
          <p v-if="item.plan?.target_source" class="description">
            <strong>目标组件：</strong>{{ item.plan.target_class || item.plan.target_source }}<span v-if="item.plan.target_method">#{{ item.plan.target_method }}</span>
          </p>
          <p v-if="item.plan" class="description">
            <strong>补充接口测试：</strong>
            {{ item.plan.gap_reason || (item.plan.gap_kind === 'no_http_case' ? '暂无对应的 HTTP 用例' : '验证组件内部行为') }}
            <span v-if="item.plan.linked_interface_case_ids?.length">
              · 关联 {{ item.plan.linked_interface_case_ids.join('、') }}
            </span>
            <span v-else> · 无直接对应的 HTTP 用例</span>
          </p>
          <p v-if="item.plan?.branch_ids?.length" class="description">
            <strong>目标源码分支：</strong>{{ item.plan.branch_ids.join('、') }}
          </p>
          <details v-if="item.plan" class="source">
            <summary>查看输入、桩数据和设计预期</summary>
            <p><strong>方法或请求入参</strong></p>
            <pre>{{ formatJson(item.plan.request_params) }}</pre>
            <p><strong>所需控制条件</strong></p>
            <pre>{{ formatJson(item.plan.mock_conditions) }}</pre>
            <p><strong>预期</strong></p>
            <pre>{{ formatJson(item.plan.expected) }}</pre>
          </details>
          <div v-if="item.interfaceCase?.oracle_control_kinds?.length" class="conditions">
            <span v-for="kind in item.interfaceCase.oracle_control_kinds" :key="kind">{{ kind }}</span>
          </div>
          <p v-if="!item.plan && item.interfaceCase" class="expected">
            <strong>预期：</strong>{{ item.interfaceCase.expected_behavior?.description || item.interfaceCase.expected }}
          </p>
          <p v-if="byCaseId.get(item.id)?.reason || item.plan?.reason" class="reason">
            {{ byCaseId.get(item.id)?.reason || item.plan?.reason }}
          </p>
          <details v-if="item.plan?.source || byCaseId.get(item.id)?.source" class="source">
            <summary>查看生成的 Java 单测</summary>
            <pre>{{ item.plan?.source || byCaseId.get(item.id)?.source }}</pre>
          </details>
        </article>
      </div>
    </template>
  </div>
</template>

<style scoped>
.unit-cases {
  max-width: 1200px;
  margin: 0 auto;
  color: var(--text-1);
}
.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-4);
  margin: var(--space-4) 0;
}
.page-head h2 {
  margin: 0;
  font-size: 24px;
}
.page-head p {
  color: var(--text-2);
  margin: var(--space-2) 0 0;
}
.page-head .eyebrow {
  color: var(--accent);
  margin: 0 0 var(--space-2);
  font-size: var(--text-sm);
}
.page-head button,
.toolbar button {
  flex: 0 0 auto;
  padding: var(--space-2) var(--space-3);
  color: var(--accent-strong);
  background: var(--bg-elev);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  font: inherit;
  cursor: pointer;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--space-3);
  margin-bottom: var(--space-4);
}
.metrics > div {
  display: grid;
  gap: var(--space-1);
  padding: var(--space-4);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}
.metrics strong {
  font-size: 24px;
}
.metrics span {
  color: var(--text-2);
  font-size: var(--text-sm);
}
.toolbar {
  display: flex;
  gap: var(--space-2);
  margin: var(--space-4) 0;
}
.toolbar input {
  flex: 1;
  min-width: 0;
  padding: var(--space-2) var(--space-3);
  color: var(--text-1);
  background: var(--bg-elev);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  font: inherit;
}
.state {
  padding: var(--space-4);
  color: var(--text-2);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}
.state.error,
.reason {
  color: var(--danger);
}
.case-list {
  display: grid;
  gap: var(--space-3);
}
.case-card {
  padding: var(--space-4);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}
.case-heading {
  display: flex;
  justify-content: space-between;
  gap: var(--space-3);
  align-items: flex-start;
}
.case-heading h3 {
  margin: var(--space-1) 0 0;
  font-size: var(--text-md);
}
.case-id {
  color: var(--accent-strong);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
}
.status {
  padding: 4px 8px;
  color: var(--warning);
  background: var(--warning-dim);
  border-radius: var(--radius-sm);
  white-space: nowrap;
  font-size: var(--text-sm);
}
.status.passed {
  color: var(--success);
  background: var(--success-dim);
}
.description,
.expected {
  color: var(--text-2);
}
.conditions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.conditions span {
  padding: 3px 7px;
  color: var(--text-2);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
}
.source {
  margin-top: var(--space-3);
}
.source summary {
  color: var(--accent-strong);
  cursor: pointer;
}
.source pre {
  max-height: 26rem;
  overflow: auto;
  padding: var(--space-3);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
}
@media (max-width: 680px) {
  .page-head {
    flex-direction: column;
  }
  .metrics {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
</style>
