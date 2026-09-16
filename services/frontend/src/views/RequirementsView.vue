<script setup lang="ts">
// 需求解析页（C-10 独立展示屏）：requirement_items.json 全量条目——
// 需求 ID/标题/描述/优先级 + 文档来源（source_files，C-10 多文档）。
// 红线：数据来自后端产物（前端不计算）；stub_note（兜底条目）如实标注。
import { ref, watch } from 'vue'
import JobPicker from '../components/JobPicker.vue'
import { getRequirementItems } from '../api/client'
import type { RequirementItem } from '../api/types'

const _jobId = ref('')

// C-16：总览页节点卡跳转携带 jobId（App.vue 动态组件下发）
const props = defineProps<{ initialJobId?: string }>()

const items = ref<RequirementItem[]>([])
const sourceFiles = ref<string[]>([])
const loading = ref(false)
const error = ref('')

async function load(): Promise<void> {
  if (!_jobId.value) return
  loading.value = true
  error.value = ''
  try {
    const payload = await getRequirementItems(_jobId.value)
    items.value = payload.items
    // C-10：多文档来源清单（旧产物无此键 → 空数组隐藏）
    sourceFiles.value = payload.source_files ?? []
  } catch (err) {
    error.value = err instanceof Error ? err.message : '需求解析产物加载失败'
    items.value = []
    sourceFiles.value = []
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
</script>

<template>
  <div class="req-view">
    <JobPicker v-model="_jobId" />

    <p v-if="loading" class="state-line">加载需求解析产物中…</p>
    <p v-else-if="error" class="state-line state-error" role="alert">{{ error }}</p>
    <p v-else-if="!_jobId" class="state-line">请先选择任务。</p>

    <template v-else-if="items.length">
      <div class="view-head">
        <span class="tag tag-accent">需求解析结果（req_parse 产物）</span>
        <span class="coverage-note"
          >共 <span class="mono">{{ items.length }}</span> 条需求条目</span
        >
      </div>

      <!-- 文档来源（C-10 多文档） -->
      <div v-if="sourceFiles.length" class="card source-card">
        <header class="derived-title">来源文档（{{ sourceFiles.length }} 份）</header>
        <ul class="derived-list">
          <li v-for="path in sourceFiles" :key="path" class="derived-item">
            <span class="mono loc">{{ path }}</span>
          </li>
        </ul>
      </div>

      <div class="card">
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

    <p v-else class="state-line">requirement_items.json 尚未生成（需任务跑到需求解析节点之后）。</p>
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

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
  margin-bottom: var(--space-4);
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
