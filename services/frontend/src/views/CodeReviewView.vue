<script setup lang="ts">
// Code Review 页（07 §3.3）：视图一映射表（四态+置信度）+ 视图二风险清单 +
// 视图三遗漏功能清单（三视图同源 review_mapping.json）+ 人工审核面板（F-REVW-06）+
// 视图四·变更范围（C-09 双分支 diff，仅配基线分支的 job 展示；旧产物无键则隐藏）。
// 红线：全部数据来自后端产物；风险/遗漏/变更范围均由后端程序化派生，前端只渲染。
// 人工审核不在 9 节点口径内（D6/D7）：approved 仅记录；rejected 回流 testcase_generate。
import { computed, ref, watch } from 'vue'
import JobPicker from '../components/JobPicker.vue'
import { getRequirementItems, getReviewMapping, postReviewLog } from '../api/client'
import type {
  ChangeScopeEntry,
  RequirementItem,
  ReviewMapping,
  DerivedRisk,
  IncrementalFinding,
  DeterministicReviewCheck,
  MissingFeature,
  RemovedFile,
  UnmatchedChangedFile,
  ReviewLogResponse,
} from '../api/types'
import { MAPPING_STATUS_LABELS } from '../api/labels'

const _jobId = ref('')

// C-16：总览页节点卡跳转携带 jobId（App.vue 动态组件下发）
const props = defineProps<{ initialJobId?: string }>()

const items = ref<RequirementItem[]>([])
const mappings = ref<ReviewMapping[]>([])
const risks = ref<DerivedRisk[]>([])
const deterministicChecks = ref<DeterministicReviewCheck[]>([])
const incrementalFindings = ref<IncrementalFinding[]>([])
const missing = ref<MissingFeature[]>([])
const changeScope = ref<ChangeScopeEntry[]>([])
const removedFiles = ref<RemovedFile[]>([])
const unmatchedFiles = ref<UnmatchedChangedFile[]>([])
const hasChangeScope = ref(false)
const loading = ref(false)
const error = ref('')

const requirementTitle = computed(() => {
  const map = new Map(items.value.map((item) => [item.id, item]))
  return (id: string) => map.get(id)?.title ?? ''
})

const evidenceText = computed(() => {
  return (mapping: ReviewMapping) => {
    const location = mapping.evidence?.code_location
    if (!location) return mapping.evidence?.note ?? ''
    const lines =
      location.start_line != null ? `:${location.start_line}-${location.end_line ?? ''}` : ''
    return `${location.repo}/${location.file}${lines}`
  }
})

async function load(): Promise<void> {
  if (!_jobId.value) return
  loading.value = true
  error.value = ''
  try {
    const [req, review] = await Promise.all([
      getRequirementItems(_jobId.value),
      getReviewMapping(_jobId.value),
    ])
    items.value = req.items
    mappings.value = review.mappings
    risks.value = review.risks
    deterministicChecks.value = review.deterministic_checks ?? []
    incrementalFindings.value = review.incremental_findings ?? []
    missing.value = review.missing_features
    // C-09：旧产物无键 → 空数组（视图四隐藏）；有键但为空 = 配了基线但无变更
    changeScope.value = review.change_scope ?? []
    removedFiles.value = review.removed_files ?? []
    unmatchedFiles.value = review.unmatched_changed_files ?? []
    hasChangeScope.value = review.change_scope !== undefined
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Review 产物加载失败'
    items.value = []
    mappings.value = []
    risks.value = []
    deterministicChecks.value = []
    incrementalFindings.value = []
    missing.value = []
    changeScope.value = []
    removedFiles.value = []
    unmatchedFiles.value = []
    hasChangeScope.value = false
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

/* ---- 人工审核面板（F-REVW-06） ---- */

const reviewer = ref('')
const note = ref('')
const submitting = ref<'approved' | 'rejected' | null>(null)
const reviewError = ref('')
const reviewResult = ref<ReviewLogResponse | null>(null)

async function submitDecision(decision: 'approved' | 'rejected'): Promise<void> {
  if (!_jobId.value || submitting.value) return
  submitting.value = decision
  reviewError.value = ''
  try {
    reviewResult.value = await postReviewLog(
      _jobId.value,
      decision,
      reviewer.value.trim() || 'web-user',
      note.value.trim(),
    )
  } catch (err) {
    reviewError.value = err instanceof Error ? err.message : '审核提交失败'
  } finally {
    submitting.value = null
  }
}

function formatConfidence(value: number): string {
  // 展示格式化（保留两位），非计算
  return value.toFixed(2)
}

const uncoveredCount = computed(
  () => changeScope.value.filter((entry) => !entry.covered_by.length).length,
)
</script>

<template>
  <div class="review">
    <JobPicker v-model="_jobId" />

    <p v-if="loading" class="state-line">加载 Review 产物中…</p>
    <p v-else-if="error" class="state-line state-error" role="alert">{{ error }}</p>
    <p v-else-if="!_jobId" class="state-line">请先选择任务。</p>

    <template v-else-if="mappings.length">
      <!-- 覆盖率口径：结构性说明（R-REVW-01），非前端计算数值 -->
      <div class="view-head">
        <span class="tag tag-accent">视图一 · 需求-代码映射表</span>
        <span class="coverage-note">
          覆盖率口径：每条需求条目均有映射行（R-REVW-01 结构性全覆盖），共
          <span class="mono">{{ mappings.length }}</span> 行
        </span>
      </div>

      <div class="card">
        <table class="mapping-table">
          <thead>
            <tr>
              <th scope="col">需求条目</th>
              <th scope="col">接口</th>
              <th scope="col">代码位置</th>
              <th scope="col">判定</th>
              <th scope="col">证据质量</th>
              <th scope="col">置信度</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="mapping in mappings" :key="mapping.requirement_id">
              <td>
                <div class="req-cell">
                  <span class="mono req-id">{{ mapping.requirement_id }}</span>
                  <span class="req-title">{{ requirementTitle(mapping.requirement_id) }}</span>
                </div>
              </td>
              <td>
                <span class="mono">{{ mapping.interface_id ?? '—' }}</span>
              </td>
              <td>
                <span class="mono loc">{{ evidenceText(mapping) || '—' }}</span>
              </td>
              <td>
                <span class="tag" :data-status="mapping.status">
                  {{ MAPPING_STATUS_LABELS[mapping.status] ?? mapping.status }}
                </span>
              </td>
              <td><span class="mono loc">{{ mapping.evidence_quality ?? '—' }}</span></td>
              <td>
                <span class="mono">{{ formatConfidence(mapping.confidence) }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 视图二 / 视图三：后端程序化派生（R-REVW-02/04），与视图一同源 -->
      <div class="derived-stack">
        <div class="card derived-card">
          <header class="derived-title">视图二 · 风险清单（部分实现 / 证据不足）</header>
          <p v-if="!risks.length" class="derived-empty">无风险条目</p>
          <ul v-else class="derived-list">
            <li v-for="risk in risks" :key="risk.requirement_id" class="derived-item">
              <span class="mono req-id">{{ risk.requirement_id }}</span>
              <span v-if="risk.severity" class="tag severity-tag">{{ risk.severity }}</span>
              <span v-if="risk.kind === 'evidence_gap'" class="tag evidence-tag">待核查</span>
              <span>{{ risk.description }}<template v-if="risk.action">；建议：{{ risk.action }}</template></span>
            </li>
          </ul>
        </div>
        <div class="card derived-card">
          <header class="derived-title">视图三 · 遗漏功能清单（程序化派生 · R-REVW-02）</header>
          <p v-if="!missing.length" class="derived-empty">无遗漏条目</p>
          <ul v-else class="derived-list">
            <li v-for="feature in missing" :key="feature.requirement_id" class="derived-item">
              <span class="mono req-id">{{ feature.requirement_id }}</span>
              <span>{{ feature.title }}</span>
            </li>
          </ul>
        </div>
      </div>

      <div v-if="deterministicChecks.length" class="card evidence-card">
        <header class="derived-title">服务端行为证据检查</header>
        <p class="panel-note">程序先检查高风险需求是否存在直接代码或配置证据；“待核查”不会被判为已实现。</p>
        <ul class="derived-list">
          <li v-for="check in deterministicChecks" :key="`${check.requirement_id}-${check.rule_id}`" class="derived-item">
            <span class="mono req-id">{{ check.requirement_id }}</span>
            <span class="tag" :class="check.status === 'needs_verification' ? 'evidence-tag' : 'evidence-ok'">
              {{ check.status === 'needs_verification' ? '待核查' : '已发现证据' }}
            </span>
            <span>{{ check.description }}</span>
          </li>
        </ul>
      </div>

      <div v-if="incrementalFindings.length" class="card evidence-card">
        <header class="derived-title">增量评审优先项</header>
        <ul class="derived-list">
          <li v-for="finding in incrementalFindings" :key="`${finding.kind}-${finding.repo}-${finding.file}`" class="derived-item">
            <span class="tag severity-tag">{{ finding.severity }}</span>
            <span>{{ finding.description }}</span>
            <span class="mono loc">{{ finding.repo }}/{{ finding.file }}</span>
          </li>
        </ul>
      </div>

      <!-- 视图四：变更范围（C-09 双分支 diff，仅配基线分支的 job 展示） -->
      <div v-if="hasChangeScope" class="card change-scope-card">
        <header class="derived-title">
          视图四 · 变更范围（C-09 基线→被测分支 diff · 程序化派生不经 LLM）
        </header>
        <p
          v-if="!changeScope.length && !removedFiles.length && !unmatchedFiles.length"
          class="derived-empty"
        >
          双分支比对完成：无变更文件。
        </p>
        <table v-else class="mapping-table scope-table">
          <thead>
            <tr>
              <th scope="col">接口</th>
              <th scope="col">仓库 / 文件</th>
              <th scope="col">变更</th>
              <th scope="col">需求覆盖</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="entry in changeScope" :key="entry.interface_id">
              <td>
                <span class="mono">{{ entry.interface_id }}</span>
              </td>
              <td>
                <span class="mono loc">{{ entry.repo }}/{{ entry.file }}</span>
              </td>
              <td>
                <span class="tag" :data-change="entry.change">
                  {{ entry.change === 'added' ? '新增' : '修改' }}
                </span>
              </td>
              <td>
                <span v-if="entry.covered_by.length" class="mono">{{
                  entry.covered_by.join('、')
                }}</span>
                <span v-else class="uncovered">未被需求覆盖（变更风险）</span>
              </td>
            </tr>
            <tr v-for="entry in unmatchedFiles" :key="`um-${entry.repo}-${entry.file}`">
              <td><span class="dim">无接口命中</span></td>
              <td>
                <span class="mono loc">{{ entry.repo }}/{{ entry.file }}</span>
              </td>
              <td>
                <span class="tag" :data-change="entry.change">
                  {{ entry.change === 'added' ? '新增' : '修改' }}
                </span>
              </td>
              <td>
                <span class="uncovered"
                  >业务逻辑层变更——接口签名未变但行为可能变更，需人工复核</span
                >
              </td>
            </tr>
            <tr v-for="entry in removedFiles" :key="`rm-${entry.repo}-${entry.file}`">
              <td><span class="dim">—</span></td>
              <td>
                <span class="mono loc">{{ entry.repo }}/{{ entry.file }}</span>
              </td>
              <td><span class="tag" data-change="removed">删除</span></td>
              <td><span class="dim">被测侧已无源码，无法关联接口</span></td>
            </tr>
          </tbody>
        </table>
        <p v-if="uncoveredCount" class="scope-note">
          {{ uncoveredCount }} 个变更接口未被任何需求条目映射——需人工确认变更意图。
        </p>
      </div>
    </template>

    <p v-else class="state-line">code_review 产物尚未生成（需任务跑到该节点之后）。</p>

    <!-- 人工审核面板（不在 9 节点口径内 · D6/D7） -->
    <div class="card review-panel">
      <header class="derived-title">人工审核（F-REVW-06 · 不计入 9 节点自动化率口径 D6/D7）</header>
      <div class="review-form">
        <label class="review-field">
          <span class="field-label">审核人</span>
          <input v-model="reviewer" type="text" placeholder="web-user" />
        </label>
        <label class="review-field review-note">
          <span class="field-label">审核意见</span>
          <textarea
            v-model="note"
            rows="2"
            placeholder="例：映射关系确认无误 / 置信度不足，驳回重新生成"
          />
        </label>
      </div>
      <div class="review-actions">
        <button
          class="primary-btn"
          type="button"
          :disabled="!_jobId || submitting !== null"
          @click="submitDecision('approved')"
        >
          {{ submitting === 'approved' ? '记录中…' : '通过（仅记录）' }}
        </button>
        <button
          class="reject-btn"
          type="button"
          :disabled="!_jobId || submitting !== null"
          @click="submitDecision('rejected')"
        >
          {{ submitting === 'rejected' ? '回流中…' : '驳回（回流用例生成）' }}
        </button>
        <span v-if="reviewResult" class="review-result mono" role="status">
          {{ reviewResult.decision === 'approved' ? '已记录' : `已回流 ${reviewResult.rerun}` }}
          · {{ reviewResult.timestamp.replace('T', ' ').slice(0, 19) }} UTC
        </span>
      </div>
      <p v-if="reviewError" class="state-line state-error" role="alert">{{ reviewError }}</p>
      <p class="panel-note">
        驳回后旧产物归档留痕，任务从 testcase_generate 重跑（可通过「流水线总览」查看进度）。
      </p>
    </div>
  </div>
</template>

<style scoped>
.review {
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

/* 视图四：变更范围（C-09） */
.scope-table td,
.scope-table th {
  text-align: left;
}

.tag[data-change='added'] {
  background: var(--accent-dim);
  color: var(--accent-strong);
}

.tag[data-change='modified'] {
  background: var(--warning-dim, rgba(204, 145, 23, 0.12));
  color: var(--warning);
}

.tag[data-change='removed'] {
  background: var(--danger-dim, rgba(196, 74, 74, 0.12));
  color: var(--danger, #c44a4a);
}

.uncovered {
  color: var(--warning);
  font-size: var(--text-sm);
}

.dim {
  color: var(--text-3);
  font-size: var(--text-sm);
}

.scope-note {
  margin: var(--space-2) 0 0;
  font-size: var(--text-sm);
  color: var(--warning);
}

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
  margin-bottom: var(--space-4);
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

.evidence-tag {
  background: var(--warning-dim);
  border-color: color-mix(in srgb, var(--warning) 30%, transparent);
  color: var(--warning);
}

.evidence-ok {
  background: var(--accent-dim);
  border-color: var(--accent-glow);
  color: var(--accent-strong);
}

.severity-tag {
  background: var(--surface-2);
  border-color: var(--border-strong);
  color: var(--text-1);
  font-weight: 600;
}

.evidence-card {
  margin-top: calc(-1 * var(--space-2));
}

.tag[data-status='implemented'] {
  background: var(--accent-dim);
  border-color: var(--accent-glow);
  color: var(--accent-strong);
}

.tag[data-status='partial'] {
  background: var(--warning-dim);
  border-color: color-mix(in srgb, var(--warning) 30%, transparent);
  color: var(--warning);
}

.tag[data-status='missing'] {
  background: var(--surface-2);
  border-color: var(--border-strong);
  color: var(--text-1);
}

.tag[data-status='undetermined'] {
  color: var(--text-3);
}

/* 映射表 */
.mapping-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}

.mapping-table th {
  text-align: left;
  font-weight: 500;
  font-size: var(--text-xs);
  color: var(--text-3);
  padding: var(--space-2);
  border-bottom: 1px solid var(--border-strong);
}

.mapping-table td {
  padding: var(--space-2) var(--space-2);
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}

.req-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.req-id {
  color: var(--accent-strong);
  font-size: var(--text-xs);
}

.req-title {
  color: var(--text-2);
}

.loc {
  font-size: var(--text-xs);
  color: var(--text-3);
  word-break: break-all;
}

/* 派生卡片（视图二/三）：风险说明通常很长，固定纵向单栏，避免一侧拥挤一侧留白。 */
.derived-stack {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: var(--space-4);
}

.derived-title {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-1);
  margin-bottom: var(--space-3);
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--border);
}

.derived-empty {
  color: var(--text-3);
  font-size: var(--text-sm);
}

.derived-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.derived-item {
  display: flex;
  gap: var(--space-2);
  padding: var(--space-2) 0;
  border-bottom: 1px dashed var(--border);
  font-size: var(--text-sm);
}

.derived-item:last-child {
  border-bottom: none;
}

/* 人工审核面板 */
.review-form {
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}

@media (max-width: 900px) {
  .review-form {
    grid-template-columns: 1fr;
  }
}

.review-field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.field-label {
  font-size: var(--text-xs);
  color: var(--text-3);
}

.review-field input,
.review-field textarea {
  padding: var(--space-2) var(--space-3);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-1);
  font-size: var(--text-md);
  width: 100%;
}

.review-field input:focus,
.review-field textarea:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-dim);
  outline: none;
}

.review-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-3);
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

.reject-btn {
  padding: var(--space-2) var(--space-4);
  background: var(--bg-elev);
  border: 1px solid color-mix(in srgb, var(--warning) 50%, transparent);
  border-radius: var(--radius-sm);
  color: var(--warning);
  font-size: var(--text-md);
  cursor: pointer;
}

.primary-btn:disabled,
.reject-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.review-result {
  font-size: var(--text-xs);
  color: var(--accent-strong);
}

.panel-note {
  margin: var(--space-2) 0 0;
  font-size: var(--text-xs);
  color: var(--text-3);
}
</style>
