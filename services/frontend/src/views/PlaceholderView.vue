<script setup lang="ts">
// 通用占位页：三视图 / 报告 / 缺陷 / 回归度量四页的方向锚点（按 M1–M5 排期实现）。
defineProps<{
  title: string
  spec: string
}>()
</script>

<template>
  <div class="placeholder">
    <div class="frame">
      <span class="mono glyph">{{ title.slice(0, 1) }}</span>
      <h2>{{ title }}</h2>
      <p class="desc">本页按 07 设计方向实现，数据将对接 orchestrator REST API（D3 冻结后）。</p>
      <span class="mono spec">{{ spec }}</span>
    </div>
  </div>
</template>

<style scoped>
.placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}

.frame {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-6);
  background: var(--surface);
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-lg);
}

.glyph {
  width: 48px;
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--accent-dim);
  border: 1px solid var(--accent-glow);
  border-radius: var(--radius-md);
  color: var(--accent-strong);
  font-size: var(--text-xl);
}

h2 {
  font-size: var(--text-xl);
  font-weight: 600;
}

.desc {
  color: var(--text-3);
  font-size: var(--text-sm);
}

.spec {
  color: var(--text-3);
  font-size: var(--text-xs);
}
</style>
