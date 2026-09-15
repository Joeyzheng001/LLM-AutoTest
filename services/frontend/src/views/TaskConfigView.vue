<script setup lang="ts">
// 任务配置页（07 页 01 方向）：新建流水线任务的表单骨架（演示态，提交对接 D3 API 后启用）。
// D8：鉴权/造数接口的前置数据在流程启动前预置于 fixtures，此处仅声明。
// 仓库口径：从 Git 服务拉取仓库列表，每仓指定「被测分支 + 基线分支」（供 code_review 差异定位）。
import { computed, reactive, ref } from 'vue'
import { fetchGitRepos, type GitRepoInfo } from '../mock/git'

const protocols = ['rest', 'grpc', 'graphql', 'soap', 'mq'] as const
const selectedProtocols = ref<string[]>(['rest', 'grpc'])

interface RepoRow {
  name: string
  testedBranch: string
  baselineBranch: string
  isFallback: boolean
}

const form = reactive({
  title: '',
  docPath: '',
  docFormat: 'md',
  fixturesNote: '',
})

/* Git 仓库拉取（mock：模拟内部 GitLab API） */
const gitServerUrl = ref('http://172.25.200.46')
const repos = reactive<GitRepoInfo[]>([])
const loadingRepos = ref(false)
const loadedOnce = ref(false)

async function loadRepos(): Promise<void> {
  loadingRepos.value = true
  try {
    const fetched = await fetchGitRepos(gitServerUrl.value)
    repos.splice(0, repos.length, ...fetched)
    loadedOnce.value = true
    // 默认选两个真实仓库（G3：≥2），兜底仓库由用户显式添加勾选
    if (!repoRows.length) {
      addRepoByName('user-service')
      addRepoByName('order-center')
    }
  } finally {
    loadingRepos.value = false
  }
}

const repoRows = reactive<RepoRow[]>([])

function repoInfo(name: string): GitRepoInfo | undefined {
  return repos.find((repo) => repo.name === name)
}

function pickRepo(event: Event): void {
  // 从下拉选仓：默认被测分支 = 仓库默认分支，基线分支 = master（无则首项）
  const name = (event.target as HTMLSelectElement).value
  addRepoByName(name)
  // 选择后复位下拉，便于再选下一个
  ;(event.target as HTMLSelectElement).value = ''
}

function addRepoByName(name: string): void {
  const info = repoInfo(name)
  if (!info || repoRows.some((row) => row.name === name)) return
  const baseline = info.branches.includes('master') ? 'master' : info.branches[0]
  repoRows.push({
    name: info.name,
    testedBranch: info.defaultBranch,
    baselineBranch: baseline,
    isFallback: info.name.startsWith('fallback'),
  })
}

function removeRepo(index: number): void {
  repoRows.splice(index, 1)
}

const branchesOf = computed(() => (name: string) => repoInfo(name)?.branches ?? [])

const gitHint = computed(() =>
  loadedOnce.value
    ? `已拉取 ${repos.length} 个仓库`
    : '先拉取仓库列表，再为每个仓库选择被测/基线分支',
)

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

      <!-- 被测仓库（G3：≥2；D5：兜底仓库须勾选分列；每仓被测+基线双分支） -->
      <section class="field-group">
        <header class="group-title">
          被测仓库
          <span class="group-note"
            >至少 2 个（G3）；每仓指定被测分支与基线分支；兜底仓库结果单独分列（D5）</span
          >
        </header>

        <!-- Git 拉取行 -->
        <div class="git-fetch-row">
          <input v-model="gitServerUrl" type="text" class="mono" placeholder="Git 服务地址" />
          <button class="ghost-btn" type="button" :disabled="loadingRepos" @click="loadRepos">
            {{ loadingRepos ? '拉取中…' : '从 Git 拉取仓库列表' }}
          </button>
          <span class="git-hint">{{ gitHint }}</span>
        </div>

        <!-- 仓库选择 + 分支选择 -->
        <div class="repo-head">
          <span>仓库</span><span>被测分支</span><span>基线分支</span><span>兜底</span><span />
        </div>

        <div v-if="!repoRows.length" class="repo-empty">
          {{ loadedOnce ? '从上方下拉添加仓库' : '尚未拉取仓库列表' }}
        </div>

        <template v-if="loadedOnce">
          <div class="repo-add-row">
            <select class="repo-select" @change="pickRepo">
              <option value="" disabled selected>选择仓库…</option>
              <option v-for="repo in repos" :key="repo.name" :value="repo.name">
                {{ repo.name }}
              </option>
            </select>
          </div>
        </template>

        <div v-for="(row, index) in repoRows" :key="row.name" class="repo-row">
          <div class="repo-name-cell">
            <span class="repo-name mono">{{ row.name }}</span>
            <span class="repo-url mono">{{ repoInfo(row.name)?.url }}</span>
          </div>
          <select v-model="row.testedBranch" class="mono">
            <option v-for="branch in branchesOf(row.name)" :key="branch" :value="branch">
              {{ branch }}
            </option>
          </select>
          <select v-model="row.baselineBranch" class="mono">
            <option v-for="branch in branchesOf(row.name)" :key="branch" :value="branch">
              {{ branch }}
            </option>
          </select>
          <input v-model="row.isFallback" type="checkbox" :aria-label="`兜底仓库 ${row.name}`" />
          <button
            class="icon-btn"
            type="button"
            :disabled="repoRows.length <= 2"
            @click="removeRepo(index)"
          >
            移除
          </button>
        </div>

        <p v-if="repoRows.length" class="branch-note">
          基线分支用于 code_review 变更定位：仅被测分支相对基线的差异部分进入 Review 范围。
        </p>
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
  max-width: 860px;
  margin: 0 auto;
}

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  padding: var(--space-5);
}

.field-group {
  margin-bottom: var(--space-5);
}

.group-title {
  display: flex;
  flex-wrap: wrap;
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
  padding: var(--space-2) var(--space-3);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-1);
  font-size: var(--text-md);
  outline: none;
  transition: border-color 0.15s;
}

input[type='text'],
textarea {
  width: 100%;
}

input[type='text']:focus,
textarea:focus,
select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-dim);
}

/* Git 拉取行 */
.git-fetch-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}

.git-fetch-row input {
  flex: 1;
}

.git-hint {
  font-size: var(--text-xs);
  color: var(--text-3);
  white-space: nowrap;
}

/* 仓库表格 */
.repo-head,
.repo-row,
.repo-add-row {
  display: grid;
  grid-template-columns: 1.6fr 1fr 1fr 40px 56px;
  gap: var(--space-2);
  align-items: center;
}

.repo-head {
  font-size: var(--text-xs);
  color: var(--text-3);
  padding: 0 var(--space-1) var(--space-1);
}

.repo-empty {
  padding: var(--space-4);
  text-align: center;
  color: var(--text-3);
  font-size: var(--text-sm);
  border: 1px dashed var(--border);
  border-radius: var(--radius-md);
  margin-bottom: var(--space-2);
}

.repo-select {
  width: 100%;
  color: var(--text-2);
}

.repo-name-cell {
  display: flex;
  flex-direction: column;
  gap: 0;
  min-width: 0;
}

.repo-name {
  font-size: var(--text-md);
  color: var(--text-1);
}

.repo-url {
  font-size: var(--text-xs);
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.repo-row {
  padding: var(--space-2) var(--space-1);
  margin-bottom: var(--space-2);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.repo-row input[type='checkbox'] {
  accent-color: var(--warning);
}

.branch-note {
  font-size: var(--text-xs);
  color: var(--text-3);
  margin-top: var(--space-1);
}

.ghost-btn {
  padding: var(--space-2) var(--space-4);
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
  background: var(--accent);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  color: #ffffff;
  font-size: var(--text-md);
  font-weight: 500;
  cursor: pointer;
}

.primary-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.submit-note {
  font-size: var(--text-xs);
  color: var(--text-3);
}
</style>
