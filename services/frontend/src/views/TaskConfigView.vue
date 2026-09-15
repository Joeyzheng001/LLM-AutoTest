<script setup lang="ts">
// 任务配置页（07 页 01 方向）：新建流水线任务的表单骨架（演示态，提交对接 D3 API 后启用）。
// D8：鉴权/造数接口的前置数据在流程启动前预置于 fixtures，此处仅声明。
import { reactive, ref } from 'vue'

const protocols = ['rest', 'grpc', 'graphql', 'soap', 'mq'] as const
const selectedProtocols = ref<string[]>(['rest', 'grpc'])

interface RepoRow {
  name: string
  path: string
  branch: string
  isFallback: boolean
}

const form = reactive({
  title: '',
  docPath: '',
  docFormat: 'md',
  fixturesNote: '',
})

const repos = reactive<RepoRow[]>([
  {
    name: 'user-service',
    path: '/data/repos/user-service',
    branch: 'release/v2.3',
    isFallback: false,
  },
  { name: 'order-center', path: '/data/repos/order-center', branch: 'master', isFallback: false },
])

function addRepo(): void {
  repos.push({ name: '', path: '', branch: 'master', isFallback: false })
}

function removeRepo(index: number): void {
  repos.splice(index, 1)
}

const submitDisabled = ref(true) // 演示态：待 orchestrator REST API（D3）落地后启用
</script>

<template>
  <div class="config">
    <form class="card" @submit.prevent>
      <!-- 基本信息 -->
      <section class="field-group">
        <header class="group-title">基本信息</header>
        <label class="field">
          <span class="field-label">任务名称</span>
          <input v-model="form.title" type="text" placeholder="例：用户服务 V2.4 回归" />
        </label>
        <label class="field">
          <span class="field-label">需求文档</span>
          <span class="field-inline">
            <input
              v-model="form.docPath"
              type="text"
              class="mono"
              placeholder="/data/docs/需求说明.md"
            />
            <select v-model="form.docFormat">
              <option value="md">md</option>
              <option value="docx">docx</option>
              <option value="xlsx">xlsx</option>
            </select>
          </span>
        </label>
      </section>

      <!-- 仓库清单（G3：≥2；D5：兜底仓库须勾选分列） -->
      <section class="field-group">
        <header class="group-title">
          被测仓库
          <span class="group-note">至少 2 个（G3）；兜底仓库结果单独分列（D5）</span>
        </header>
        <div class="repo-head">
          <span>名称</span><span>本地路径（已检出）</span><span>分支</span><span>兜底</span><span />
        </div>
        <div v-for="(repo, index) in repos" :key="index" class="repo-row">
          <input v-model="repo.name" type="text" placeholder="repo-name" class="mono" />
          <input v-model="repo.path" type="text" placeholder="/data/repos/…" class="mono" />
          <input v-model="repo.branch" type="text" class="mono" />
          <input v-model="repo.isFallback" type="checkbox" :aria-label="`兜底仓库 ${repo.name}`" />
          <button
            class="icon-btn"
            type="button"
            :disabled="repos.length <= 2"
            @click="removeRepo(index)"
          >
            移除
          </button>
        </div>
        <button class="ghost-btn" type="button" @click="addRepo">+ 添加仓库</button>
      </section>

      <!-- 协议范围（D2：非 REST 至少 1 种，gRPC 优先） -->
      <section class="field-group">
        <header class="group-title">
          协议范围
          <span class="group-note">非 REST 至少覆盖 1 种（D2，gRPC 优先）</span>
        </header>
        <div class="protocol-row">
          <label v-for="protocol in protocols" :key="protocol" class="protocol-chip">
            <input v-model="selectedProtocols" type="checkbox" :value="protocol" />
            <span class="mono">{{ protocol }}</span>
          </label>
        </div>
      </section>

      <!-- D8 前置数据声明 -->
      <section class="field-group">
        <header class="group-title">
          前置数据（fixtures）
          <span class="group-note">鉴权/造数接口须在启动前预置（D8），不计为节点内人工介入</span>
        </header>
        <label class="field">
          <span class="field-label">说明</span>
          <textarea
            v-model="form.fixturesNote"
            rows="3"
            placeholder="例：登录态 token 预置于 fixtures/auth.json；测试账号 user-001 由流程启动脚本创建"
          />
        </label>
      </section>

      <div class="submit-row">
        <button class="primary-btn" type="submit" :disabled="submitDisabled">启动流水线</button>
        <span class="submit-note">提交端点待 orchestrator REST API 冻结（D3）后启用</span>
      </div>
    </form>
  </div>
</template>

<style scoped>
.config {
  max-width: 760px;
  margin: 0 auto;
}

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-5);
}

.field-group {
  margin-bottom: var(--space-5);
}

.group-title {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  font-size: var(--text-lg);
  font-weight: 600;
  margin-bottom: var(--space-3);
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--border);
}

.group-note {
  font-size: var(--text-xs);
  font-weight: 400;
  color: var(--text-3);
}

.field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-bottom: var(--space-3);
}

.field-label {
  font-size: var(--text-sm);
  color: var(--text-2);
}

.field-inline {
  display: flex;
  gap: var(--space-2);
}

input[type='text'],
textarea,
select {
  width: 100%;
  padding: var(--space-2) var(--space-3);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-1);
  font-size: var(--text-md);
  outline: none;
  transition: border-color 0.15s;
}

input[type='text']:focus,
textarea:focus,
select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-dim);
}

select {
  width: auto;
}

/* 仓库表格 */
.repo-head,
.repo-row {
  display: grid;
  grid-template-columns: 1.1fr 2fr 1fr 48px 64px;
  gap: var(--space-2);
  align-items: center;
}

.repo-head {
  font-size: var(--text-xs);
  color: var(--text-3);
  padding: 0 var(--space-1) var(--space-1);
}

.repo-row {
  margin-bottom: var(--space-2);
}

.repo-row input[type='checkbox'] {
  accent-color: var(--warning);
}

.ghost-btn {
  margin-top: var(--space-1);
  padding: var(--space-2) var(--space-4);
  background: transparent;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-sm);
  color: var(--text-2);
  cursor: pointer;
  font-size: var(--text-sm);
}

.ghost-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.icon-btn {
  padding: var(--space-1) var(--space-2);
  background: transparent;
  border: none;
  color: var(--text-3);
  cursor: pointer;
  font-size: var(--text-xs);
}

.icon-btn:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}

/* 协议选择 */
.protocol-row {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.protocol-chip {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: var(--text-sm);
}

.protocol-chip:has(input:checked) {
  border-color: var(--accent-glow);
  background: var(--accent-dim);
  color: var(--accent-strong);
}

/* 提交区 */
.submit-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding-top: var(--space-4);
  border-top: 1px solid var(--border);
}

.primary-btn {
  padding: var(--space-2) var(--space-5);
  background: var(--accent-dim);
  border: 1px solid var(--accent-glow);
  border-radius: var(--radius-sm);
  color: var(--accent-strong);
  font-size: var(--text-md);
  font-weight: 500;
  cursor: pointer;
}

.primary-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.submit-note {
  font-size: var(--text-xs);
  color: var(--text-3);
}
</style>
