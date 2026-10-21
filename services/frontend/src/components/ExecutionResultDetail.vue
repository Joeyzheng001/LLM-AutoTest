<script setup lang="ts">
import { computed, ref } from 'vue'
import BrowserEvidence from './BrowserEvidence.vue'
import type { ExecResult } from '../api/types'

const props = defineProps<{
  jobId?: string
  result: ExecResult
  sourceResult?: ExecResult
  screenshotUrl?: string
}>()
const copyMessage = ref('')
const reused = computed(() => props.result.execution_state === 'reused_response')
const request = computed(() => (reused.value ? props.sourceResult?.request : props.result.request))
const requestEvidence = computed(() =>
  reused.value
    ? {
        source_case_id: props.result.source_case_id,
        request: request.value,
        source_params: props.result.source_params,
      }
    : request.value,
)
const hasResponse = computed(
  () => props.result.actual_body !== undefined || props.result.actual_status !== undefined,
)
const responseDuration = computed(() =>
  reused.value ? props.sourceResult?.duration_ms : props.result.duration_ms,
)

function json(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? '未记录'
}

async function copy(value: unknown, label: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(json(value))
    copyMessage.value = `${label}已复制`
  } catch {
    copyMessage.value = '复制失败，请选中文本手动复制'
  }
}
</script>

<template>
  <div class="execution-detail">
    <BrowserEvidence v-if="jobId && result.step_results?.length" :job-id="jobId" :result="result" />
    <template v-else>
      <p v-if="result.failure_reason || result.skipped_reason" class="result-reason">
        {{ result.failure_reason || result.skipped_reason }}
      </p>
      <p v-if="reused" class="source-note">
        复用
        {{
          result.source_case_id
        }}
        的真实响应，本用例没有另发请求。下方展示来源请求及本用例核对结果。
      </p>
      <p v-if="copyMessage" class="copy-message" role="status">{{ copyMessage }}</p>
      <div class="exchange-grid">
        <section class="exchange-panel">
          <header>
            <h4>{{ reused ? '来源请求' : '请求' }}</h4>
            <button
              v-if="requestEvidence !== undefined"
              type="button"
              @click="copy(requestEvidence, '请求')"
            >
              复制请求
            </button>
          </header>
          <template v-if="request">
            <div class="http-meta">
              <strong>{{ request.method || 'HTTP' }}</strong
              ><span>{{ request.attempted ? '已发送' : '未发送 · 请求预览' }}</span>
            </div>
            <p class="request-url mono">{{ request.url || 'URL 未记录' }}</p>
            <template v-if="request.query_params !== undefined">
              <h5>查询参数</h5>
              <pre>{{ json(request.query_params) }}</pre>
            </template>
            <template v-if="request.body !== undefined">
              <h5>请求体</h5>
              <pre>{{ json(request.body) }}</pre>
            </template>
            <details v-if="request.headers" class="headers-detail">
              <summary>请求头（敏感值已脱敏）</summary>
              <pre>{{ json(request.headers) }}</pre>
            </details>
          </template>
          <p v-else class="empty-note">
            {{
              result.execution_state === 'not_executed'
                ? '本用例未发送请求。'
                : '此记录未保存请求明细。'
            }}
          </p>
          <details v-if="reused && result.source_params" class="headers-detail">
            <summary>来源用例参数</summary>
            <pre>{{ json(result.source_params) }}</pre>
          </details>
        </section>
        <section class="exchange-panel">
          <header>
            <h4>实际响应</h4>
            <button
              v-if="hasResponse"
              type="button"
              @click="copy({ status: result.actual_status, body: result.actual_body }, '响应')"
            >
              复制响应
            </button>
          </header>
          <template v-if="hasResponse">
            <div class="http-meta">
              <strong>HTTP {{ result.actual_status ?? '未记录' }}</strong
              ><span v-if="responseDuration !== undefined"
                >{{ reused ? '来源耗时 ' : '' }}{{ responseDuration }} ms</span
              >
            </div>
            <h5>响应体</h5>
            <pre>{{ json(result.actual_body) }}</pre>
          </template>
          <p v-else class="empty-note">
            {{
              result.execution_state === 'not_executed'
                ? '未执行，没有实际响应。'
                : '未留存响应体，可查看下方执行证据。'
            }}
          </p>
        </section>
      </div>
      <section class="assertion-panel">
        <h4>预期与判定依据</h4>
        <p class="expected mono">{{ result.expected || '预期待确认' }}</p>
        <div v-if="result.assertion_results?.length" class="assertion-scroll">
          <table>
            <thead>
              <tr>
                <th>字段</th>
                <th>判断</th>
                <th>预期值</th>
                <th>实际值</th>
                <th>结果</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(item, index) in result.assertion_results" :key="index">
                <td class="mono">{{ item.path }}</td>
                <td class="mono">{{ item.op }}</td>
                <td>
                  <pre>{{ item.expected === undefined ? '—' : json(item.expected) }}</pre>
                </td>
                <td>
                  <pre>{{ item.found ? json(item.actual) : '字段不存在' }}</pre>
                </td>
                <td :class="item.passed ? 'check-pass' : 'check-fail'">
                  {{ item.passed ? '通过' : '未通过' }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else class="empty-note">
          未记录字段断言；仅 HTTP 状态符合预期不等于业务规则已经验证。
        </p>
      </section>
      <section
        v-if="result.execution_blockers?.length || result.missing_control_kinds?.length"
        class="assertion-panel"
      >
        <h4>{{ reused ? '原独立执行所需条件' : '执行所缺条件' }}</h4>
        <p v-if="reused" class="empty-note">
          本次使用来源响应完成核对；以下条件说明该场景独立制造时仍需准备什么。
        </p>
        <ul v-if="result.execution_blockers?.length">
          <li v-for="reason in result.execution_blockers" :key="reason">{{ reason }}</li>
        </ul>
        <p v-if="result.missing_control_kinds?.length">
          需准备：{{ result.missing_control_kinds.join('、') }}
        </p>
      </section>
      <details v-if="result.branch_observation" class="headers-detail">
        <summary>源码路径观察</summary>
        <pre>{{ json(result.branch_observation) }}</pre>
      </details>
      <section v-if="result.evidence?.kind === 'browser'" class="assertion-panel">
        <h4>页面执行证据</h4>
        <a v-if="screenshotUrl" :href="screenshotUrl" target="_blank" rel="noopener"
          >查看执行截图</a
        >
        <pre>{{ json(result.evidence) }}</pre>
      </section>
    </template>
  </div>
</template>

<style scoped>
.execution-detail {
  padding: var(--space-4);
  border-top: 1px solid var(--border);
}
.result-reason,
.source-note {
  margin: 0 0 var(--space-3);
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.source-note {
  color: var(--accent-strong);
}
.copy-message {
  font-size: var(--text-xs);
  color: var(--accent-strong);
}
.exchange-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: var(--space-3);
}
.exchange-panel {
  min-width: 0;
  padding: var(--space-3);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}
header,
.http-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  flex-wrap: wrap;
}
h4 {
  margin: 0;
  font-size: var(--text-sm);
}
h5 {
  margin: var(--space-3) 0 var(--space-1);
  color: var(--text-3);
  font-size: var(--text-xs);
}
button {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 4px 8px;
  background: var(--surface);
  color: var(--accent-strong);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-xs);
}
button:hover {
  border-color: var(--accent-strong);
}
.http-meta {
  margin-top: var(--space-3);
  font-size: var(--text-xs);
  color: var(--text-2);
}
.request-url {
  margin: var(--space-2) 0;
  overflow-wrap: anywhere;
  font-size: var(--text-xs);
}
pre {
  margin: var(--space-1) 0 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  overflow: auto;
  max-height: 360px;
  font-size: var(--text-xs);
  line-height: 1.65;
}
.headers-detail {
  margin-top: var(--space-3);
  font-size: var(--text-xs);
}
summary,
a {
  color: var(--accent-strong);
  cursor: pointer;
}
.assertion-panel {
  margin-top: var(--space-4);
}
.expected,
.empty-note {
  color: var(--text-3);
  font-size: var(--text-xs);
  line-height: 1.6;
}
.assertion-scroll {
  overflow-x: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-xs);
}
th,
td {
  padding: var(--space-2);
  text-align: left;
  vertical-align: top;
  border-bottom: 1px solid var(--border);
}
th {
  color: var(--text-3);
  font-weight: 500;
}
td pre {
  max-height: 160px;
  min-width: 70px;
}
.check-pass {
  color: var(--accent-strong);
  white-space: nowrap;
}
.check-fail {
  color: var(--warning);
  white-space: nowrap;
}
ul {
  padding-left: 20px;
  line-height: 1.7;
  font-size: var(--text-sm);
}
@media (max-width: 760px) {
  .exchange-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
