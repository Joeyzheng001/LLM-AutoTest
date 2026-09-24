<script setup lang="ts">
// 需求解析页：先展示 requirement_review 的质量结论与澄清风险，
// 再展示 requirement_items 的可执行条目。所有内容均来自后端产物，前端不计算。
import { computed, ref, watch } from 'vue'
import JobPicker from '../components/JobPicker.vue'
import { getRequirementItems, getRequirementReview } from '../api/client'
import type { RequirementItem, RequirementReview, RequirementSource } from '../api/types'

const _jobId = ref('')

// C-16：总览页节点卡跳转携带 jobId（App.vue 动态组件下发）
const props = defineProps<{ initialJobId?: string }>()

const items = ref<RequirementItem[]>([])
const sourceFiles = ref<string[]>([])
const sourceCatalog = ref<RequirementSource[]>([])
const sourceById = computed(
  () => new Map(sourceCatalog.value.map((source) => [source.source_id, source])),
)
const review = ref<RequirementReview | null>(null)
const reviewNote = ref('')
const loading = ref(false)
const error = ref('')

const dimensionOrder = [
  '需求完整性',
  '一致性与逻辑性',
  '可测试性',
  '清晰度与规范性',
  '风险与依赖描述',
]

async function load(): Promise<void> {
  if (!_jobId.value) return
  loading.value = true
  error.value = ''
  reviewNote.value = ''
  try {
    // 历史任务没有 requirement_review 时仍展示条目；新任务一定同时拥有两份产物。
    const [itemsResult, reviewResult] = await Promise.allSettled([
      getRequirementItems(_jobId.value),
      getRequirementReview(_jobId.value),
    ])
    if (itemsResult.status === 'rejected') throw itemsResult.reason
    const payload = itemsResult.value
    items.value = payload.items
    sourceFiles.value = payload.source_files ?? []
    sourceCatalog.value = payload.source_catalog ?? []
    if (reviewResult.status === 'fulfilled') review.value = reviewResult.value
    else {
      review.value = null
      reviewNote.value = '该任务未生成需求质量评审（兼容历史任务）。'
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '需求解析产物加载失败'
    items.value = []
    sourceFiles.value = []
    sourceCatalog.value = []
    review.value = null
  } finally {
    loading.value = false
  }
}

function sourceLocation(source: RequirementSource): string {
  const version = source.version === 'baseline' ? '基线' : source.version === 'target' ? '被测' : ''
  const parts = [version, source.document].filter(Boolean)
  if (source.pair_index) parts.unshift(`文档对 ${source.pair_index}`)
  if (source.section) parts.push(source.section)
  parts.push(`文本片段 ${source.paragraph_index}.${source.fragment_index}`)
  return parts.join(' · ')
}

function sourceText(ref: string): string {
  const source = sourceById.value.get(ref)
  return source ? `${ref} · ${sourceLocation(source)}` : ref
}

function sourceExcerpt(ref: string): string {
  return sourceById.value.get(ref)?.excerpt ?? ''
}

function sourceExcerptTruncated(ref: string): boolean {
  return sourceById.value.get(ref)?.excerpt_truncated ?? false
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
</script>

<template>
  <div class="req-view">
    <JobPicker v-model="_jobId" />

    <p v-if="loading" class="state-line">加载需求解析产物中…</p>
    <p v-else-if="error" class="state-line state-error" role="alert">{{ error }}</p>
    <p v-else-if="!_jobId" class="state-line">请先选择任务。</p>

    <template v-else-if="items.length || review">
      <div class="view-head">
        <span class="tag tag-accent">需求解析结果</span>
        <span class="coverage-note"
          >共 <span class="mono">{{ items.length }}</span> 条需求条目</span
        >
      </div>

      <section v-if="review" class="quality-card card" aria-labelledby="quality-title">
        <div class="quality-head">
          <div>
            <p class="eyebrow">需求质量评审</p>
            <h2 id="quality-title">{{ review.quality_assessment.grade_name }}</h2>
            <p class="summary">{{ review.summary }}</p>
          </div>
          <div class="score-block" :data-grade="review.quality_grade">
            <span class="score-number mono">{{ review.score }}</span>
            <span class="score-label">/ 100 · {{ review.quality_grade }} 级</span>
          </div>
        </div>

        <div class="dimension-grid">
          <div v-for="name in dimensionOrder" :key="name" class="dimension-item">
            <div class="dimension-label">
              <span>{{ name }}</span
              ><b class="mono">{{ review.dimension_scores[name]?.score ?? 0 }}</b>
            </div>
            <div class="score-track">
              <span :style="{ width: `${review.dimension_scores[name]?.score ?? 0}%` }" />
            </div>
            <p>{{ review.dimension_scores[name]?.reason }}</p>
          </div>
        </div>

        <div
          v-if="review.readable_issue_points.length || review.risk_flags.length"
          class="signal-grid"
        >
          <div v-if="review.readable_issue_points.length" class="signal-panel issue-panel">
            <h3>优先澄清</h3>
            <ul>
              <li v-for="point in review.readable_issue_points" :key="point">{{ point }}</li>
            </ul>
          </div>
          <div v-if="review.risk_flags.length" class="signal-panel">
            <h3>风险与依赖</h3>
            <ul>
              <li v-for="risk in review.risk_flags" :key="`${risk.type}-${risk.desc}`">
                <span class="mono risk-type">{{ risk.type }}</span
                >{{ risk.desc }}
              </li>
            </ul>
          </div>
        </div>

        <details v-if="review.issues.length" class="details-panel">
          <summary>查看全部 {{ review.issues.length }} 项评审问题</summary>
          <div
            v-for="issue in review.issues"
            :key="`${issue.location}-${issue.question}`"
            class="issue-row"
          >
            <span class="tag" :data-priority="issue.priority">{{ issue.priority }}</span>
            <div>
              <b>{{ issue.question }}</b>
              <p>{{ issue.location }} · {{ issue.reason }} · 影响：{{ issue.impact }}</p>
              <p v-if="issue.claim_ids?.length || issue.source_refs?.length" class="issue-trace">
                <span v-if="issue.claim_ids?.length"
                  >关联声明：{{ issue.claim_ids.join('、') }}</span
                >
                <span v-if="issue.source_refs?.length">
                  原文来源：{{ issue.source_refs.map((ref) => sourceText(ref)).join('；') }}
                </span>
              </p>
              <template v-for="sourceRef in issue.source_refs ?? []" :key="sourceRef">
                <blockquote v-if="sourceExcerpt(sourceRef)" class="issue-source-excerpt">
                  {{ sourceExcerpt(sourceRef) }}
                </blockquote>
                <small v-if="sourceExcerptTruncated(sourceRef)" class="excerpt-note">
                  原文较长，当前仅展示前 600 字。
                </small>
              </template>
            </div>
          </div>
        </details>

        <details class="details-panel">
          <summary>测试范围与思维导图</summary>
          <p class="feature-line">
            <b>可直接设计测试：</b>{{ review.testable_features.join('、') || '暂无' }}
          </p>
          <p v-if="review.untestable_items.length" class="feature-line">
            <b>暂不可测：</b>{{ review.untestable_items.join('、') }}
          </p>
          <ul class="mindmap-root">
            <li>
              <b>{{ review.mindmap.主题 }}</b>
              <ul>
                <li v-for="node in review.mindmap.子主题" :key="node.主题">
                  {{ node.主题 }}
                  <ul v-if="node.子主题.length">
                    <li v-for="child in node.子主题" :key="child.主题">{{ child.主题 }}</li>
                  </ul>
                </li>
              </ul>
            </li>
          </ul>
        </details>
      </section>

      <p v-else-if="reviewNote" class="state-line">{{ reviewNote }}</p>

      <!-- 文档来源（C-10 多文档） -->
      <div v-if="sourceFiles.length" class="card source-card">
        <header class="derived-title">来源文档（{{ sourceFiles.length }} 份）</header>
        <ul class="derived-list">
          <li v-for="path in sourceFiles" :key="path" class="derived-item">
            <span class="mono loc">{{ path }}</span>
          </li>
        </ul>
      </div>

      <div v-if="items.length" class="card">
        <header class="section-title">可执行需求条目</header>
        <table class="req-table">
          <thead>
            <tr>
              <th scope="col">需求 ID</th>
              <th scope="col">标题</th>
              <th scope="col">描述</th>
              <th scope="col">优先级</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in items" :key="item.id">
              <td>
                <span class="mono req-id">{{ item.id }}</span>
              </td>
              <td>
                <div class="req-cell">
                  <span>{{ item.title }}</span>
                  <span v-if="item.stub_note" class="stub-note">{{ item.stub_note }}</span>
                </div>
              </td>
              <td>
                <span class="desc">{{ item.description }}</span>
                <details v-if="item.claims?.length" class="claim-details">
                  <summary>原子声明与来源（{{ item.claims.length }}）</summary>
                  <article v-for="claim in item.claims" :key="claim.claim_id" class="claim-item">
                    <header>
                      <span class="mono req-id">{{ claim.claim_id }}</span>
                      <span class="tag">{{ claim.kind }}</span>
                    </header>
                    <p>{{ claim.statement }}</p>
                    <ul v-if="claim.acceptance_criteria?.length">
                      <li v-for="criterion in claim.acceptance_criteria" :key="criterion">
                        {{ criterion }}
                      </li>
                    </ul>
                    <div
                      v-for="sourceRef in claim.source_refs ?? item.source_refs ?? []"
                      :key="sourceRef"
                      class="source-ref"
                    >
                      <span class="mono">{{ sourceText(sourceRef) }}</span>
                      <blockquote v-if="sourceExcerpt(sourceRef)">
                        {{ sourceExcerpt(sourceRef) }}
                      </blockquote>
                      <small v-if="sourceExcerptTruncated(sourceRef)"
                        >原文较长，当前仅展示前 600 字；原文仍以来源文档为准。</small
                      >
                    </div>
                  </article>
                </details>
              </td>
              <td>
                <span v-if="item.priority" class="tag" :data-priority="item.priority">{{
                  item.priority
                }}</span>
                <span v-else class="dim">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <p v-else class="state-line">需求解析产物尚未生成（需任务跑到需求解析节点之后）。</p>
  </div>
</template>

<style scoped>
.req-view {
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

.claim-details {
  margin-top: var(--space-2);
  max-width: 720px;
  color: var(--text-2);
  font-size: var(--text-xs);
}
.claim-details summary {
  cursor: pointer;
  color: var(--accent-strong);
}
.claim-item {
  margin-top: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-left: 2px solid var(--border);
  overflow-wrap: anywhere;
}
.claim-item header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.claim-item p,
.claim-item ul {
  margin: var(--space-1) 0;
}
.source-ref {
  margin-top: var(--space-1);
  color: var(--text-3);
  overflow-wrap: anywhere;
}
.source-ref blockquote {
  margin: var(--space-1) 0 0;
  padding: var(--space-2);
  max-height: 8rem;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  color: var(--text-2);
}
.source-ref small,
.excerpt-note {
  color: var(--text-3);
  font-size: var(--text-xs);
}

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
  margin-bottom: var(--space-4);
}

.quality-card {
  padding: var(--space-5);
}
.quality-head {
  display: flex;
  justify-content: space-between;
  gap: var(--space-5);
  align-items: flex-start;
}
.eyebrow {
  margin: 0 0 var(--space-1);
  color: var(--accent-strong);
  font-size: var(--text-xs);
}
h2,
h3,
p {
  margin-top: 0;
}
h2 {
  margin-bottom: var(--space-2);
  font-size: var(--text-lg);
}
.summary {
  margin-bottom: 0;
  color: var(--text-2);
}
.score-block {
  min-width: 118px;
  padding: var(--space-3);
  border-radius: var(--radius-lg);
  text-align: right;
  background: var(--accent-dim);
  color: var(--accent-strong);
}
.score-block[data-grade='C'],
.score-block[data-grade='D'] {
  background: var(--warning-dim, rgba(204, 145, 23, 0.12));
  color: var(--warning);
}
.score-number {
  display: block;
  font-size: 30px;
  font-weight: 700;
  line-height: 1;
}
.score-label {
  display: block;
  margin-top: var(--space-1);
  font-size: var(--text-xs);
}
.dimension-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(120px, 1fr));
  gap: var(--space-3);
  margin-top: var(--space-5);
}
.dimension-label {
  display: flex;
  justify-content: space-between;
  gap: var(--space-2);
  color: var(--text-2);
  font-size: var(--text-xs);
}
.dimension-item p {
  margin: var(--space-1) 0 0;
  color: var(--text-3);
  font-size: var(--text-xs);
}
.score-track {
  height: 5px;
  margin-top: var(--space-2);
  overflow: hidden;
  border-radius: 4px;
  background: var(--surface-2);
}
.score-track span {
  display: block;
  height: 100%;
  background: var(--accent);
}
.signal-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3);
  margin-top: var(--space-4);
}
.signal-panel {
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.issue-panel {
  border-color: color-mix(in srgb, var(--warning) 35%, var(--border));
}
.signal-panel h3 {
  margin-bottom: var(--space-2);
  font-size: var(--text-sm);
}
.signal-panel ul,
.mindmap-root {
  margin: 0;
  padding-left: 18px;
  color: var(--text-2);
  font-size: var(--text-sm);
}
.signal-panel li + li {
  margin-top: var(--space-1);
}
.risk-type {
  margin-right: var(--space-2);
  color: var(--text-3);
  font-size: var(--text-xs);
}
.details-panel {
  margin-top: var(--space-3);
  padding-top: var(--space-3);
  border-top: 1px solid var(--border);
}
.details-panel summary {
  cursor: pointer;
  color: var(--accent-strong);
  font-size: var(--text-sm);
}
.issue-row {
  display: flex;
  gap: var(--space-2);
  padding: var(--space-3) 0;
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
}
.issue-row p,
.feature-line {
  margin: var(--space-1) 0 0;
  color: var(--text-2);
  font-size: var(--text-sm);
}
.issue-trace {
  display: grid;
  gap: var(--space-1);
  overflow-wrap: anywhere;
  font-size: var(--text-xs) !important;
}
.issue-source-excerpt {
  margin: var(--space-2) 0 0;
  padding: var(--space-2);
  max-height: 8rem;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  color: var(--text-2);
  font-size: var(--text-xs);
}
.mindmap-root {
  margin-top: var(--space-3);
}
.mindmap-root ul {
  padding-left: 18px;
}
.section-title {
  margin-bottom: var(--space-3);
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-1);
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

.tag[data-priority='P0'] {
  background: var(--accent-dim);
  border-color: var(--accent-glow);
  color: var(--accent-strong);
}

.tag[data-priority='P1'] {
  background: var(--warning-dim, rgba(204, 145, 23, 0.12));
  border-color: color-mix(in srgb, var(--warning) 30%, transparent);
  color: var(--warning);
}

.tag[data-priority='高'] {
  color: var(--warning);
  border-color: color-mix(in srgb, var(--warning) 40%, transparent);
}
.tag[data-priority='中'] {
  color: var(--accent-strong);
}

@media (max-width: 860px) {
  .dimension-grid {
    grid-template-columns: 1fr 1fr;
  }
}

@media (max-width: 600px) {
  .quality-head,
  .signal-grid {
    grid-template-columns: 1fr;
    display: grid;
  }
  .score-block {
    text-align: left;
  }
  .req-table {
    min-width: 680px;
  }
  .card {
    overflow-x: auto;
  }
}

.source-card {
  padding-bottom: var(--space-3);
}

.derived-title {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-1);
  margin-bottom: var(--space-3);
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--border);
}

.derived-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.derived-item {
  display: flex;
  gap: var(--space-2);
  padding: var(--space-1) 0;
  font-size: var(--text-sm);
}

.req-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}

.req-table th {
  text-align: left;
  font-weight: 500;
  font-size: var(--text-xs);
  color: var(--text-3);
  padding: var(--space-2);
  border-bottom: 1px solid var(--border-strong);
}

.req-table td {
  padding: var(--space-2);
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}

.req-id {
  color: var(--accent-strong);
  font-size: var(--text-xs);
}

.req-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.stub-note {
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

.dim {
  color: var(--text-3);
  font-size: var(--text-sm);
}
</style>
