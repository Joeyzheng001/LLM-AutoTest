<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type {
  FieldValueContract,
  InterfaceDef,
  ReviewCallChain,
  ReviewBranchCandidate,
  ReviewEvidence,
  ReviewTestOracle,
} from '../api/types'

const props = defineProps<{
  interfaceDef: InterfaceDef
  chain?: ReviewCallChain
  oracles: ReviewTestOracle[]
  branches: ReviewBranchCandidate[]
  fieldContracts: FieldValueContract[]
  evidence: ReviewEvidence[]
}>()
const emit = defineEmits<{ (event: 'show-cases'): void }>()
const tab = ref<'overview' | 'logic' | 'contract'>('overview')
const branchFilter = ref<'all' | 'unmapped'>('unmapped')
watch(
  () => props.interfaceDef.id,
  () => {
    tab.value = 'overview'
    branchFilter.value = 'unmapped'
  },
)

const evidenceById = computed(() => new Map(props.evidence.map((item) => [item.evidence_id, item])))
const responseChecks = computed(() => {
  const seen = new Set<string>()
  return props.oracles
    .flatMap((oracle) =>
      (oracle.assertions ?? []).map((assertion) => ({ ...assertion, oracleId: oracle.oracle_id })),
    )
    .filter((assertion) => {
      const key = JSON.stringify([
        assertion.oracleId,
        assertion.path,
        assertion.op,
        assertion.value,
      ])
      if (!assertion.path || seen.has(key)) return false
      seen.add(key)
      return true
    })
})
const requestFields = computed(() =>
  (props.interfaceDef.params ?? []).flatMap((param) =>
    (param.fields ?? []).map((field) => ({ ...field, parameter: param.name })),
  ),
)
const unmappedBranchCount = computed(
  () => props.branches.filter((branch) => !branch.oracle_ids.length).length,
)
const visibleBranches = computed(() =>
  branchFilter.value === 'unmapped'
    ? props.branches.filter((branch) => !branch.oracle_ids.length)
    : props.branches,
)
function sourceLocation(file?: string, line?: number | null): string {
  if (!file) return '源码位置未记录'
  return `${file}${line ? `:${line}` : ''}`
}
function requiredLabel(value?: boolean): string {
  return value === true ? '还原标记必填' : value === false ? '还原标记可选' : '未判定'
}
function evidenceLocation(ref: string): string {
  const item = evidenceById.value.get(ref)
  return item ? sourceLocation(item.file, item.start_line) : ref
}
function conditionText(condition: { path: string; op: string; value?: unknown }): string {
  return `${condition.path} ${condition.op}${condition.value === undefined ? '' : ` ${JSON.stringify(condition.value)}`}`
}
function controlLabel(kind: ReviewBranchCandidate['control_kind']): string {
  return {
    request: '请求输入',
    downstream_response: '下游返回',
    application_config: '应用配置',
    service_registry: '服务注册',
    unresolved: '控制来源待确认',
  }[kind]
}
function linkedOracleText(branch: ReviewBranchCandidate): string {
  if (!branch.oracle_ids.length) return '未关联独立预期'
  return `已关联 ${branch.oracle_ids
    .map((id, index) =>
      branch.node_kind === 'if'
        ? `${id}（条件${branch.oracle_outcomes[index] ? '成立' : '不成立'}）`
        : id,
    )
    .join('、')}`
}
</script>

<template>
  <section class="logic-panel" aria-label="接口逻辑">
    <header class="panel-head">
      <div>
        <span class="eyebrow">{{ interfaceDef.id }} · 接口逻辑</span>
        <h2>
          {{ interfaceDef.signature.method || interfaceDef.protocol.toUpperCase() }}
          {{ interfaceDef.signature.path || interfaceDef.signature.service_method }}
        </h2>
        <p>{{ interfaceDef.signature.summary || '接口用途尚未从源码确认' }}</p>
      </div>
      <button type="button" class="case-action" @click="emit('show-cases')">
        查看此接口用例 →
      </button>
    </header>

    <nav class="logic-tabs" aria-label="接口逻辑内容">
      <button
        type="button"
        :aria-current="tab === 'overview' ? 'page' : undefined"
        @click="tab = 'overview'"
      >
        接口概览
      </button>
      <button
        type="button"
        :aria-current="tab === 'logic' ? 'page' : undefined"
        @click="tab = 'logic'"
      >
        代码逻辑
      </button>
      <button
        type="button"
        :aria-current="tab === 'contract' ? 'page' : undefined"
        @click="tab = 'contract'"
      >
        请求与响应
      </button>
    </nav>

    <div v-if="tab === 'overview'" class="panel-body">
      <dl class="overview-grid">
        <div>
          <dt>入口位置</dt>
          <dd class="mono">
            {{
              sourceLocation(
                interfaceDef.code_location?.file,
                interfaceDef.code_location?.start_line,
              )
            }}
          </dd>
        </div>
        <div>
          <dt>响应类型</dt>
          <dd class="mono">{{ interfaceDef.returns?.type || '未记录' }}</dd>
        </div>
        <div>
          <dt>请求参数</dt>
          <dd>
            {{ interfaceDef.params?.length ?? 0 }} 个参数 · {{ requestFields.length }} 个已识别字段
          </dd>
        </div>
        <div>
          <dt>源码场景</dt>
          <dd>{{ oracles.length }} 个已形成可追溯预期的场景</dd>
        </div>
        <div v-if="branches.length">
          <dt>静态分支候选</dt>
          <dd>{{ branches.length }} 处；其中 {{ unmappedBranchCount }} 处尚无独立预期</dd>
        </div>
      </dl>
      <div v-if="chain?.status === 'partial' || interfaceDef.call_graph_incomplete" class="notice">
        静态调用链尚未闭合。下方只列已定位的方法，不能据此断定没有其他调用。
      </div>
      <div v-if="chain" class="call-preview">
        <h3>已定位调用</h3>
        <ol>
          <li class="mono">
            {{ sourceLocation(chain.entry.file, chain.entry.start_line) }} ·
            {{ chain.entry.method_name || '入口' }}
          </li>
          <li v-for="(callee, index) in chain.callees" :key="index" class="mono">
            <template v-if="typeof callee === 'string'">{{ callee }}</template>
            <template v-else
              >{{ sourceLocation(callee.file, callee.start_line) }} ·
              {{ callee.method_name || '方法未记录' }}</template
            >
          </li>
        </ol>
        <p class="explain">
          这是静态调用关系，列表顺序不代表请求运行时的执行顺序；完整调用图和未解析节点仍在下方 Code
          Review 证据区。
        </p>
      </div>
      <p v-else class="explain">当前尚无可展示的调用链；可继续查看已确认的场景与字段证据。</p>
    </div>

    <div v-else-if="tab === 'logic'" class="panel-body">
      <p class="explain">
        按源码分支展示触发条件和可观察结果。需要下游返回、配置或测试数据的场景会保留设计条件，不在这里标为已执行。
      </p>
      <div v-if="!oracles.length" class="notice">
        尚未形成此接口的源码场景；请查看下方评审诊断。
      </div>
      <article v-for="oracle in oracles" :key="oracle.oracle_id" class="scenario">
        <header>
          <span class="mono">{{ oracle.oracle_id }}</span>
          <strong>{{ oracle.scenario }}</strong>
          <span class="readiness" :data-readiness="oracle.execution_readiness || 'ready'">
            {{
              oracle.execution_readiness === 'blocked'
                ? '条件未就绪'
                : oracle.execution_readiness === 'conditional'
                  ? '需核对条件'
                  : '设计已确认'
            }}
          </span>
        </header>
        <p v-if="oracle.expected_behavior">
          <b>源码行为：</b>{{ oracle.expected_behavior.description }}
        </p>
        <p v-if="oracle.preconditions?.length">
          <b>请求条件：</b
          ><span class="mono">{{ oracle.preconditions.map(conditionText).join('；') }}</span>
        </p>
        <p v-for="(condition, index) in oracle.state_conditions ?? []" :key="`state-${index}`">
          <b>{{ condition.mode === 'controlled' ? '需控制下游：' : '联调条件：' }}</b
          >{{ condition.description }}
        </p>
        <p
          v-for="(blocker, index) in oracle.execution_blockers ?? []"
          :key="`block-${index}`"
          class="blocker"
        >
          <b>待准备：</b>{{ blocker }}
        </p>
        <p><b>响应状态：</b>{{ oracle.expected_status }}</p>
        <p v-if="oracle.assertions?.length">
          <b>响应检查：</b
          ><span class="mono">{{ oracle.assertions.map(conditionText).join('；') }}</span>
        </p>
        <details v-if="oracle.evidence_refs?.length">
          <summary>查看源码证据（{{ oracle.evidence_refs.length }}）</summary>
          <ul>
            <li v-for="evidenceRef in oracle.evidence_refs" :key="evidenceRef">
              <span class="mono">{{ evidenceRef }} · {{ evidenceLocation(evidenceRef) }}</span>
              <pre v-if="evidenceById.get(evidenceRef)?.excerpt" class="evidence-excerpt">{{
                evidenceById.get(evidenceRef)?.excerpt
              }}</pre>
            </li>
          </ul>
        </details>
      </article>
      <details v-if="branches.length" class="branch-inventory">
        <summary>
          源码分支候选（{{ branches.length }} 处，{{ unmappedBranchCount }} 处未关联独立预期）
        </summary>
        <p class="explain">
          这里保留静态提取的 if、catch 和 return。未关联 oracle
          表示尚无独立测试预期；它也可能只是内部控制或与其他分支共享同一响应，需要结合源码判断。
        </p>
        <label class="branch-filter">
          <span>显示范围</span>
          <select v-model="branchFilter">
            <option value="unmapped">仅未关联预期</option>
            <option value="all">全部候选</option>
          </select>
        </label>
        <p v-if="!visibleBranches.length" class="explain">当前范围没有未关联预期的分支候选。</p>
        <ul v-else class="branch-list">
          <li v-for="branch in visibleBranches" :key="branch.branch_id">
            <div class="branch-head">
              <span class="mono">{{ branch.branch_id }}</span>
              <span>{{ branch.node_kind }} · {{ controlLabel(branch.control_kind) }}</span>
              <span>{{ linkedOracleText(branch) }}</span>
            </div>
            <code>{{ branch.quote }}</code>
            <p class="mono">{{ evidenceLocation(branch.evidence_ref) }}</p>
            <p v-if="branch.source_refs?.some((ref) => ref !== branch.evidence_ref)" class="explain">
              条件来源：{{ (branch.source_refs ?? []).filter((ref) => ref !== branch.evidence_ref).map(evidenceLocation).join('；') }}
            </p>
          </li>
        </ul>
      </details>
      <p v-else class="explain">当前 Review 产物未保存分支候选清单。</p>
    </div>

    <div v-else class="panel-body">
      <h3>请求字段</h3>
      <p class="explain">
        字段来自接口还原结果；其必填标记须结合源码依据复核，也不等于下游 O32 的必填契约。
      </p>
      <table v-if="requestFields.length" class="contract-table">
        <thead>
          <tr>
            <th>位置 / 字段</th>
            <th>说明</th>
            <th>必填性</th>
            <th>来源</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="field in requestFields" :key="`${field.parameter}.${field.name}`">
            <td class="mono">{{ field.parameter }}.{{ field.name }}</td>
            <td>{{ field.description || '未说明' }}</td>
            <td>{{ requiredLabel(field.required) }}</td>
            <td class="mono">
              {{
                (field.source_refs ?? [])
                  .map((ref) => sourceLocation(ref.file, ref.line))
                  .join('；') || '未记录'
              }}
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="notice">尚未还原请求体字段。</p>
      <details v-if="fieldContracts.length" class="field-contracts">
        <summary>字段取值依据（{{ fieldContracts.length }}）</summary>
        <ul>
          <li v-for="field in fieldContracts" :key="`${field.parameter}.${field.field}`">
            <span class="mono">{{ field.field }}</span> · {{ field.description }} ·
            {{
              field.value_rule_status === 'coded_values'
                ? '源码限定取值'
                : field.value_rule_status === 'examples_only'
                  ? '仅有示例取值'
                  : '取值未说明'
            }}
          </li>
        </ul>
      </details>
      <h3>响应与可观察检查</h3>
      <p>
        响应类型：<span class="mono">{{ interfaceDef.returns?.type || '未记录' }}</span>
      </p>
      <p class="explain">
        以下字段来自当前源码场景的响应断言，不是完整响应 Schema；也不表示已有成功业务样本。
      </p>
      <ul v-if="responseChecks.length">
        <li
          v-for="item in responseChecks"
          :key="`${item.oracleId}-${item.path}-${item.op}-${JSON.stringify(item.value)}`"
          class="mono"
        >
          {{ item.oracleId }} · {{ conditionText(item) }}
        </li>
      </ul>
      <p v-else class="notice">当前没有可机器判定的响应字段预期。</p>
    </div>
  </section>
</template>

<style scoped>
.logic-panel {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  margin: var(--space-4) 0;
  overflow: hidden;
}
.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-4);
}
.panel-head h2 {
  margin: var(--space-2) 0;
  font-size: 1.3rem;
  overflow-wrap: anywhere;
}
.panel-head p,
.explain {
  color: var(--text-3);
  line-height: 1.5;
}
.panel-head p {
  margin: 0;
}
.eyebrow {
  color: var(--accent-strong);
  font-size: var(--text-sm);
  font-weight: 700;
}
.case-action {
  flex: none;
  border: 1px solid var(--accent-strong);
  border-radius: var(--radius-sm);
  color: var(--accent-strong);
  background: transparent;
  padding: var(--space-2) var(--space-3);
  font-family: var(--font-sans);
  font-size: var(--text-sm);
  line-height: 1.6;
  cursor: pointer;
}
.logic-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: 0 var(--space-4);
  border-bottom: 1px solid var(--border);
}
.logic-tabs button {
  border: 0;
  border-bottom: 2px solid transparent;
  padding: var(--space-2) var(--space-3);
  background: none;
  color: var(--text-2);
  font: inherit;
  cursor: pointer;
}
.logic-tabs button[aria-current='page'] {
  border-color: var(--accent-strong);
  color: var(--accent-strong);
  font-weight: 700;
}
.panel-body {
  padding: var(--space-4);
}
.panel-body h3 {
  margin: var(--space-4) 0 var(--space-2);
  font-size: 1rem;
}
.overview-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3);
  margin: 0 0 var(--space-4);
}
.overview-grid div {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--space-3);
  min-width: 0;
}
.overview-grid dt {
  color: var(--text-3);
  font-size: var(--text-sm);
}
.overview-grid dd {
  margin: var(--space-2) 0 0;
  overflow-wrap: anywhere;
}
.notice {
  padding: var(--space-3);
  border-left: 3px solid var(--warning);
  background: var(--surface-2);
  color: var(--text-2);
}
.call-preview ol {
  max-height: 17rem;
  overflow: auto;
  padding-left: var(--space-4);
}
.call-preview li {
  margin: var(--space-2) 0;
  overflow-wrap: anywhere;
}
.scenario {
  border-top: 1px solid var(--border);
  padding: var(--space-3) 0;
}
.scenario header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.scenario p {
  margin: var(--space-2) 0;
  line-height: 1.5;
}
.scenario b {
  margin-right: 0.35em;
}
.scenario ul {
  padding-left: var(--space-4);
}
.evidence-excerpt {
  max-height: 18rem;
  overflow: auto;
  padding: var(--space-2);
  white-space: pre-wrap;
  background: var(--surface-2);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
}
.readiness {
  margin-left: auto;
  color: var(--text-3);
  font-size: var(--text-sm);
}
.readiness[data-readiness='blocked'],
.blocker {
  color: var(--warning);
}
.contract-table {
  width: 100%;
  border-collapse: collapse;
}
.contract-table th,
.contract-table td {
  text-align: left;
  vertical-align: top;
  border-bottom: 1px solid var(--border);
  padding: var(--space-2);
  overflow-wrap: anywhere;
}
.contract-table th {
  color: var(--text-3);
  font-size: var(--text-sm);
}
.field-contracts {
  margin: var(--space-3) 0;
}
.field-contracts li {
  margin: var(--space-2) 0;
}
.branch-inventory {
  margin-top: var(--space-4);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.branch-inventory summary {
  cursor: pointer;
  font-weight: 700;
}
.branch-filter {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--text-3);
  font-size: var(--text-sm);
}
.branch-filter select {
  padding: var(--space-1) var(--space-2);
  color: var(--text-1);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-family: var(--font-sans);
  font-size: var(--text-sm);
  line-height: 1.6;
}
.branch-list {
  max-height: 28rem;
  overflow: auto;
  padding-left: 0;
  list-style: none;
}
.branch-list li {
  padding: var(--space-2) 0;
  border-top: 1px solid var(--border);
}
.branch-head {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  color: var(--text-2);
  font-size: var(--text-sm);
}
.branch-list code {
  display: block;
  margin-top: var(--space-2);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.branch-list p {
  margin: var(--space-1) 0 0;
  color: var(--text-3);
  font-size: var(--text-xs);
}
@media (max-width: 720px) {
  .panel-head {
    flex-direction: column;
  }
  .overview-grid {
    grid-template-columns: 1fr;
  }
  .contract-table {
    display: block;
    overflow-x: auto;
  }
}
</style>
