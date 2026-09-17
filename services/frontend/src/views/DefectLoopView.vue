<script setup lang="ts">
// 缺陷闭环页（07 §3.5）：缺陷清单（双向关联 + 复现脚本引用）+ 生命周期时间线（R-DEF-03）+
// 单条复测（07 §3.5）/ 一键全量复测（F-DEF-02）。边界提示如实分列（X-DEF-01/03、N9）。
import { computed, ref, watch } from 'vue'
import JobPicker from '../components/JobPicker.vue'
import { getDefects, retestFull, retestSingle } from '../api/client'
import type { DefectEntry, RetestResponse } from '../api/types'
import { DEFECT_STATUS_LABELS, DEFECT_EVENT_LABELS } from '../api/labels'
import { navigateTo } from '../api/navigation'

const _jobId = ref('')

// C-16：总览页节点卡跳转携带 jobId（App.vue 动态组件下发）
const props = defineProps<{ initialJobId?: string; viewKey?: string }>()

const defects = ref<DefectEntry[]>([])
const warnings = ref<string[]>([])
const loading = ref(false)
const error = ref('')

const retesting = ref<string | null>(null) // 'full' 或缺陷 id
const retestError = ref('')
const retestResult = ref<RetestResponse | null>(null)
const expandedId = ref('')
const e2eMode = computed(() => props.viewKey === 'e2e-defects')
const visibleDefects = computed(() => defects.value.filter((defect) =>
  e2eMode.value ? defect.case_id.startsWith('E2E-') : !defect.case_id.startsWith('E2E-')
))

async function load(): Promise<void> {
  if (!_jobId.value) return
  loading.value = true
  error.value = ''
  try {
    const payload = await getDefects(_jobId.value)
    defects.value = payload.defects
    warnings.value = payload.warnings ?? []
  } catch (err) {
    error.value = err instanceof Error ? err.message : '缺陷产物加载失败'
    defects.value = []
    warnings.value = []
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

function toggleTimeline(defectId: string): void {
  expandedId.value = expandedId.value === defectId ? '' : defectId
}

function actualText(defect: DefectEntry): string {
  const actual = defect.actual
  if (!actual || (actual.status == null && actual.body == null)) return '历史任务未记录实际响应'
  const status = actual.status == null ? '—' : `HTTP ${actual.status}`
  if (actual.body == null) return status
  const body = typeof actual.body === 'string' ? actual.body : JSON.stringify(actual.body, null, 2)
  return `${status}\n${body}`
}

function viewRetestProgress(): void {
  if (_jobId.value) navigateTo('overview', _jobId.value)
}

function viewRetestRecords(): void {
  if (_jobId.value) navigateTo(e2eMode.value ? 'e2e-defects' : 'interface-defects', _jobId.value)
}

async function retestOne(defectId: string): Promise<void> {
  if (!_jobId.value || retesting.value) return
  retesting.value = defectId
  retestError.value = ''
  retestResult.value = null
  try {
    retestResult.value = await retestSingle(_jobId.value, defectId)
    await load() // 单条同步复测完成后刷新清单（状态流转/时间线更新）
  } catch (err) {
    retestError.value = err instanceof Error ? err.message : '复测请求失败'
  } finally {
    retesting.value = null
  }
}

async function retestAll(): Promise<void> {
  if (!_jobId.value || retesting.value) return
  retesting.value = 'full'
  retestError.value = ''
  retestResult.value = null
  try {
    retestResult.value = await retestFull(_jobId.value)
  } catch (err) {
    retestError.value = err instanceof Error ? err.message : '全量复测请求失败'
  } finally {
    retesting.value = null
  }
}
</script>

<template>
  <div class="defects">
    <JobPicker v-model="_jobId" />

    <p v-if="loading" class="state-line">加载缺陷产物中…</p>
    <p v-else-if="error" class="state-line state-error" role="alert">{{ error }}</p>
    <p v-else-if="!_jobId" class="state-line">请先选择任务。</p>

    <template v-else-if="visibleDefects.length || warnings.length">
      <!-- 全量复测（F-DEF-02：后台 force_node 重跑，进度见流水线总览） -->
      <div v-if="!e2eMode" class="action-row">
        <button class="primary-btn" type="button" :disabled="retesting !== null" @click="retestAll">
          {{ retesting === 'full' ? '已提交全量复测…' : '一键复测全量（F-DEF-02）' }}
        </button>
        <span v-if="retestResult" class="retest-result mono" role="status">
          {{
            retestResult.mode === 'full'
              ? '全量复测已入队（后台重跑回归沉淀节点）'
              : `单条复测完成：${retestResult.retested?.join('、') ?? retestResult.defect_id}`
          }}
        </span>
        <template v-if="retestResult?.mode === 'full'">
          <button class="ghost-btn" type="button" @click="viewRetestProgress">查看进度</button>
          <button class="ghost-btn" type="button" @click="viewRetestRecords">查看复测记录</button>
        </template>
      </div>
      <div v-else class="hint hint-notice">
        E2E 缺陷由浏览器执行产物生成；请从「流水线总览」选择“从自动执行起”重跑，避免把接口复测错误用于前端用例。
      </div>
      <p v-if="retestError" class="state-line state-error" role="alert">{{ retestError }}</p>

      <!-- 处理告警（X-DEF-02 等，如实分列） -->
      <div v-for="(warning, index) in warnings" :key="index" class="hint hint-warning">
        处理告警：{{ warning }}
      </div>

      <!-- 缺陷清单 -->
      <div class="card">
        <header class="card-title">
          {{ e2eMode ? 'E2E 缺陷清单' : '接口缺陷清单' }}（{{ visibleDefects.length }} 条 · 仅断言失败进入 R-DEF-01）
        </header>
        <p v-if="!visibleDefects.length" class="state-line">无{{ e2eMode ? ' E2E' : '接口' }}缺陷条目。</p>
        <div
          v-for="defect in visibleDefects"
          :key="defect.id"
          class="defect-item"
          :class="{ expanded: expandedId === defect.id }"
        >
          <button class="defect-head" type="button" @click="toggleTimeline(defect.id)">
            <span class="mono defect-id">{{ defect.id }}</span>
            <span class="mono defect-link">{{ defect.case_id }} ↔ {{ defect.interface_id }}</span>
            <span class="tag" :data-status="defect.status">
              {{ DEFECT_STATUS_LABELS[defect.status] ?? defect.status }}
            </span>
            <span v-if="defect.is_fallback" class="tag tag-warning">兜底/演示缺陷</span>
            <span v-if="defect.based_on_new_version" class="tag tag-warning">基于新版本</span>
            <span class="expand-hint">{{ expandedId === defect.id ? '收起' : '展开' }}</span>
          </button>

          <div v-if="expandedId === defect.id" class="defect-body">
            <div class="defect-detail-grid">
              <div class="detail-block">
                <span class="detail-label">接口</span>
                <p class="detail-text mono">
                  {{ defect.interface?.protocol || '—' }}
                  {{ defect.interface?.method || '' }} {{ defect.interface?.path || defect.interface_id }}
                </p>
                <p v-if="defect.interface?.summary" class="detail-subtext">{{ defect.interface.summary }}</p>
              </div>
              <div class="detail-block">
                <span class="detail-label">测试用例</span>
                <p class="detail-text"><span class="mono">{{ defect.case_id }}</span> {{ defect.testcase?.title || '历史任务未记录用例标题' }}</p>
                <p v-if="defect.testcase?.category" class="detail-subtext">类型：{{ defect.testcase.category }}</p>
              </div>
              <div class="detail-block">
                <span class="detail-label">预期结果</span>
                <p class="detail-text pre-wrap">{{ defect.testcase?.expected || '历史任务未记录预期结果' }}</p>
              </div>
              <div class="detail-block">
                <span class="detail-label">实际结果</span>
                <pre class="detail-code">{{ actualText(defect) }}</pre>
              </div>
              <div class="detail-block">
                <span class="detail-label">失败原因</span>
                <p class="detail-text">{{ defect.failure_reason }}</p>
              </div>
              <div class="detail-block">
                <span class="detail-label">复现脚本（F-DEF-01 · 用例脚本引用）</span>
                <p class="detail-text mono">{{ defect.reproduce?.script_path ?? '—' }}</p>
              </div>
            </div>

            <!-- 生命周期时间线（R-DEF-03 可审计流转） -->
            <div class="timeline">
              <div
                v-for="(entry, index) in defect.history"
                :key="index"
                class="timeline-item"
                :data-final="index === defect.history.length - 1"
              >
                <span class="timeline-dot" aria-hidden="true" />
                <span class="timeline-event">{{
                  DEFECT_EVENT_LABELS[entry.event] ?? entry.event
                }}</span>
                <span class="mono timeline-time"
                  >{{ entry.timestamp.replace('T', ' ').slice(0, 19) }} UTC</span
                >
              </div>
            </div>

            <div class="defect-actions">
              <button
                class="ghost-btn"
                type="button"
                :disabled="retesting !== null"
                @click="retestOne(defect.id)"
              >
                {{ retesting === defect.id ? '复测中…' : '单条复测' }}
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- 边界提示（07 §3.5 底部：如实分列） -->
      <div class="hint hint-notice">
        复测时代码已变更的缺陷标注「基于新版本」（X-DEF-01）；演示埋入缺陷由独立分支埋入、不进主干（X-DEF-03）。
      </div>
      <div class="hint hint-notice">
        本地缺陷记录不对接 Jira（N9 / D8 口径，D3 已裁决）；缺陷状态流转以 defects.json
        产物为唯一事实源。
      </div>
    </template>

    <p v-else class="state-line">缺陷产物尚未生成（需任务跑到缺陷清单节点之后）。</p>
  </div>
</template>

<style scoped>
.defects {
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

.action-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}

.retest-result {
  font-size: var(--text-xs);
  color: var(--accent-strong);
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

.hint-notice {
  background: var(--accent-dim);
  color: var(--text-2);
  border: 1px solid var(--border);
}

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

.tag {
  display: inline-block;
  padding: 2px var(--space-2);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  border: 1px solid var(--border);
  background: var(--surface-2);
  color: var(--text-2);
  white-space: nowrap;
}

.tag-warning {
  background: var(--warning-dim);
  border-color: color-mix(in srgb, var(--warning) 30%, transparent);
  color: var(--warning);
}

.tag[data-status='discovered'],
.tag[data-status='retesting'] {
  background: var(--accent-dim);
  border-color: var(--accent-glow);
  color: var(--accent-strong);
}

.tag[data-status='passed'],
.tag[data-status='closed_suggested'] {
  background: var(--surface-2);
  border-color: var(--border-strong);
  color: var(--text-1);
}

.tag[data-status='retained'] {
  background: var(--warning-dim);
  border-color: color-mix(in srgb, var(--warning) 30%, transparent);
  color: var(--warning);
}

/* 缺陷条目 */
.defect-item {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  margin-bottom: var(--space-2);
  background: var(--surface-2);
}

.defect-item.expanded {
  border-color: var(--accent-glow);
}

.defect-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-3);
  width: 100%;
  padding: var(--space-3);
  background: transparent;
  border: none;
  cursor: pointer;
  text-align: left;
  font-size: var(--text-sm);
  color: var(--text-1);
}

.defect-id {
  color: var(--accent-strong);
  font-weight: 600;
}

.defect-link {
  font-size: var(--text-xs);
  color: var(--text-3);
}

.expand-hint {
  margin-left: auto;
  font-size: var(--text-xs);
  color: var(--text-3);
}

.defect-body {
  padding: 0 var(--space-3) var(--space-3);
  border-top: 1px dashed var(--border);
}

.defect-detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3);
  padding: var(--space-3) 0;
}

@media (max-width: 860px) {
  .defect-detail-grid {
    grid-template-columns: 1fr;
  }
}

.detail-label {
  display: block;
  font-size: var(--text-xs);
  color: var(--text-3);
  margin-bottom: var(--space-1);
}

.detail-text {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--text-2);
  word-break: break-all;
}

.detail-subtext {
  margin: var(--space-1) 0 0;
  color: var(--text-3);
  font-size: var(--text-xs);
}

.pre-wrap { white-space: pre-wrap; }

.detail-code {
  max-height: 180px;
  margin: 0;
  padding: var(--space-2);
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-2);
  font: var(--text-xs)/1.5 var(--font-mono, monospace);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* 时间线 */
.timeline {
  display: flex;
  flex-direction: column;
  gap: 0;
  padding: var(--space-2) 0 var(--space-3);
}

.timeline-item {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  position: relative;
  padding: 0 0 var(--space-2) var(--space-4);
  font-size: var(--text-sm);
}

.timeline-item::before {
  content: '';
  position: absolute;
  left: 5px;
  top: 14px;
  bottom: 0;
  width: 1px;
  background: var(--border-strong);
}

.timeline-item[data-final='true']::before {
  display: none;
}

.timeline-dot {
  position: absolute;
  left: 0;
  top: 5px;
  width: 11px;
  height: 11px;
  border-radius: 50%;
  background: var(--surface);
  border: 2px solid var(--accent);
}

.timeline-event {
  color: var(--text-1);
}

.timeline-time {
  font-size: var(--text-xs);
  color: var(--text-3);
}

.defect-actions {
  display: flex;
  gap: var(--space-2);
  padding-top: var(--space-2);
}

.primary-btn {
  padding: var(--space-2) var(--space-4);
  background: var(--accent);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  color: #ffffff;
  font-size: var(--text-md);
  cursor: pointer;
}

.primary-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.ghost-btn {
  padding: var(--space-2) var(--space-4);
  background: var(--bg-elev);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  color: var(--text-2);
  cursor: pointer;
  font-size: var(--text-md);
}

.ghost-btn:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
}

.ghost-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
