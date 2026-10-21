<script setup lang="ts">
// 用例生成页（C-10 独立展示屏）：testcases.json 全量用例——
// 三类分列（F-GEN-01）+ 接口/需求双溯源（F-GEN-04）+ 参数/预期 + 脚本可加载性（R-GEN-03）。
// 红线：low_confidence（unknown 参数，X-GEN-03）与 warnings 如实标注，前端不计算。
import { computed, ref, watch } from 'vue'
import JobPicker from '../components/JobPicker.vue'
import { getTestcases, getReviewDiagnostics } from '../api/client'
import type { E2eTestCaseEntry, TestCaseEntry, OracleDiagnostic } from '../api/types'
import { CASE_CATEGORY_LABELS } from '../api/labels'

const _jobId = ref('')

// C-16：总览页节点卡跳转携带 jobId（App.vue 动态组件下发）
const props = defineProps<{ initialJobId?: string; viewKey?: string }>()

const cases = ref<TestCaseEntry[]>([])
const e2eCases = ref<E2eTestCaseEntry[]>([])
const warnings = ref<string[]>([])
const processedWarnings = ref<OracleDiagnostic[]>([])
const loading = ref(false)
const error = ref('')

// 三类分列（F-GEN-01：每接口三类各 ≥1 为生成侧口径，此处仅展示分列）
const byCategory = computed(() => ({
  normal: cases.value.filter((c) => c.category === 'normal'),
  boundary: cases.value.filter((c) => c.category === 'boundary'),
  exception: cases.value.filter((c) => c.category === 'exception'),
}))
const caseType = computed<'api' | 'e2e' | null>(() => {
  if (props.viewKey === 'e2e-cases') return 'e2e'
  if (props.viewKey === 'interface-cases') return 'api'
  return null
})
const visibleCases = computed(() => (caseType.value === 'e2e' ? [] : cases.value))
const visibleE2eCases = computed(() => (caseType.value === 'api' ? [] : e2eCases.value))

async function load(): Promise<void> {
  if (!_jobId.value) return
  loading.value = true
  error.value = ''
  try {
    const [payload, diagnostics] = await Promise.all([
      getTestcases(_jobId.value),
      getReviewDiagnostics(_jobId.value).catch(() => null),
    ])
    cases.value = payload.cases
    e2eCases.value = payload.e2e_cases ?? []
    const byMessage = new Map((diagnostics?.items ?? []).map((item) => [item.message, item]))
    processedWarnings.value = (diagnostics?.items ?? []).filter((item) =>
      ['resolved', 'replaced'].includes(item.status),
    )
    warnings.value = (payload.warnings ?? []).flatMap((message) => {
      const item = byMessage.get(message)
      if (!item) return [message]
      return ['resolved', 'replaced'].includes(item.status)
        ? []
        : [`${item.summary || item.message}；系统处理结果：${item.action}`]
    })
  } catch (err) {
    error.value = err instanceof Error ? err.message : '用例产物加载失败'
    cases.value = []
    e2eCases.value = []
    warnings.value = []
    processedWarnings.value = []
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
watch(_jobId, () => void load())

function paramsText(params: Record<string, unknown> | undefined): string {
  if (!params || !Object.keys(params).length) return '无参数'
  return Object.entries(params)
    .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
    .join(' ')
}
</script>

<template>
  <div class="cases-view">
    <JobPicker v-model="_jobId" />

    <p v-if="loading" class="state-line">加载用例产物中…</p>
    <p v-else-if="error" class="state-line state-error" role="alert">{{ error }}</p>
    <p v-else-if="!_jobId" class="state-line">请先选择任务。</p>

    <template v-else-if="visibleCases.length || visibleE2eCases.length">
      <div class="view-head">
        <span class="tag tag-accent"
          >{{
            caseType === 'e2e' ? 'E2E 用例' : caseType === 'api' ? '接口用例' : '测试用例'
          }}（testcases.json）</span
        >
        <span class="coverage-note">
          共 <span class="mono">{{ visibleCases.length + visibleE2eCases.length }}</span> 条<span
            v-if="caseType !== 'e2e'"
            >（正常 <span class="mono">{{ byCategory.normal.length }}</span> / 边界
            <span class="mono">{{ byCategory.boundary.length }}</span> / 异常
            <span class="mono">{{ byCategory.exception.length }}</span
            >，F-GEN-01 三类）</span
          >
        </span>
      </div>

      <!-- 生成质量告警（X-GEN-01/02 如实标注） -->
      <details v-if="processedWarnings.length" class="hint">
        <summary>系统已处理 {{ processedWarnings.length }} 条历史候选记录</summary>
        <ul>
          <li v-for="item in processedWarnings" :key="item.message">
            {{ item.summary }}：{{ item.action }}
          </li>
        </ul>
      </details>
      <div v-for="(warning, index) in warnings" :key="index" class="hint hint-warning">
        {{ warning }}
      </div>

      <div class="card">
        <table class="case-table">
          <thead>
            <tr>
              <th scope="col">用例</th>
              <th scope="col">类别</th>
              <th scope="col">标题</th>
              <th scope="col">溯源（F-GEN-04）</th>
              <th scope="col">参数与场景条件</th>
              <th scope="col">预期</th>
              <th scope="col">脚本</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in visibleCases" :key="item.id">
              <td>
                <span class="mono case-id">{{ item.id }}</span>
              </td>
              <td>
                <span class="tag" :data-category="item.category">
                  {{ CASE_CATEGORY_LABELS[item.category] ?? item.category }}
                </span>
              </td>
              <td>
                <div class="req-cell">
                  <span>{{ item.title }}</span>
                  <span v-if="item.low_confidence" class="low-conf"
                    >低置信度（unknown 参数，X-GEN-03）</span
                  >
                </div>
              </td>
              <td>
                <div class="req-cell">
                  <span class="mono loc">{{ item.interface_id }}</span>
                  <span v-if="item.requirement_id" class="mono loc">{{ item.requirement_id }}</span>
                  <span v-if="item.claim_id" class="mono loc">{{ item.claim_id }}</span>
                  <span v-if="item.testpoint_id" class="mono loc">{{ item.testpoint_id }}</span>
                </div>
              </td>
              <td>
                <span class="mono loc">{{
                  item.execution_blockers?.length && !Object.keys(item.params ?? {}).length
                    ? '有效请求待补齐'
                    : paramsText(item.params)
                }}</span>
                <div
                  v-for="(condition, index) in item.state_conditions ?? []"
                  :key="index"
                  class="loc"
                >
                  {{ condition.mode === 'integration' ? '联调前提' : '指定状态' }}：{{
                    condition.description
                  }}
                </div>
                <div
                  v-for="(blocker, index) in item.execution_blockers ?? []"
                  :key="`blocker-${index}`"
                  class="state-error"
                >
                  待执行：{{ blocker }}
                </div>
                <div
                  v-if="item.execution_readiness === 'blocked' && !item.execution_blockers?.length"
                  class="loc"
                >
                  设计已保留，执行时检查所需状态控制能力
                </div>
              </td>
              <td>
                <div class="req-cell">
                  <span class="desc">{{
                    item.expected === 'unresolved' ? 'HTTP 响应映射待确认' : item.expected
                  }}</span>
                  <span v-if="item.expected_behavior" class="desc">{{
                    item.expected_behavior.description
                  }}</span>
                  <span v-if="item.assertions?.length" class="mono loc">
                    {{ item.assertions.length }} 条响应字段断言
                  </span>
                  <span v-else class="low-conf">仅状态码断言</span>
                </div>
              </td>
              <td>
                <span v-if="item.loadable" class="mono loc">{{ item.script_path }}</span>
                <span v-else class="state-error">脚本不可加载（R-GEN-03）</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="visibleE2eCases.length" class="card e2e-card">
        <header class="card-title">
          前端 E2E 用例
          <span class="e2e-count mono">{{ visibleE2eCases.length }} 条</span>
        </header>
        <p class="e2e-note">由需求测试点生成，步骤仅使用受限的浏览器动作与语义定位器。</p>
        <article v-for="item in visibleE2eCases" :key="item.id" class="e2e-case">
          <div class="e2e-head">
            <span class="mono case-id">{{ item.id }}</span>
            <span class="tag tag-e2e">前端 E2E</span>
            <span class="mono loc">{{ item.requirement_id }}</span>
            <span v-if="item.claim_id" class="mono loc">{{ item.claim_id }}</span>
            <span v-if="item.testpoint_id" class="mono loc">{{ item.testpoint_id }}</span>
            <strong>{{ item.title }}</strong>
          </div>
          <p v-if="item.preconditions?.length" class="e2e-detail">
            前置：{{ item.preconditions.join('；') }}
          </p>
          <ol class="e2e-steps">
            <li v-for="(step, index) in item.steps" :key="index">
              <span class="mono">{{ step.action }}</span>
              <span v-if="step.selector" class="mono"> · {{ step.selector }}</span>
              <span v-if="step.value"> · {{ step.value }}</span>
            </li>
          </ol>
          <p class="e2e-detail"><strong>预期：</strong>{{ item.expected }}</p>
        </article>
      </div>
    </template>

    <p v-else class="state-line">testcases.json 尚未生成（需任务跑到用例生成节点之后）。</p>
  </div>
</template>

<style scoped>
.cases-view {
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
  font-size: var(--text-xs);
}

.view-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}

.coverage-note {
  font-size: var(--text-xs);
  color: var(--text-3);
}

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

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
  margin-bottom: var(--space-4);
  overflow-x: auto;
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

.tag-e2e {
  background: var(--warning-dim);
  color: var(--warning);
  border-color: color-mix(in srgb, var(--warning) 35%, var(--border));
}
.e2e-count {
  margin-left: var(--space-2);
  color: var(--text-3);
  font-weight: 400;
}
.e2e-note,
.e2e-detail {
  color: var(--text-2);
  font-size: var(--text-sm);
}
.e2e-case {
  padding: var(--space-3) 0;
  border-top: 1px solid var(--border);
}
.e2e-head {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
  font-size: var(--text-sm);
}
.e2e-steps {
  margin: var(--space-2) 0;
  padding-left: 24px;
  font-size: var(--text-sm);
  color: var(--text-2);
}
.e2e-steps li {
  margin: 4px 0;
}

.tag[data-category='normal'] {
  background: var(--accent-dim);
  border-color: var(--accent-glow);
  color: var(--accent-strong);
}

.tag[data-category='boundary'] {
  background: var(--warning-dim, rgba(204, 145, 23, 0.12));
  border-color: color-mix(in srgb, var(--warning) 30%, transparent);
  color: var(--warning);
}

.tag[data-category='exception'] {
  background: var(--danger-dim, rgba(196, 74, 74, 0.12));
  border-color: color-mix(in srgb, var(--danger, #c44a4a) 30%, transparent);
  color: var(--danger, #c44a4a);
}

.case-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}

.case-table th {
  text-align: left;
  font-weight: 500;
  font-size: var(--text-xs);
  color: var(--text-3);
  padding: var(--space-2);
  border-bottom: 1px solid var(--border-strong);
  white-space: nowrap;
}

.case-table td {
  padding: var(--space-2);
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}

.case-id {
  color: var(--accent-strong);
  font-size: var(--text-xs);
}

.req-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.low-conf {
  font-size: var(--text-xs);
  color: var(--warning);
}

.desc {
  color: var(--text-2);
}

.loc {
  font-size: var(--text-xs);
  color: var(--text-3);
  word-break: break-all;
}
</style>
