<script setup lang="ts">
// 流水线总览页（07 页 02 方向）：job 列表 + 9 节点状态 + 自动化率。
// 红线：automationRate 等数值全部来自后端（mock 直供展示值）；降级/兜底如实分列（D1/D5）。
import { computed, ref } from 'vue'
import { mockJobs, type JobSummary, type NodeStatus } from '../mock/jobs'

const selectedId = ref(mockJobs[0]?.id ?? '')
const selected = computed<JobSummary | undefined>(
  () => mockJobs.find((job) => job.id === selectedId.value) ?? mockJobs[0],
)

const STATUS_LABEL: Record<NodeStatus, string> = {
  completed: '已完成',
  running: '运行中',
  manual_intervention: '人工介入',
  pending: '待运行',
}

function formatRate(rate: number | null): string {
  // 展示口径：后端已汇总的值仅做格式化（百分比），null = 数据不完整（X-MTR-02）
  return rate === null ? '—' : `${Math.round(rate * 100)}%`
}

function formatTime(iso: string): string {
  return iso.replace('T', ' ').replace('Z', ' UTC')
}
</script>

<template>
  <div class="overview">
    <!-- 左：job 列表 -->
    <aside class="job-list">
      <header class="panel-title">任务列表</header>
      <button
        v-for="job in mockJobs"
        :key="job.id"
        class="job-item"
        :class="{ active: job.id === selected?.id }"
        type="button"
        @click="selectedId = job.id"
      >
        <span class="job-title">{{ job.title }}</span>
        <span class="mono job-id">{{ job.id.slice(0, 17) }}…</span>
        <span v-if="job.hasFallbackRepo" class="tag tag-warning">含兜底仓库</span>
      </button>
    </aside>

    <!-- 右：选中 job 详情 -->
    <section v-if="selected" class="detail">
      <header class="detail-head">
        <h2>{{ selected.title }}</h2>
        <span class="mono detail-meta">{{ formatTime(selected.createdAt) }}</span>
      </header>

      <!-- 自动化率（数值来自后端） -->
      <div class="rate-row">
        <div class="rate-block">
          <span class="rate-value mono">{{ formatRate(selected.automationRate) }}</span>
          <span class="rate-label">自动化率 · 分母固定 9</span>
        </div>
        <div v-if="selected.automationRate === null" class="hint hint-warning">
          数据不完整，不输出误导数值（X-MTR-02）
        </div>
      </div>

      <!-- 降级清单（D1：如实标注，分母不缩减） -->
      <div v-for="record in selected.degraded" :key="record.node" class="hint hint-warning">
        降级 · {{ record.node }}：{{ record.reason }}（按人工介入计入）
      </div>
      <!-- 兜底仓库分列提示（D5） -->
      <div v-if="selected.hasFallbackRepo" class="hint hint-notice">
        本任务含兜底演示仓库：结果单独分列，不计入 G3 真实仓库口径（D5）
      </div>

      <!-- 9 节点流水线 -->
      <div class="pipeline">
        <div v-for="(node, index) in selected.nodes" :key="node.name" class="node-wrap">
          <div class="node" :data-status="node.status">
            <span class="node-index mono">{{ String(index + 1).padStart(2, '0') }}</span>
            <span class="node-label">{{ node.label }}</span>
            <span class="node-status">{{ STATUS_LABEL[node.status] }}</span>
          </div>
          <span v-if="index < selected.nodes.length - 1" class="connector" aria-hidden="true" />
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.overview {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: var(--space-4);
  height: 100%;
}

/* 左列 job 列表 */
.job-list {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-3);
  overflow-y: auto;
}

.panel-title {
  color: var(--text-3);
  font-size: var(--text-sm);
  letter-spacing: 0.08em;
  padding: var(--space-2) var(--space-2) var(--space-3);
}

.job-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  width: 100%;
  padding: var(--space-3);
  margin-bottom: var(--space-2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  color: var(--text-2);
  cursor: pointer;
  text-align: left;
  transition:
    background 0.15s,
    border-color 0.15s;
}

.job-item:hover {
  background: var(--surface-2);
}

.job-item.active {
  background: var(--accent-dim);
  border-color: var(--accent-glow);
  color: var(--text-1);
}

.job-title {
  font-size: var(--text-md);
  font-weight: 500;
}

.job-id {
  font-size: var(--text-xs);
  color: var(--text-3);
}

/* 右列详情 */
.detail {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-5);
  overflow-y: auto;
}

.detail-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: var(--space-5);
}

.detail-head h2 {
  font-size: var(--text-xl);
  font-weight: 600;
}

.detail-meta {
  color: var(--text-3);
  font-size: var(--text-sm);
}

/* 自动化率 */
.rate-row {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}

.rate-block {
  padding: var(--space-3) var(--space-5);
  background: var(--accent-dim);
  border: 1px solid var(--accent-glow);
  border-radius: var(--radius-md);
}

.rate-value {
  display: block;
  font-size: 32px;
  font-weight: 600;
  color: var(--accent-strong);
  line-height: 1.2;
}

.rate-label {
  font-size: var(--text-xs);
  color: var(--text-3);
}

/* 提示条（降级/兜底/数据不完整：如实分列） */
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

/* 9 节点流水线 */
.pipeline {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4) var(--space-2);
  margin-top: var(--space-4);
}

.node-wrap {
  position: relative;
  display: flex;
  align-items: center;
}

.connector {
  width: 20px;
  height: 1px;
  background: var(--border-strong);
  margin: 0 var(--space-2);
}

.node {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 104px;
  padding: var(--space-3);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.node-index {
  font-size: var(--text-xs);
  color: var(--text-3);
}

.node-label {
  font-size: var(--text-md);
  font-weight: 500;
  color: var(--text-2);
}

.node-status {
  font-size: var(--text-xs);
  color: var(--text-3);
}

.node[data-status='completed'] {
  border-color: var(--accent-glow);
  background: var(--accent-dim);
}

.node[data-status='completed'] .node-label,
.node[data-status='completed'] .node-status {
  color: var(--accent-strong);
}

.node[data-status='running'] {
  border-color: var(--accent-glow);
  animation: breathe 1.8s ease-in-out infinite;
}

.node[data-status='running'] .node-status {
  color: var(--accent);
}

.node[data-status='manual_intervention'] {
  border-color: color-mix(in srgb, var(--warning) 40%, transparent);
  background: var(--warning-dim);
}

.node[data-status='manual_intervention'] .node-status {
  color: var(--warning);
}

@keyframes breathe {
  0%,
  100% {
    box-shadow: 0 0 0 0 transparent;
  }
  50% {
    box-shadow: 0 0 10px 0 var(--accent-glow);
  }
}
</style>
