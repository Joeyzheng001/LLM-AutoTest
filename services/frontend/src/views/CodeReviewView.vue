<script setup lang="ts">
// Code Review 页（07 §3.3，C-108 两视图）：视图一映射表（四态+置信度三档徽章）+
// 视图二·变更范围（C-09 双分支 diff，仅配基线分支的 job 展示；旧产物无键则隐藏）+
// 人工审核面板（F-REVW-06）。原四视图中的风险/遗漏清单已停产（C-108）。
// 红线：视图二基础事实由后端程序化派生；影响分析/盲区归因/变更总评由 LLM 生成
// （后端已校验编号引用，失败回退纯程序视图），前端只渲染并标注「LLM 生成」。
// 人工审核不在 9 节点口径内（D6/D7）：approved 仅记录；rejected 回流 testcase_generate。
import { computed, ref, watch } from 'vue'
import JobPicker from '../components/JobPicker.vue'
import {
  getRequirementItems,
  getReviewMapping,
  getReviewDiagnostics,
  postReviewLog,
} from '../api/client'
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
  FieldValueContract,
  ReviewTestOracle,
  OracleDiagnostic,
} from '../api/types'
import {
  CONFIDENCE_TIER_LABELS,
  EVIDENCE_QUALITY_LABELS,
  MAPPING_STATUS_LABELS,
} from '../api/labels'

const _jobId = ref('')

// C-16：总览页节点卡跳转携带 jobId（App.vue 动态组件下发）
const props = defineProps<{ initialJobId?: string }>()

const items = ref<RequirementItem[]>([])
const mappings = ref<ReviewMapping[]>([])
const fieldContracts = ref<FieldValueContract[]>([])
const testOracles = ref<ReviewTestOracle[]>([])
const testOracleWarnings = ref<string[]>([])
const oracleDiagnostics = ref<OracleDiagnostic[]>([])
const diagnosticGroups = computed(() =>
  [
    { status: 'candidate_invalid', title: '本轮仍未形成有效预期' },
    { status: 'control_required', title: '运行控制条件未满足' },
    { status: 'execution_pending', title: '异常触发或数据条件未满足' },
    { status: 'response_evidence_missing', title: '响应预期尚未确认' },
    { status: 'framework_boundary', title: 'HTTP 框架边界处理结果' },
    { status: 'resolved', title: '系统已处理 · 正常设计或候选已保留' },
    { status: 'replaced', title: '系统已处理 · 重复候选已替代' },
  ]
    .map((group) => ({
      ...group,
      items: oracleDiagnostics.value.filter((item) => item.status === group.status),
    }))
    .filter((group) => group.items.length),
)
const fieldFilter = ref('')
const visibleFieldContracts = computed(() => {
  const query = fieldFilter.value.trim().toLowerCase()
  const matched = query
    ? fieldContracts.value.filter((item) =>
        `${item.interface_id} ${item.field} ${item.description}`.toLowerCase().includes(query),
      )
    : fieldContracts.value
  return matched.slice(0, 200)
})
const unresolvedFieldCount = computed(
  () => fieldContracts.value.filter((item) => item.value_rule_status === 'unspecified').length,
)
const evidenceCatalog = ref<ReviewEvidence[]>([])
const callChains = ref<ReviewCallChain[]>([])
const callChainFilter = ref('')
const visibleCallChains = computed(() => {
  const query = callChainFilter.value.trim().toLowerCase()
  return query
    ? callChains.value.filter((chain) =>
        `${chain.interface_id} ${chain.entry.repo ?? ''} ${chain.entry.file ?? ''}`
          .toLowerCase()
          .includes(query),
      )
    : callChains.value
})
const visibleCallChainDiagrams = computed(() =>
  visibleCallChains.value.map((chain) => ({ chain, diagram: callChainGraph(chain) })),
)
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
    const [req, review, diagnostics] = await Promise.all([
      getRequirementItems(_jobId.value),
      getReviewMapping(_jobId.value),
      getReviewDiagnostics(_jobId.value).catch(() => null),
    ])
    items.value = req.items
    mappings.value = review.mappings
    fieldContracts.value = review.field_value_contracts ?? []
    testOracles.value = review.test_oracles ?? []
    testOracleWarnings.value = review.test_oracle_warnings ?? []
    oracleDiagnostics.value = diagnostics?.items ?? review.test_oracle_diagnostics ?? []
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
    fieldContracts.value = []
    testOracles.value = []
    testOracleWarnings.value = []
    oracleDiagnostics.value = []
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

type CallChainGraphNode = {
  key: string
  file: string
  role: string
  methodName: string
  startLine: number | null
  endLine: number | null
  depth: number
  x: number
  y: number
}

type CallChainGraphEdge = {
  key: string
  path: string
  callLine?: number
  labelX: number
  labelY: number
  isBackEdge: boolean
}

function callChainGraph(chain: ReviewCallChain): {
  nodes: CallChainGraphNode[]
  edges: CallChainGraphEdge[]
  width: number
  height: number
  hasEdges: boolean
  legacyNames: string[]
} {
  const nodeByKey = new Map<string, CallChainGraphNode>()
  const keyFor = (file: string, line: number | null) => `${file}:${line ?? 'entry'}`
  const addNode = (
    file: string,
    role: string,
    methodName: string | undefined,
    startLine: number | null | undefined,
    endLine: number | null | undefined,
    depth: number,
  ): string => {
    const key = keyFor(file, startLine ?? null)
    const existing = nodeByKey.get(key)
    if (existing) {
      existing.depth = Math.min(existing.depth, Math.max(0, depth))
      if (!existing.methodName && methodName) existing.methodName = methodName
      if (!existing.endLine && endLine) existing.endLine = endLine
      return key
    }
    nodeByKey.set(key, {
      key,
      file,
      role,
      methodName: methodName ?? '',
      startLine: startLine ?? null,
      endLine: endLine ?? null,
      depth: Math.max(0, depth),
      x: 0,
      y: 0,
    })
    return key
  }

  const legacyNames: string[] = []
  const entryFile = chain.entry.file ?? '接口入口'
  addNode(
    entryFile,
    chain.entry.role ?? 'controller',
    chain.entry.method_name,
    chain.entry.start_line,
    chain.entry.end_line,
    0,
  )
  chain.callees.forEach((callee) => {
    if (typeof callee === 'string') {
      legacyNames.push(callee)
      return
    }
    if (!callee.file) return
    addNode(
      callee.file,
      callee.role ?? 'other',
      callee.method_name,
      callee.start_line,
      callee.end_line,
      callee.depth ?? 1,
    )
  })

  const rawEdges = chain.edges ?? []
  rawEdges.forEach((edge) => {
    addNode(
      edge.caller_file,
      edge.caller_role ?? 'other',
      edge.caller_method,
      edge.caller_start_line,
      edge.caller_end_line,
      edge.depth - 1,
    )
    addNode(
      edge.callee_file,
      edge.callee_role ?? 'other',
      edge.callee_method,
      edge.callee_start_line,
      edge.callee_end_line,
      edge.depth,
    )
  })

  const nodes = [...nodeByKey.values()].sort(
    (left, right) =>
      left.depth - right.depth ||
      left.file.localeCompare(right.file) ||
      (left.startLine ?? 0) - (right.startLine ?? 0),
  )
  const columns = new Map<number, CallChainGraphNode[]>()
  nodes.forEach((node) => columns.set(node.depth, [...(columns.get(node.depth) ?? []), node]))
  const nodeWidth = 250
  const nodeHeight = 88
  const columnGap = 112
  const rowGap = 108
  const padding = 24
  let maxDepth = 0
  let maxRows = 1
  columns.forEach((column, depth) => {
    maxDepth = Math.max(maxDepth, depth)
    maxRows = Math.max(maxRows, column.length)
    column.forEach((node, index) => {
      node.x = padding + depth * (nodeWidth + columnGap)
      node.y = padding + index * rowGap
    })
  })
  const positions = new Map(nodes.map((node) => [node.key, node]))
  const edges = rawEdges.flatMap((edge, index) => {
    const from = positions.get(keyFor(edge.caller_file, edge.caller_start_line))
    const to = positions.get(keyFor(edge.callee_file, edge.callee_start_line))
    if (!from || !to) return []
    const x1 = from.x + nodeWidth
    const y1 = from.y + nodeHeight / 2
    const x2 = to.x
    const y2 = to.y + nodeHeight / 2
    const bend = Math.max(40, Math.abs(x2 - x1) / 2)
    return [
      {
        key: `${edge.caller_file}:${edge.caller_start_line}-${edge.callee_file}:${edge.callee_start_line}-${index}`,
        path: `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`,
        callLine: edge.call_line,
        labelX: (x1 + x2) / 2,
        labelY: (y1 + y2) / 2 - 8,
        isBackEdge: to.depth <= from.depth,
      },
    ]
  })
  return {
    nodes,
    edges,
    width: padding * 2 + nodeWidth + maxDepth * (nodeWidth + columnGap),
    height: padding * 2 + maxRows * rowGap,
    hasEdges: edges.length > 0,
    legacyNames,
  }
}

function callChainRole(role: string): string {
  const labels: Record<string, string> = {
    controller: '控制器',
    service: 'Service',
    dao: 'DAO',
    mapper: 'Mapper',
    repository: 'Repository',
    dto: 'DTO',
    listener: '监听器',
    other: '其他源码方法',
  }
  return labels[role.toLowerCase()] ?? role
}

function callChainFileLabel(file: string): string {
  const parts = file.split('/')
  const label = parts.slice(-2).join('/')
  return label.length > 34 ? `…${label.slice(-33)}` : label
}

function callChainMethodLabel(methodName: string): string {
  if (!methodName) return '方法名未记录'
  return methodName.length > 34 ? `${methodName.slice(0, 33)}…` : methodName
}

function callChainNodeLines(node: CallChainGraphNode): string {
  if (!node.startLine) return '源码行未记录'
  return `源码行 ${node.startLine}${node.endLine ? `–${node.endLine}` : ''}`
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
      <div class="view-head">
        <span class="tag tag-accent">Code Review · 源码测试设计</span>
        <span class="coverage-note"
          >已确认 {{ testOracles.length }} 条条件化预期；执行准备单独检查</span
        >
      </div>
      <details
        v-for="group in diagnosticGroups"
        :key="`${_jobId}-${group.status}`"
        class="coverage-warning"
        :class="{ 'diagnostic-done': ['resolved', 'replaced'].includes(group.status) }"
        :open="!['resolved', 'replaced'].includes(group.status)"
        role="status"
      >
        <summary>
          <strong>{{ group.title }}</strong> · {{ group.items.length }} 条
        </summary>
        <ul class="coverage-details">
          <li v-for="item in group.items" :key="`${item.interface_id}-${item.message}`">
            {{ item.summary || item.message }}<br />系统处理结果：{{ item.action }}
            <p v-if="item.oracle_ids?.length" class="diagnostic-links">
              关联设计：{{ item.oracle_ids.join('、') }}
            </p>
            <details
              v-if="item.summary && item.summary !== item.message"
              class="diagnostic-original"
            >
              <summary>查看原始过程记录</summary>
              {{ item.message }}
            </details>
          </li>
        </ul>
      </details>
      <div
        v-if="!oracleDiagnostics.length && testOracleWarnings.length"
        class="coverage-warning"
        role="status"
      >
        <strong>未进入执行清单的分支</strong>
        <ul class="coverage-details">
          <li v-for="warning in testOracleWarnings" :key="warning">{{ warning }}</li>
        </ul>
      </div>
      <div v-if="testOracles.length" class="card">
        <table class="mapping-table">
          <thead>
            <tr>
              <th scope="col">接口 / 场景</th>
              <th scope="col">源码确认的预期</th>
              <th scope="col">前置条件</th>
              <th scope="col">源码证据</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="oracle in testOracles" :key="oracle.oracle_id">
              <td>
                <span class="mono">{{ oracle.interface_id }}</span> · {{ oracle.category }}<br />{{
                  oracle.scenario
                }}
              </td>
              <td>
                <span class="mono">{{
                  oracle.expected_status === 'unresolved'
                    ? 'HTTP 响应映射待确认'
                    : oracle.expected_status
                }}</span>
                <div v-if="oracle.expected_behavior">
                  {{ oracle.expected_behavior.description }}
                </div>
                <div v-for="(assertion, index) in oracle.assertions" :key="index" class="mono loc">
                  {{ assertion.path }} {{ assertion.op }}
                  {{ 'value' in assertion ? JSON.stringify(assertion.value) : '' }}
                </div>
              </td>
              <td class="mono loc">
                <div v-for="(condition, index) in oracle.state_conditions ?? []" :key="index">
                  {{
                    condition.mode === 'integration' ? '真实环境联调前提' : '待控制的场景条件'
                  }}：{{ condition.description }}
                </div>
                <div
                  v-for="(blocker, index) in oracle.execution_blockers ?? []"
                  :key="`blocker-${index}`"
                >
                  待准备：{{ blocker }}
                </div>
                <template v-if="oracle.fixture_plan">
                  任务自动新增记录 → 查询真实 ID → 执行用例 → 接口软删除
                </template>
                <template v-else>{{
                  oracle.preconditions.length
                    ? JSON.stringify(oracle.preconditions)
                    : '无请求前置条件'
                }}</template>
              </td>
              <td class="mono loc">
                {{ oracle.evidence_refs.join('、') }}
                <div v-if="oracle.fixture_plan">
                  造数链路：{{ oracle.fixture_plan.source_refs.join('、') }}
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
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

      <section
        v-if="callChains.length"
        class="card call-chain-card"
        aria-labelledby="call-chain-title"
      >
        <header id="call-chain-title" class="derived-title">
          源码调用链图（{{ callChains.length }} 个接口）
        </header>
        <p class="dim call-chain-intro">
          连线只表示静态解析确认的直接调用关系；虚线表示回到已出现的方法。未解析调用单独列出，不补画推测路径。
        </p>
        <input
          v-model="callChainFilter"
          class="call-chain-filter"
          type="search"
          placeholder="按接口编号、仓库或入口文件筛选"
          aria-label="筛选源码调用链"
        />
        <details
          v-for="{ chain, diagram } in visibleCallChainDiagrams"
          :key="chain.interface_id"
          class="call-chain-item"
        >
          <summary>
            <span class="mono req-id">{{ chain.interface_id }}</span>
            <span class="mono loc">{{ chain.entry.file ?? '入口文件未记录' }}</span>
            <span class="tag" :data-chain-status="chain.status ?? 'partial'">
              {{
                chain.status === 'complete'
                  ? '静态解析完整'
                  : `待核查 · 未解析 ${chain.unresolved_calls ?? chain.unresolved?.length ?? 0} 个`
              }}
            </span>
            <span class="mapping-meta">
              {{ chain.resolved_nodes ?? diagram.nodes.length - 1 }} 个已定位方法 ·
              {{ chain.resolved_calls ?? diagram.edges.length }} 条已确认调用
            </span>
          </summary>
          <div class="call-chain-scroll">
            <svg
              v-if="diagram.nodes.length"
              class="call-chain-svg"
              :viewBox="`0 0 ${diagram.width} ${diagram.height}`"
              :style="{ width: `${diagram.width}px`, height: `${diagram.height}px` }"
              role="img"
              :aria-label="`${chain.interface_id} 源码调用链，${diagram.nodes.length} 个源码方法节点，${diagram.edges.length} 条已确认调用边`"
            >
              <defs>
                <marker
                  :id="`chain-arrow-${chain.interface_id}`"
                  markerWidth="8"
                  markerHeight="8"
                  refX="7"
                  refY="4"
                  orient="auto"
                  markerUnits="strokeWidth"
                >
                  <path d="M 0 0 L 8 4 L 0 8 z" class="call-chain-arrow" />
                </marker>
              </defs>
              <g v-for="edge in diagram.edges" :key="edge.key">
                <path
                  :d="edge.path"
                  class="call-chain-edge"
                  :class="{ 'call-chain-back-edge': edge.isBackEdge }"
                  :marker-end="`url(#chain-arrow-${chain.interface_id})`"
                />
                <text
                  v-if="edge.callLine"
                  :x="edge.labelX"
                  :y="edge.labelY"
                  class="call-chain-edge-label"
                >
                  调用行 {{ edge.callLine }}
                </text>
              </g>
              <g
                v-for="node in diagram.nodes"
                :key="node.key"
                :transform="`translate(${node.x} ${node.y})`"
              >
                <title>
                  {{ node.methodName }} · {{ node.file }}:{{ node.startLine ?? '?' }}-{{
                    node.endLine ?? '?'
                  }}
                </title>
                <rect
                  class="call-chain-node"
                  :data-role="node.role"
                  :width="250"
                  :height="88"
                  rx="10"
                />
                <text x="14" y="23" class="call-chain-node-role">
                  {{ callChainRole(node.role) }} · 深度 {{ node.depth }}
                </text>
                <text x="14" y="44" class="call-chain-node-file">
                  {{ callChainMethodLabel(node.methodName) }}
                </text>
                <text x="14" y="63" class="call-chain-node-path">
                  {{ callChainFileLabel(node.file) }}
                </text>
                <text x="14" y="80" class="call-chain-node-lines">
                  {{ callChainNodeLines(node) }}
                </text>
              </g>
            </svg>
          </div>
          <p v-if="!diagram.hasEdges" class="call-chain-note">
            该报告没有保存调用边数据；当前只展示能定位的源码节点，不画推测连线。
          </p>
          <p v-if="diagram.legacyNames.length" class="call-chain-note">
            旧报告只保留了被调对象名称：{{
              diagram.legacyNames.join('、')
            }}；没有源码方法位置可绘图。
          </p>
          <ul v-if="chain.unresolved?.length" class="call-chain-unresolved">
            <li
              v-for="(unresolved, index) in chain.unresolved"
              :key="`${chain.interface_id}-unresolved-${index}`"
            >
              <span class="mono"
                >{{ String(unresolved.type ?? '未知类型') }}.{{
                  String(unresolved.method ?? '未知方法')
                }}</span
              >
              <span> {{ String(unresolved.reason ?? '未解析原因未记录') }}</span>
            </li>
          </ul>
        </details>
        <p v-if="!visibleCallChainDiagrams.length" class="dim">没有匹配的接口调用链。</p>
      </section>

      <div v-if="fieldContracts.length" class="card">
        <details>
          <summary>
            字段取值依据（{{ fieldContracts.length }} 个字段；{{ unresolvedFieldCount }}
            个取值规则未明确）
          </summary>
          <p class="dim">
            编码来自源码字段说明；“示例”不代表全部合法值。未明确的字段需在执行前核对。
          </p>
          <input
            v-model="fieldFilter"
            type="search"
            placeholder="按接口、字段或说明筛选"
            aria-label="筛选字段取值依据"
          />
          <table class="mapping-table">
            <thead>
              <tr>
                <th>接口</th>
                <th>字段</th>
                <th>取值规则</th>
                <th>源码位置</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in visibleFieldContracts"
                :key="`${item.interface_id}-${item.parameter}-${item.field}`"
              >
                <td class="mono">{{ item.interface_id }}</td>
                <td class="mono">
                  {{ item.field }} <small>({{ item.type }})</small>
                  <small v-if="item.required !== undefined" class="mapping-meta">
                    {{ item.required ? '必填' : '可选' }}
                  </small>
                </td>
                <td>
                  <template v-if="item.value_rule_status === 'coded_values'">
                    {{
                      item.documented_values
                        .map((value) => `${value.code} ${value.label}`)
                        .join('；')
                    }}
                  </template>
                  <template v-else-if="item.value_rule_status === 'examples_only'">
                    示例：{{ item.example_codes.join('、') }}
                  </template>
                  <template v-else>未明确</template>
                  <small v-if="item.description" class="mapping-meta">
                    {{ item.description_origin === 'source' ? '源码说明' : '未核实说明' }}：{{
                      item.description
                    }}
                  </small>
                  <small v-if="item.constraints?.length" class="mapping-meta">
                    代码约束：{{ item.constraints.join('；') }}
                  </small>
                </td>
                <td class="mono loc">
                  {{
                    item.source_refs
                      .map((ref) => `${ref.file ?? ''}:${ref.line ?? ''}`)
                      .join('；') || '—'
                  }}
                </td>
              </tr>
            </tbody>
          </table>
          <p v-if="fieldContracts.length > 200" class="dim">
            最多显示 200 条；可输入接口或字段继续筛选。
          </p>
        </details>
      </div>

      <!-- 视图二：变更范围（C-09 双分支 diff；C-108 事实程序化派生 + LLM 影响分析） -->
      <div v-if="hasChangeScope" class="card change-scope-card">
        <header class="derived-title">
          视图二 · 变更范围与存量回归影响（C-09 基线→被测分支 diff · 事实程序化派生 + LLM
          回归影响分析）
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
.coverage-warning > summary {
  cursor: pointer;
}
.coverage-warning.diagnostic-done {
  background: var(--surface-2);
  color: var(--text-2);
  border-color: var(--border);
}
.diagnostic-original {
  margin-top: 6px;
  color: var(--text-3);
  font-size: var(--text-xs);
}
.diagnostic-original > summary {
  cursor: pointer;
}
.diagnostic-links {
  margin: 4px 0;
  font-size: var(--text-xs);
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
.call-chain-card > .derived-title {
  margin-bottom: var(--space-2);
}
.call-chain-intro {
  margin: 0 0 var(--space-3);
}
.call-chain-filter {
  width: min(100%, 420px);
  margin-bottom: var(--space-3);
}
.call-chain-item {
  border-top: 1px solid var(--border);
}
.call-chain-item > summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) 0;
  cursor: pointer;
}
.call-chain-item > summary .loc {
  overflow-wrap: anywhere;
}
.tag[data-chain-status='complete'] {
  color: var(--accent-strong);
  background: var(--accent-dim);
}
.tag[data-chain-status='partial'] {
  color: var(--warning);
  background: var(--warning-dim);
}
.call-chain-scroll {
  max-width: 100%;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface-2);
}
.call-chain-svg {
  display: block;
  max-width: none;
}
.call-chain-edge {
  fill: none;
  stroke: var(--accent-strong);
  stroke-width: 2;
  opacity: 0.8;
}
.call-chain-back-edge {
  stroke: var(--warning);
  stroke-dasharray: 5 4;
}
.call-chain-arrow {
  fill: var(--accent-strong);
}
.call-chain-edge-label {
  fill: var(--text-3);
  font-size: 10px;
  text-anchor: middle;
}
.call-chain-node {
  fill: var(--surface);
  stroke: var(--border-strong);
  stroke-width: 1.25;
}
.call-chain-node[data-role='controller'] {
  stroke: var(--accent-strong);
}
.call-chain-node[data-role='service'] {
  stroke: var(--warning);
}
.call-chain-node-role {
  fill: var(--text-3);
  font-size: 10px;
}
.call-chain-node-file {
  fill: var(--text-1);
  font-family: var(--font-mono, monospace);
  font-size: 12px;
}
.call-chain-node-path {
  fill: var(--text-3);
  font-family: var(--font-mono, monospace);
  font-size: 10px;
}
.call-chain-node-lines {
  fill: var(--text-3);
  font-size: 10px;
}
.call-chain-note {
  margin: var(--space-2) 0;
  color: var(--text-3);
  font-size: var(--text-xs);
}
.call-chain-unresolved {
  margin: var(--space-2) 0 var(--space-3);
  padding-left: var(--space-4);
  color: var(--warning);
  font-size: var(--text-xs);
}
.call-chain-unresolved li + li {
  margin-top: var(--space-1);
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
