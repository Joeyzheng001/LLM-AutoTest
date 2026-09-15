<script setup lang="ts">
// 测试报告页（07 §3.4）：KPI（execution.json summary 后端汇总）+ 失败二态明细（R-EXEC-03）+
// 降级说明（degraded_protocols，F-RPT-03）+ 报告 Markdown 原文（后端唯一事实源）。
// 红线：KPI 数值全部来自后端 summary，前端不计算通过率；报告原文按 Markdown 纯文本呈现（不用 v-html）。
import { computed, ref, watch } from 'vue'
import JobPicker from '../components/JobPicker.vue'
import { getExecution, getReportMd } from '../api/client'
import type { ExecResult, ExecSummary } from '../api/types'

const _jobId = ref('')

const summary = ref<ExecSummary | null>(null)
const results = ref<ExecResult[]>([])
const degradedProtocols = ref<Array<{ protocol: string; mode: string; reason?: string }>>([])
const reportMd = ref('')
const loading = ref(false)
const error = ref('')

const failedResults = computed(() => results.value.filter((result) => !result.passed))
const assertionFailures = computed(() =>
  failedResults.value.filter((result) => result.failure_kind === 'assertion'),
)
const environmentFailures = computed(() =>
  failedResults.value.filter((result) => result.failure_kind === 'environment'),
)
const skippedResults = computed(() =>
  results.value.filter((result) => result.skipped_reason != null),
)

async function load(): Promise<void> {
  if (!_jobId.value) return
  loading.value = true
  error.value = ''
  try {
    const execution = await getExecution(_jobId.value)
    summary.value = execution.summary
    results.value = execution.results
    degradedProtocols.value = execution.degraded_protocols ?? []
    reportMd.value = await getReportMd(_jobId.value)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '执行产物加载失败'
    summary.value = null
    results.value = []
    degradedProtocols.value = []
    reportMd.value = ''
  } finally {
    loading.value = false
  }
}

watch(_jobId, () => void load())
</script>

<template>
  <div class="report">
    <JobPicker v-model="_jobId" />

    <p v-if="loading" class="state-line">加载执行产物中…</p>
    <p v-else-if="error" class="state-line state-error" role="alert">{{ error }}</p>
    <p v-else-if="!_jobId" class="state-line">请先选择任务。</p>

    <template v-else-if="summary">
      <!-- 双产物徽标（R-RPT-02） -->
      <div class="artifact-row">
        <span class="tag tag-accent mono">execution.json</span>
        <span class="tag tag-accent mono">test_report.md</span>
        <span class="artifact-note">数值由 execution.json 程序化汇总，不经 LLM（F-RPT-02）</span>
      </div>

      <!-- KPI：全部来自后端 summary，前端不计算比率 -->
      <div class="kpi-row">
        <div class="kpi-card">
          <span class="kpi-value mono">{{ summary.total }}</span>
          <span class="kpi-label">用例总数</span>
        </div>
        <div class="kpi-card kpi-pass">
          <span class="kpi-value mono">{{ summary.passed }}</span>
          <span class="kpi-label">通过</span>
        </div>
        <div class="kpi-card kpi-assert">
          <span class="kpi-value mono">{{ summary.failed_assertion }}</span>
          <span class="kpi-label">断言失败</span>
        </div>
        <div class="kpi-card kpi-env">
          <span class="kpi-value mono">{{ summary.failed_environment }}</span>
          <span class="kpi-label">环境失败</span>
        </div>
      </div>

      <!-- 降级说明（F-RPT-03 / X-EXEC-03 / D4：如实分列，不计入跑通口径） -->
      <div v-for="item in degradedProtocols" :key="item.protocol" class="hint hint-warning">
        降级 · {{ item.protocol }}：{{ item.mode }}（不计入跑通口径 · D4）<template
          v-if="item.reason"
        >
          —— {{ item.reason }}</template
        >
      </div>
      <div v-if="summary.skipped" class="hint hint-notice">
        {{ summary.skipped }} 条用例跳过执行（脚本不可加载或前置缺失，如实分列）
      </div>

      <!-- 失败二态明细（R-EXEC-03） -->
      <div v-if="assertionFailures.length" class="card">
        <header class="card-title">断言失败明细（进入缺陷清单 · R-DEF-01）</header>
        <table class="result-table">
          <thead>
            <tr>
              <th scope="col">用例</th>
              <th scope="col">接口</th>
              <th scope="col">失败原因</th>
              <th scope="col">超时</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="result in assertionFailures" :key="result.case_id">
              <td>
                <span class="mono">{{ result.case_id }}</span>
              </td>
              <td>
                <span class="mono">{{ result.interface_id }}</span>
              </td>
              <td>{{ result.failure_reason ?? '—' }}</td>
              <td>{{ result.timeout ? '是' : '否' }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="environmentFailures.length" class="card">
        <header class="card-title">
          环境失败明细（隔离至待环境确认 · X-EXEC-01，不入缺陷清单）
        </header>
        <table class="result-table">
          <thead>
            <tr>
              <th scope="col">用例</th>
              <th scope="col">接口</th>
              <th scope="col">失败原因</th>
              <th scope="col">超时</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="result in environmentFailures" :key="result.case_id">
              <td>
                <span class="mono">{{ result.case_id }}</span>
              </td>
              <td>
                <span class="mono">{{ result.interface_id }}</span>
              </td>
              <td>{{ result.failure_reason ?? '—' }}</td>
              <td>{{ result.timeout ? '是' : '否' }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="skippedResults.length" class="card">
        <header class="card-title">跳过用例（如实分列）</header>
        <table class="result-table">
          <thead>
            <tr>
              <th scope="col">用例</th>
              <th scope="col">接口</th>
              <th scope="col">跳过原因</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="result in skippedResults" :key="result.case_id">
              <td>
                <span class="mono">{{ result.case_id }}</span>
              </td>
              <td>
                <span class="mono">{{ result.interface_id }}</span>
              </td>
              <td>{{ result.skipped_reason }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <p v-if="!failedResults.length && !skippedResults.length" class="hint hint-notice">
        无失败与跳过用例。
      </p>

      <!-- 报告原文（后端生成的唯一事实源；纯文本呈现避免 v-html） -->
      <div class="card">
        <header class="card-title">测试报告原文（test_report.md · G5 证据物）</header>
        <pre class="report-md mono">{{ reportMd }}</pre>
      </div>
    </template>

    <p v-else class="state-line">执行产物尚未生成（需任务跑到自动执行节点之后）。</p>
  </div>
</template>

<style scoped>
.report {
  max-width: 1080px;
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

.artifact-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}

.artifact-note {
  font-size: var(--text-xs);
  color: var(--text-3);
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

/* KPI（数值来自后端 summary；不展示前端计算的比率） */
.kpi-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--space-3);
  margin-bottom: var(--space-4);
}

@media (max-width: 720px) {
  .kpi-row {
    grid-template-columns: repeat(2, 1fr);
  }
}

.kpi-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-3) var(--space-4);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.kpi-value {
  font-size: 28px;
  font-weight: 600;
  color: var(--text-1);
  line-height: 1.2;
}

.kpi-pass .kpi-value {
  color: var(--accent-strong);
}

.kpi-assert .kpi-value {
  color: var(--warning);
}

.kpi-env .kpi-value {
  color: var(--text-2);
}

.kpi-label {
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

.hint-notice {
  background: var(--accent-dim);
  color: var(--text-2);
  border: 1px solid var(--border);
}

/* 明细表 */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
  margin-bottom: var(--space-4);
}

.card-title {
  font-size: var(--text-sm);
  font-weight: 600;
  margin-bottom: var(--space-3);
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--border);
}

.result-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}

.result-table th {
  text-align: left;
  font-weight: 500;
  font-size: var(--text-xs);
  color: var(--text-3);
  padding: var(--space-2);
  border-bottom: 1px solid var(--border-strong);
}

.result-table td {
  padding: var(--space-2);
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}

/* 报告原文 */
.report-md {
  margin: 0;
  padding: var(--space-3);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  line-height: 1.7;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 480px;
  overflow-y: auto;
}
</style>
