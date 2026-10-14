<script setup lang="ts">
// Code Review 页（07 §3.3，C-108 两视图）：视图一映射表（四态+置信度三档徽章）+
// 视图二·变更范围（C-09 双分支 diff，仅配基线分支的 job 展示；旧产物无键则隐藏）+
// 人工审核面板（F-REVW-06）。原四视图中的风险/遗漏清单已停产（C-108）。
// 红线：视图二基础事实由后端程序化派生；影响分析/盲区归因/变更总评由 LLM 生成
// （后端已校验编号引用，失败回退纯程序视图），前端只渲染并标注「LLM 生成」。
// 人工审核不在 9 节点口径内（D6/D7）：approved 仅记录；rejected 回流 testcase_generate。
import { computed, ref, watch } from 'vue'
import JobPicker from '../components/JobPicker.vue'
import { getRequirementItems, getReviewMapping, postReviewLog } from '../api/client'
import type {
  ChangeScopeEntry,
  ChangeSummary,
  RequirementItem,
  ReviewMapping,
  RemovedFile,
  UnmatchedChangedFile,
  ReviewLogResponse,
  ReviewEvidence,
  ReviewClaimAssessment,
  ReviewScanCoverage,
  ReviewCallChain,
} from '../api/types'
import { CONFIDENCE_TIER_LABELS, EVIDENCE_QUALITY_LABELS, MAPPING_STATUS_LABELS } from '../api/labels'

const _jobId = ref('')

// C-16：总览页节点卡跳转携带 jobId（App.vue 动态组件下发）
const props = defineProps<{ initialJobId?: string }>()

const items = ref<RequirementItem[]>([])
const mappings = ref<ReviewMapping[]>([])
const evidenceCatalog = ref<ReviewEvidence[]>([])
const callChains = ref<ReviewCallChain[]>([])
const scanCoverage = ref<ReviewScanCoverage | null>(null)
const changeScope = ref<ChangeScopeEntry[]>([])
const changeSummary = ref<ChangeSummary | null>(null)
const removedFiles = ref<RemovedFile[]>([])
const unmatchedFiles = ref<UnmatchedChangedFile[]>([])
const hasChangeScope = ref(false)
const loading = ref(false)
const error = ref('')

const requirementTitle = computed(() => {
  const map = new Map(items.value.map((item) => [item.id, item]))
  return (id: string) => map.get(id)?.title ?? ''
})

const claimById = computed(() => {
  const claims = items.value.flatMap((item) => item.claims ?? [])
  return new Map(claims.map((claim) => [claim.claim_id, claim]))
})

const evidenceById = computed(
  () => new Map(evidenceCatalog.value.map((item) => [item.evidence_id, item])),
)

const callChainByInterface = computed(
  () => new Map(callChains.value.map((item) => [item.interface_id, item])),
)

const evidenceText = computed(() => {
  return (mapping: ReviewMapping) => {
    const refs = mapping.evidence?.refs ?? []
    const locations = refs
      .map((ref) => evidenceById.value.get(ref))
      .filter((item): item is ReviewEvidence => Boolean(item))
      .map((item) => `${item.repo}/${item.file}:${item.start_line}-${item.end_line}`)
    if (locations.length) return [...new Set(locations)].join('；')
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
    evidenceCatalog.value = review.evidence_catalog ?? []
    callChains.value = review.call_chains ?? []
    scanCoverage.value = review.scan_coverage ?? null
    // C-09：优先使用明确的 available 标志；旧产物按 change_scope 字段兼容。
    changeScope.value = review.change_scope ?? []
    changeSummary.value = review.change_summary ?? null
    removedFiles.value = review.removed_files ?? []
    unmatchedFiles.value = review.unmatched_changed_files ?? []
    hasChangeScope.value = review.change_scope_available ?? review.change_scope !== undefined
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Review 产物加载失败'
    items.value = []
    mappings.value = []
    evidenceCatalog.value = []
    callChains.value = []
    scanCoverage.value = null
    changeScope.value = []
    changeSummary.value = null
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

type ConfidenceTier = 'reliable' | 'review' | 'untrusted'

function confidenceTier(value: number): ConfidenceTier {
  // 展示分档（置信度本质是人工复核的优先级信号，三档比两位小数更诚实）：
  // ≥0.7 可抽查；≥0.45 需复核（0.45 为确定性门槛封顶落点）；其余不可信
  if (value >= 0.7) return 'reliable'
  if (value >= 0.45) return 'review'
  return 'untrusted'
}

function confidenceTitle(mapping: ReviewMapping): string {
  const base = `置信度 ${mapping.confidence.toFixed(2)}`
  return mapping.confidence_capped && mapping.raw_confidence != null
    ? `${base}（确定性证据门槛封顶，原始 ${mapping.raw_confidence.toFixed(2)}）`
    : base
}

function evidenceLocation(ref: string): string {
  const item = evidenceById.value.get(ref)
  return item ? `${item.repo}/${item.file}:${item.start_line}-${item.end_line}` : ref
}

function evidenceExcerpt(ref: string): string {
  return evidenceById.value.get(ref)?.excerpt ?? ''
}

function callChainSummary(interfaceIds: string[] | undefined): string {
  const chains = (interfaceIds ?? [])
    .map((id) => callChainByInterface.value.get(id))
    .filter((item): item is ReviewCallChain => Boolean(item))
  if (!chains.length) return ''
  const unresolved = chains.reduce((total, item) => total + (item.unresolved_calls ?? 0), 0)
  return chains.some((item) => item.status === 'partial' || unresolved > 0)
    ? `调用链待核查 · 未解析 ${unresolved} 个`
    : '调用链已确认'
}

function claimCountSummary(mapping: ReviewMapping): string {
  const counts = mapping.claim_counts
  if (!counts) return ''
  return `细分需求点 ${counts.total} 条 · 已实现 ${counts.implemented} · 待核查 ${counts.undetermined} · 缺失 ${counts.missing}`
}

function assessmentSummary(assessment: ReviewClaimAssessment): string {
  const values = [...assessment.unmet_conditions, ...assessment.verification_needed]
  return values.join('；')
}

function claimStatement(claimId: string): string {
  return claimById.value.get(claimId)?.statement ?? ''
}

const uncoveredCount = computed(
  () => changeScope.value.filter((entry) => !entry.covered_by.length).length,
)

const claimTotals = computed(() =>
  mappings.value.reduce(
    (total, mapping) => {
      const counts = mapping.claim_counts
      if (!counts) return total
      total.total += counts.total
      total.implemented += counts.implemented
      total.partial += counts.partial
      total.missing += counts.missing
      total.undetermined += counts.undetermined
      return total
    },
    { total: 0, implemented: 0, partial: 0, missing: 0, undetermined: 0 },
  ),
)
</script>

<template>
  <div class="review">
    <JobPicker v-model="_jobId" />

    <p v-if="loading" class="state-line">加载 Review 产物中…</p>
    <p v-else-if="error" class="state-line state-error" role="alert">{{ error }}</p>
    <p v-else-if="!_jobId" class="state-line">请先选择任务。</p>

    <template v-else-if="mappings.length">
      <div v-if="scanCoverage && !scanCoverage.complete" class="coverage-warning" role="status">
        <strong>证据覆盖未闭合</strong>
        <span>当前“缺失”结论已自动降为“待核查”，请先补齐下面列出的证据范围。</span>
        <ul class="coverage-details">
          <li v-if="scanCoverage.repositories.length">
            未完整扫描仓库：{{
              scanCoverage.repositories.map((repo) => repo.repo || '未命名仓库').join('、')
            }}
          </li>
          <li v-if="scanCoverage.review_evidence_limited">
            代码证据池达到上限：当前最多保留系统允许的证据条目，需扩大证据池或按仓库分批复核。
          </li>
          <li v-if="scanCoverage.interface_evidence_truncated">
            接口实现证据不完整：已覆盖
            {{ scanCoverage.interface_evidence_count ?? 0 }} /
            {{ scanCoverage.interfaces_total ?? 0 }} 个接口。
          </li>
          <li v-if="scanCoverage.interface_evidence_missing?.length">
            缺少接口证据：{{ scanCoverage.interface_evidence_missing.join('、') }}
          </li>
          <li v-if="scanCoverage.call_chain_truncated">
            调用链分析达到上限：最多处理 {{ scanCoverage.call_chain_limit ?? '未知' }} 个接口。
          </li>
          <li v-if="scanCoverage.scan_integrity_complete === false">
            接口扫描完整性未闭合：{{
              (scanCoverage.scan_integrity_reasons ?? []).join('；') || '缺少完整性记录'
            }}
          </li>
        </ul>
      </div>
      <!-- 覆盖率口径：结构性说明（R-REVW-01），非前端计算数值 -->
      <div class="view-head">
        <span class="tag tag-accent">视图一 · 需求-代码映射表</span>
        <span class="coverage-note">
          需求映射完整率：<span class="mono">{{ mappings.length }}/{{ items.length }}</span
          >； 细分需求点评审：<span class="mono">{{ claimTotals.total }}</span> 条； 已实现
          <span class="mono">{{ claimTotals.implemented }}</span
          >， 部分实现 <span class="mono">{{ claimTotals.partial }}</span
          >， 待核查 <span class="mono">{{ claimTotals.undetermined }}</span
          >， 已确认缺失 <span class="mono">{{ claimTotals.missing }}</span>
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
            <template v-for="mapping in mappings" :key="mapping.requirement_id">
              <tr>
                <td>
                  <div class="req-cell">
                    <span class="mono req-id">{{ mapping.requirement_id }}</span>
                    <span class="req-title">{{ requirementTitle(mapping.requirement_id) }}</span>
                    <span v-if="claimCountSummary(mapping)" class="mapping-meta">
                      {{ claimCountSummary(mapping) }}
                    </span>
                  </div>
                </td>
                <td>
                  <span class="mono">{{
                    mapping.interface_ids?.join('、') || mapping.interface_id || '—'
                  }}</span>
                  <span v-if="callChainSummary(mapping.interface_ids)" class="chain-status">
                    {{ callChainSummary(mapping.interface_ids) }}
                  </span>
                </td>
                <td>
                  <span class="mono loc">{{ evidenceText(mapping) || '—' }}</span>
                </td>
                <td>
                  <span class="tag" :data-status="mapping.status">
                    {{ MAPPING_STATUS_LABELS[mapping.status] ?? mapping.status }}
                  </span>
                </td>
                <td>
                  <span class="mono loc">{{
                    EVIDENCE_QUALITY_LABELS[mapping.evidence_quality ?? ''] ?? '—'
                  }}</span>
                </td>
                <td>
                  <span
                    class="tag"
                    :data-confidence="confidenceTier(mapping.confidence)"
                    :title="confidenceTitle(mapping)"
                  >
                    {{ CONFIDENCE_TIER_LABELS[confidenceTier(mapping.confidence)] }}
                  </span>
                </td>
              </tr>
              <tr
                v-if="mapping.claim_assessments?.length"
                :key="`${mapping.requirement_id}-claims`"
                class="claim-detail-row"
              >
                <td colspan="6">
                  <details>
                    <summary>
                      逐条需求点评审与代码证据（{{ mapping.claim_assessments.length }}）
                    </summary>
                    <article
                      v-for="assessment in mapping.claim_assessments"
                      :key="assessment.claim_id"
                      class="claim-assessment"
                    >
                      <header>
                        <span class="mono req-id">{{ assessment.claim_id }}</span>
                        <span class="tag" :data-status="assessment.status">
                          {{ MAPPING_STATUS_LABELS[assessment.status] ?? assessment.status }}
                        </span>
                        <span v-if="assessment.interface_ids.length" class="mono loc">
                          接口：{{ assessment.interface_ids.join('、') }}
                        </span>
                      </header>
                      <p v-if="claimStatement(assessment.claim_id)" class="claim-statement">
                        {{ claimStatement(assessment.claim_id) }}
                      </p>
                      <p v-if="assessment.met_conditions.length" class="assessment-positive">
                        已支持：{{ assessment.met_conditions.join('；') }}
                      </p>
                      <p v-if="assessmentSummary(assessment)" class="assessment-caveat">
                        缺口 / 待核查：{{ assessmentSummary(assessment) }}
                      </p>
                      <div
                        v-for="evidenceRef in assessment.evidence_refs"
                        :key="evidenceRef"
                        class="evidence-reference"
                      >
                        <div class="mono">
                          {{ evidenceRef }} · {{ evidenceLocation(evidenceRef) }}
                        </div>
                        <pre v-if="evidenceExcerpt(evidenceRef)">{{
                          evidenceExcerpt(evidenceRef)
                        }}</pre>
                      </div>
                      <p v-if="!assessment.evidence_refs.length" class="assessment-caveat">
                        没有可引用的代码证据。
                      </p>
                    </article>
                  </details>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>

      <!-- 视图二：变更范围（C-09 双分支 diff；C-108 事实程序化派生 + LLM 影响分析） -->
      <div v-if="hasChangeScope" class="card change-scope-card">
        <header class="derived-title">
          视图二 · 变更范围与存量回归影响（C-09 基线→被测分支 diff · 事实程序化派生 + LLM 回归影响分析）
        </header>
        <div v-if="changeSummary" class="change-summary">
          <span class="tag severity-tag">{{ changeSummary.overall_risk }}</span>
          <strong>{{ changeSummary.nature }}</strong>
          <p class="summary-advice">{{ changeSummary.regression_advice }}</p>
          <small class="dim">变更总评由 LLM 生成，需人工复核</small>
        </div>
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
              <th scope="col">风险</th>
              <th scope="col">需求 / 需求点覆盖</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="entry in changeScope" :key="entry.interface_id">
              <tr>
                <td>
                  <span class="mono">{{ entry.interface_id }}</span>
                </td>
                <td>
                  <span class="mono loc">{{ entry.repo }}/{{ entry.file }}</span>
                  <span v-if="entry.matched_via || entry.evidence_level" class="mapping-meta">
                    {{ entry.matched_via === 'related_file' ? '关联文件命中' : '接口定义命中' }}
                    <template v-if="entry.evidence_level"> · {{ entry.evidence_level }}</template>
                  </span>
                  <span
                    v-for="related in entry.related_changed_files ?? []"
                    :key="related.file"
                    class="mapping-meta"
                  >
                    关联变更：{{ related.file }}（{{ related.role }}）
                  </span>
                </td>
                <td>
                  <span class="tag" :data-change="entry.change">
                    {{ entry.change === 'added' ? '新增' : '修改' }}
                  </span>
                </td>
                <td>
                  <span
                    v-if="entry.impact_analysis?.risk_level ?? entry.risk_level"
                    class="tag severity-tag"
                    :title="entry.impact_analysis ? 'LLM 影响分析评级' : '程序派生回归优先级下限'"
                  >
                    {{ entry.impact_analysis?.risk_level ?? entry.risk_level }}
                  </span>
                  <span v-else class="dim">—</span>
                </td>
                <td>
                  <template v-if="entry.covered_by.length">
                    <span
                      v-for="covered in entry.covered_claims ?? []"
                      :key="covered.requirement_id"
                      class="coverage-group"
                    >
                      <span class="mono">{{ covered.requirement_id }}</span>
                      <span class="tag" :data-status="covered.status">
                        {{ MAPPING_STATUS_LABELS[covered.status] ?? covered.status }}
                      </span>
                      <span v-if="covered.claim_ids.length" class="mono claim-ref">
                        {{ covered.claim_ids.join('、') }}
                      </span>
                    </span>
                    <span v-if="!(entry.covered_claims ?? []).length" class="mono">
                      {{ entry.covered_by.join('、') }}
                    </span>
                  </template>
                  <span v-else class="uncovered">未被需求覆盖（变更风险）</span>
                </td>
              </tr>
              <tr v-if="entry.impact_analysis" class="claim-detail-row">
                <td colspan="5">
                  <div class="impact-detail">
                    <p>
                      <strong>影响分析</strong>
                      <small class="dim">（LLM 生成，需人工复核）</small>：
                      {{ entry.impact_analysis.summary }}
                    </p>
                    <p v-if="entry.impact_analysis.impact_surface?.length">
                      影响面：{{ entry.impact_analysis.impact_surface.join('、') }}
                    </p>
                    <p v-if="entry.impact_analysis.test_suggestion">
                      测试建议：{{ entry.impact_analysis.test_suggestion }}
                    </p>
                  </div>
                </td>
              </tr>
            </template>
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
              <td><span class="dim">—</span></td>
              <td>
                <template v-if="entry.attribution">
                  <span class="uncovered">
                    可能影响：{{
                      (entry.attribution.likely_interfaces ?? []).join('、') || '未知接口'
                    }}<template v-if="entry.attribution.likely_requirements?.length">
                      / {{ entry.attribution.likely_requirements.join('、') }}</template
                    >
                  </span>
                  <span v-if="entry.attribution.rationale" class="mapping-meta">
                    {{ entry.attribution.rationale }}
                    <small class="dim">（LLM 归因，需人工复核）</small>
                  </span>
                </template>
                <span v-else class="uncovered"
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
              <td><span class="dim">—</span></td>
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

/* 视图二：变更范围（C-09） */
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

.coverage-warning {
  margin-bottom: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--warning);
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--warning) 8%, var(--surface));
  color: var(--text-2);
  font-size: var(--text-sm);
  overflow-wrap: anywhere;
}

.coverage-warning > span {
  display: block;
  margin-top: 2px;
}

.coverage-details {
  margin: var(--space-2) 0 0;
  padding-left: var(--space-4);
}

.coverage-details li + li {
  margin-top: 2px;
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

.severity-tag {
  background: var(--surface-2);
  border-color: var(--border-strong);
  color: var(--text-1);
  font-weight: 600;
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

/* 置信度三档（展示分流，hover title 显示原始数值） */
.tag[data-confidence='reliable'] {
  background: var(--accent-dim);
  border-color: var(--accent-glow);
  color: var(--accent-strong);
}

.tag[data-confidence='review'] {
  background: var(--warning-dim);
  border-color: color-mix(in srgb, var(--warning) 30%, transparent);
  color: var(--warning);
}

.tag[data-confidence='untrusted'] {
  background: var(--surface-2);
  border-color: var(--border-strong);
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

.claim-detail-row > td {
  padding: var(--space-2) var(--space-3) var(--space-3);
  background: var(--surface-2);
}
.claim-detail-row summary {
  cursor: pointer;
  color: var(--accent-strong);
  font-size: var(--text-xs);
}
.claim-assessment {
  margin-top: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-left: 2px solid var(--border-strong);
  overflow-wrap: anywhere;
}
.claim-assessment header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}
.claim-assessment p {
  margin: var(--space-1) 0;
  font-size: var(--text-xs);
}
.claim-statement {
  color: var(--text-1);
  font-weight: 500;
}
.assessment-positive {
  color: var(--accent-strong);
}
.assessment-caveat {
  color: var(--warning);
}
.evidence-reference {
  margin-top: var(--space-2);
  color: var(--text-3);
  font-size: var(--text-xs);
}
.evidence-reference pre {
  margin: var(--space-1) 0 0;
  padding: var(--space-2);
  max-height: 9rem;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-2);
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

.mapping-meta {
  display: block;
  color: var(--text-3);
  font-size: var(--text-xs);
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.chain-status {
  display: block;
  margin-top: 3px;
  color: var(--accent-strong);
  font-size: var(--text-xs);
}

.claim-ref {
  color: var(--text-3);
  font-size: var(--text-xs);
}

.coverage-group {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-1);
  margin-bottom: var(--space-1);
}

.loc {
  font-size: var(--text-xs);
  color: var(--text-3);
  word-break: break-all;
}

/* 视图二·变更总评横幅（C-108：LLM 生成，前端标注需人工复核） */
.change-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-2);
  padding: var(--space-3);
  margin-bottom: var(--space-3);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
}

.change-summary .summary-advice {
  flex-basis: 100%;
  margin: 0;
  color: var(--text-2);
}

/* 视图二·接口影响分析展开行 */
.impact-detail {
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  color: var(--text-2);
}

.impact-detail p {
  margin: 0 0 var(--space-1);
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
  flex-wrap: wrap;
  align-items: baseline;
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
