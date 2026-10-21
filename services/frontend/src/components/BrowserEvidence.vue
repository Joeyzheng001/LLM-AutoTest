<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import type { ExecResult, BrowserStepResult } from '../api/types'
import { getEvidenceBlob } from '../api/client'
const props = defineProps<{ jobId: string; result: ExecResult }>()
const images = ref<Record<string, string>>({})
const errors = ref<Record<string, string>>({})
const raw = ref<Record<string, string>>({})
let generation = 0
function reset(): void {
  generation++
  Object.values(images.value).forEach((url) => URL.revokeObjectURL(url))
  images.value = {}
  errors.value = {}
  raw.value = {}
}
watch(() => [props.jobId, props.result.run_id], reset)
onBeforeUnmount(reset)
function json(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? '未记录'
}
async function loadImage(path?: string): Promise<void> {
  if (!path || images.value[path]) return
  const current = generation
  try {
    const blob = await getEvidenceBlob(props.jobId, path)
    if (current === generation) images.value[path] = URL.createObjectURL(blob)
  } catch (error) {
    if (current === generation) errors.value[path] = String(error)
  }
}
async function expand(event: Event, step: BrowserStepResult): Promise<void> {
  if (!(event.target as HTMLDetailsElement).open) return
  await Promise.all([loadImage(step.before_ref), loadImage(step.after_ref)])
}
async function loadFile(path: string): Promise<void> {
  try {
    raw.value[path] = await (await getEvidenceBlob(props.jobId, path)).text()
  } catch (error) {
    errors.value[path] = String(error)
  }
}
async function download(path: string): Promise<void> {
  try {
    const url = URL.createObjectURL(await getEvidenceBlob(props.jobId, path))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = path.split('/').at(-1) || 'evidence'
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (error) {
    errors.value[path] = String(error)
  }
}
const phases: Record<string, string> = {
  setup: '数据与身份准备',
  precondition_checks: '前置条件检查',
  steps: '业务执行',
  cleanup: '清理',
}
</script>

<template>
  <section class="browser-evidence">
    <p v-if="result.failure_reason" class="reason">{{ result.failure_reason }}</p>
    <p v-if="result.evidence_complete === false" class="incomplete">
      执行证据不完整：{{ result.evidence_errors?.join('；') }}
    </p>
    <p v-if="!result.step_results?.length">尚未进入业务步骤，可查看前置条件与执行原因。</p>
    <details
      v-for="step in result.step_results"
      :key="step.step_id"
      :open="step.step_id === result.failed_step_id"
      :class="['step', step.status]"
      @toggle="expand($event, step)"
    >
      <summary>
        <span class="status">{{
          step.status === 'passed' ? '✓' : step.status === 'failed' ? '✕' : '…'
        }}</span
        ><span>{{ phases[step.phase] }} · {{ step.intent }}</span
        ><small>{{ step.duration_ms ?? '—' }} ms</small>
      </summary>
      <div class="step-body">
        <p v-if="step.error" class="reason">{{ step.error }}</p>
        <div v-if="step.assertion" class="comparison">
          <section>
            <strong>预期</strong>
            <pre>{{ json(step.assertion.expected) }}</pre>
            <p>{{ step.assertion.op }} · {{ step.assertion.path }}</p>
          </section>
          <section>
            <strong>实际</strong>
            <pre>{{ json(step.assertion.actual) }}</pre>
            <p>{{ step.assertion.passed ? '断言通过' : '断言未通过' }}</p>
          </section>
        </div>
        <div class="screenshots">
          <figure
            v-for="item in [
              { label: '操作前', path: step.before_ref },
              { label: '操作后', path: step.after_ref },
            ]"
            :key="item.label"
          >
            <figcaption>{{ item.label }}</figcaption>
            <a
              v-if="item.path && images[item.path]"
              :href="images[item.path]"
              target="_blank"
              rel="noopener"
              ><img :src="images[item.path]" :alt="`${step.intent} · ${item.label}`" loading="lazy"
            /></a>
            <p v-else>{{ item.path ? errors[item.path] || '正在加载截图…' : '未取得截图' }}</p>
          </figure>
        </div>
        <details>
          <summary>操作、输入与定位依据</summary>
          <pre>{{ json(step.step) }}</pre>
          <p v-if="step.frame">{{ json(step.frame) }}</p>
          <pre v-if="step.recovery">重新定位：{{ json(step.recovery) }}</pre>
        </details>
      </div>
    </details>
    <div class="files">
      <button v-if="result.evidence?.network_ref" @click="loadFile(result.evidence.network_ref)">
        查看完整网络记录
      </button>
      <button v-if="result.evidence?.console_ref" @click="loadFile(result.evidence.console_ref)">
        查看控制台记录
      </button>
      <button v-if="result.trace_ref" @click="download(result.trace_ref)">下载浏览器 Trace</button>
    </div>
    <details v-for="(content, path) in raw" :key="path" open>
      <summary>{{ path }}</summary>
      <pre>{{ content }}</pre>
    </details>
    <details v-if="result.evidence_files?.length">
      <summary>全部证据文件 · {{ result.evidence_files.length }}</summary>
      <button
        v-for="path in result.evidence_files"
        :key="path"
        class="file"
        @click="download(path)"
      >
        {{ path.split('/').at(-1) }}
      </button>
    </details>
    <p v-for="(error, path) in errors" :key="path" class="reason">{{ error }}</p>
  </section>
</template>

<style scoped>
.browser-evidence {
  padding: 16px;
}
.step {
  border: 1px solid var(--border);
  border-left: 4px solid #8b99a9;
  border-radius: 8px;
  margin: 12px 0;
  overflow: hidden;
}
.step.passed {
  border-left-color: #22825a;
}
.step.failed {
  border-left-color: #ca4040;
}
summary {
  cursor: pointer;
  padding: 12px;
  display: flex;
  gap: 12px;
  align-items: center;
  background: var(--surface-2);
}
summary small {
  margin-left: auto;
  white-space: nowrap;
}
.step-body {
  padding: 14px;
}
.status {
  font-weight: 700;
}
.comparison,
.screenshots {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.comparison section {
  background: var(--surface-2);
  padding: 12px;
  border-radius: 6px;
}
figure {
  margin: 12px 0;
}
figcaption {
  margin-bottom: 8px;
  color: var(--text-muted);
}
img {
  width: 100%;
  max-height: 520px;
  object-fit: contain;
  object-position: top;
  border: 1px solid var(--border);
  border-radius: 6px;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 550px;
  overflow: auto;
}
.reason,
.incomplete {
  color: #a93636;
  background: #fff2ed;
  padding: 10px;
  border-radius: 6px;
}
.files {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin: 14px 0;
}
button {
  cursor: pointer;
  padding: 7px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
}
.file {
  margin: 5px;
}
@media (max-width: 760px) {
  .comparison,
  .screenshots {
    grid-template-columns: 1fr;
  }
}
</style>
