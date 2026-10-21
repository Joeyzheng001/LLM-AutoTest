<script setup lang="ts">
// 共享 job 选择器：下拉列出产物目录中的 job（后端 list_jobs），v-model 双向绑定 job_id。
// 运行中的 job 如实标注（running 来自后端内存任务表）。
import { onMounted, ref } from 'vue'
import { listJobs } from '../api/client'
import type { JobListItem } from '../api/types'

const props = withDefaults(defineProps<{ showNodeCount?: boolean }>(), { showNodeCount: true })
const model = defineModel<string>({ default: '' })

const jobs = ref<JobListItem[]>([])
const loading = ref(false)
const error = ref('')

async function refresh(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const result = await listJobs()
    jobs.value = result.jobs
    // 当前选中失效（被清空或不在清单）时回落到首项
    if (model.value && !jobs.value.some((job) => job.job_id === model.value)) {
      model.value = jobs.value[0]?.job_id ?? ''
    } else if (!model.value && jobs.value.length) {
      model.value = jobs.value[0].job_id
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'job 清单加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(refresh)

defineExpose({ refresh })
</script>

<template>
  <div class="job-picker">
    <label class="picker-label" for="job-picker-select">选择任务</label>
    <select id="job-picker-select" v-model="model" class="picker-select mono" :aria-busy="loading">
      <option v-if="!jobs.length" value="" disabled>
        {{ loading ? '加载中…' : '暂无任务（先在「任务配置」页提交）' }}
      </option>
      <option v-for="job in jobs" :key="job.job_id" :value="job.job_id">
        {{ job.job_id }}{{ job.running ? '（运行中）' : ''
        }}{{
          props.showNodeCount
            ? ` · ${job.completed}/9 节点${job.degraded ? ` · ${job.degraded} 降级` : ''}`
            : ''
        }}
      </option>
    </select>
    <button class="ghost-btn" type="button" :disabled="loading" @click="refresh">刷新</button>
    <p v-if="error" class="picker-error" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.job-picker {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-bottom: var(--space-4);
}

.picker-label {
  font-size: var(--text-sm);
  color: var(--text-2);
}

.picker-select {
  min-width: 320px;
  max-width: 560px;
  padding: var(--space-2) var(--space-3);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-1);
  font-size: var(--text-sm);
}

.picker-select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-dim);
}

.picker-error {
  width: 100%;
  margin: 0;
  font-size: var(--text-sm);
  color: var(--warning);
}

.ghost-btn {
  padding: var(--space-2) var(--space-3);
  background: var(--bg-elev);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  color: var(--text-2);
  cursor: pointer;
  font-size: var(--text-sm);
  white-space: nowrap;
}

.ghost-btn:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
}

.ghost-btn:disabled {
  opacity: 0.5;
  cursor: wait;
}
</style>
