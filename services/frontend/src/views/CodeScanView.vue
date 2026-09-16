<script setup lang="ts">
// 源码扫描与接口还原页（C-10 独立展示屏）：code_inventory.json 仓库四态 +
// interface_candidates.json 静态候选（置信度）+ interfaces.json 还原结果（参数与代码溯源）。
// 红线：兜底候选如实标注（D5）；候选与最终接口分列（静态定位 ≠ 语义还原）。
import { ref, watch } from 'vue'
import JobPicker from '../components/JobPicker.vue'
import { getCodeInventory, getInterfaceCandidates, getInterfaces } from '../api/client'
import type { InterfaceCandidate, InterfaceDef, RepoInventory } from '../api/types'
import { REPO_STATUS_LABELS } from '../api/labels'

const _jobId = ref('')

// C-16：总览页节点卡跳转携带 jobId（App.vue 动态组件下发）
const props = defineProps<{ initialJobId?: string }>()

const repositories = ref<RepoInventory[]>([])
const candidates = ref<InterfaceCandidate[]>([])
const interfaces = ref<InterfaceDef[]>([])
const loading = ref(false)
const error = ref('')

async function load(): Promise<void> {
  if (!_jobId.value) return
  loading.value = true
  error.value = ''
  // 三产物独立加载：节点未跑到时如实提示，已生成的部分照常展示
  const [repoResult, candResult, ifaceResult] = await Promise.allSettled([
    getCodeInventory(_jobId.value),
    getInterfaceCandidates(_jobId.value),
    getInterfaces(_jobId.value),
  ])
  repositories.value = repoResult.status === 'fulfilled' ? repoResult.value.repositories : []
  candidates.value = candResult.status === 'fulfilled' ? candResult.value.candidates : []
  interfaces.value = ifaceResult.status === 'fulfilled' ? ifaceResult.value.interfaces : []
  if (repoResult.status === 'rejected' && candidates.value.length === 0) {
    error.value =
      'code_inventory.json 与 interface_candidates.json 均未生成（需任务跑到源码扫描节点之后）'
  }
  loading.value = false
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

function locationText(location: {
  repo: string
  file: string
  start_line?: number
  end_line?: number
}): string {
  const lines =
    location.start_line != null ? `:${location.start_line}-${location.end_line ?? ''}` : ''
  return `${location.repo}/${location.file}${lines}`
}
</script>

<template>
  <div class="scan-view">
    <JobPicker v-model="_jobId" />

    <p v-if="loading" class="state-line">加载扫描与接口还原产物中…</p>
    <p v-else-if="error && !_jobId" class="state-line">请先选择任务。</p>
    <p
      v-else-if="error && !repositories.length && !candidates.length && !interfaces.length"
      class="state-line state-error"
      role="alert"
    >
      {{ error }}
    </p>

    <template v-else-if="_jobId">
      <!-- 一、仓库扫描四态（X-CODE-01~03 如实分列） -->
      <section v-if="repositories.length" class="card">
        <header class="derived-title">
          源码扫描清单（{{ repositories.length }} 个仓库 · 四态如实分列 X-CODE-01~03）
        </header>
        <table class="scan-table">
          <thead>
            <tr>
              <th scope="col">仓库</th>
              <th scope="col">分支</th>
              <th scope="col">文件数</th>
              <th scope="col">状态</th>
              <th scope="col">说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="repo in repositories" :key="repo.name">
              <td>
                <span class="mono">{{ repo.name }}</span>
                <span v-if="repo.is_fallback" class="tag tag-warning">兜底</span>
              </td>
              <td>
                <span class="mono loc">{{ repo.branch }}</span>
              </td>
              <td>
                <span class="mono">{{ repo.file_count }}</span>
              </td>
              <td>
                <span class="tag" :data-repo-status="repo.status">
                  {{ REPO_STATUS_LABELS[repo.status] ?? repo.status }}
                </span>
              </td>
              <td>
                <span v-if="repo.error" class="state-error mono">{{ repo.error.code }}</span>
                <span v-else-if="repo.truncated_scope?.length" class="dim">
                  截断范围 {{ repo.truncated_scope.length }} 目录
                </span>
                <span v-else class="dim">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- 二、静态候选（R-REV-02 第①步，静态侧产物） -->
      <section v-if="candidates.length" class="card">
        <header class="derived-title">
          接口候选（{{ candidates.length }} 个 · 静态特征定位 R-REV-02 · 置信度标注）
        </header>
        <ul class="iface-list">
          <li v-for="cand in candidates" :key="cand.candidate_id" class="iface-item">
            <span class="tag mono">{{ cand.protocol }}</span>
            <span class="mono cand-id">{{ cand.candidate_id }}</span>
            <span class="mono sig">{{ cand.signature }}</span>
            <span class="mono loc">{{ locationText(cand.code_location) }}</span>
            <span v-if="cand.confidence != null" class="mono conf"
              >置信 {{ cand.confidence.toFixed(2) }}</span
            >
            <span v-if="cand.is_fallback" class="tag tag-warning">兜底</span>
          </li>
        </ul>
      </section>

      <!-- 三、接口还原结果（LLM 语义解析 + F-REV-04 代码溯源） -->
      <section v-if="interfaces.length" class="card">
        <header class="derived-title">
          接口还原结果（{{ interfaces.length }} 个 · LLM 语义解析 · 代码位置溯源 F-REV-04）
        </header>
        <table class="scan-table">
          <thead>
            <tr>
              <th scope="col">接口</th>
              <th scope="col">协议</th>
              <th scope="col">签名</th>
              <th scope="col">参数</th>
              <th scope="col">代码位置</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="iface in interfaces" :key="iface.id">
              <td>
                <span class="mono cand-id">{{ iface.id }}</span>
              </td>
              <td>
                <span class="tag mono">{{ iface.protocol }}</span>
              </td>
              <td>
                <div class="req-cell">
                  <span class="mono sig"
                    >{{ iface.signature.method }} {{ iface.signature.path }}</span
                  >
                  <span v-if="iface.signature.summary" class="dim">{{
                    iface.signature.summary
                  }}</span>
                </div>
              </td>
              <td>
                <span v-if="iface.params?.length" class="mono loc">
                  {{ iface.params.map((p) => p.name).join('、') }}
                </span>
                <span v-else class="dim">无参数</span>
              </td>
              <td>
                <span v-if="iface.code_location" class="mono loc">{{
                  locationText(iface.code_location)
                }}</span>
                <span v-else class="dim">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <p v-if="!repositories.length && !candidates.length && !interfaces.length" class="state-line">
        源码扫描与接口还原产物尚未生成（需任务跑到对应节点之后）。
      </p>
    </template>
  </div>
</template>

<style scoped>
.scan-view {
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

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
  margin-bottom: var(--space-4);
}

.derived-title {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-1);
  margin-bottom: var(--space-3);
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--border);
}

.scan-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}

.scan-table th {
  text-align: left;
  font-weight: 500;
  font-size: var(--text-xs);
  color: var(--text-3);
  padding: var(--space-2);
  border-bottom: 1px solid var(--border-strong);
}

.scan-table td {
  padding: var(--space-2);
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}

.iface-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.iface-item {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface-2);
  margin-bottom: var(--space-2);
  font-size: var(--text-sm);
}

.tag {
  display: inline-block;
  padding: 2px var(--space-2);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text-2);
  text-transform: uppercase;
}

.tag-warning {
  background: var(--warning-dim);
  border-color: color-mix(in srgb, var(--warning) 30%, transparent);
  color: var(--warning);
  text-transform: none;
}

.tag[data-repo-status='ok'] {
  background: var(--accent-dim);
  border-color: var(--accent-glow);
  color: var(--accent-strong);
}

.tag[data-repo-status='error'] {
  background: var(--danger-dim, rgba(196, 74, 74, 0.12));
  border-color: color-mix(in srgb, var(--danger, #c44a4a) 30%, transparent);
  color: var(--danger, #c44a4a);
}

.tag[data-repo-status='truncated'],
.tag[data-repo-status='empty'] {
  background: var(--warning-dim);
  border-color: color-mix(in srgb, var(--warning) 30%, transparent);
  color: var(--warning);
}

.cand-id {
  color: var(--accent-strong);
  font-size: var(--text-xs);
}

.sig {
  font-size: var(--text-xs);
  color: var(--text-1);
}

.conf {
  font-size: var(--text-xs);
  color: var(--text-3);
}

.loc {
  font-size: var(--text-xs);
  color: var(--text-3);
  word-break: break-all;
}

.req-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.dim {
  color: var(--text-3);
  font-size: var(--text-sm);
}
</style>
