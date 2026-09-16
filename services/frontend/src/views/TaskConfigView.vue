<script setup lang="ts">
// 任务配置页（07 页 01 · 真实提交）：多文档上传（C-10，base64）+ 本地仓库路径（R-CODE-01 口径）+
// 被测服务地址 + 断点续跑开关 → POST /api/v1/jobs（后台驱动同一 PipelineRunner，F-ORCH-01）。
// 红线：协议范围由源码扫描自动识别（非表单参数）；fixtures 前置数据（D8）在流程启动前预置，不在本表单收集。
import { computed, reactive, ref } from 'vue'
import { submitJob } from '../api/client'
import type { JobSubmitPayload } from '../api/types'

interface RepoRow {
  name: string
  path: string
  branch: string
  baselineBranch: string
  language: 'java' | 'python' // C-13：仓库语言（扫描器分流）
  isFallback: boolean
}

// C-10：多文档（至少 1 份；三格式可混传，LLM 单次解析分节拼接）
interface DocRow {
  name: string
  size: number
  file: File
}

const form = reactive({
  jobId: '',
  restUrl: 'http://127.0.0.1:9000',
  resume: true,
})

const docFiles = reactive<DocRow[]>([])
const docError = ref('')
const repos = reactive<RepoRow[]>([
  {
    name: 'user-service',
    path: '',
    branch: 'main',
    baselineBranch: '',
    language: 'java',
    isFallback: false,
  },
  {
    name: 'order-center',
    path: '',
    branch: 'main',
    baselineBranch: '',
    language: 'java',
    isFallback: false,
  },
])

const submitting = ref(false)
const submitError = ref('')
const submittedJobId = ref('')

function addRepo(): void {
  repos.push({
    name: '',
    path: '',
    branch: 'main',
    baselineBranch: '',
    language: 'java',
    isFallback: false,
  })
}

function removeRepo(index: number): void {
  repos.splice(index, 1)
}

function removeDoc(index: number): void {
  docFiles.splice(index, 1)
}

const reposValid = computed(
  () =>
    repos.length >= 1 &&
    repos.every((row) => row.name.trim() !== '' && row.path.trim().startsWith('/')),
)

const formValid = computed(
  () => docFiles.length >= 1 && reposValid.value && form.restUrl.startsWith('http'),
)

function pickDocs(event: Event): void {
  const files = Array.from((event.target as HTMLInputElement).files ?? [])
  docError.value = ''
  const input = event.target as HTMLInputElement
  for (const file of files) {
    const suffix = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
    if (!['.md', '.docx', '.xlsx'].includes(suffix)) {
      docError.value = `文档格式不支持（F-REQ-01~03：md/docx/xlsx）：${file.name}`
      continue
    }
    // 重名去重：同名文件不重复入列（后端落盘也会加序号，前端提前拦）
    if (docFiles.some((row) => row.name === file.name)) {
      continue
    }
    docFiles.push({ name: file.name, size: file.size, file })
  }
  input.value = '' // 允许移除后重选同一文件
}

function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('文档读取失败'))
    reader.onload = () => {
      // ArrayBuffer → base64（分块拼接避免大文件 String.fromCharCode 溢出）
      const bytes = new Uint8Array(reader.result as ArrayBuffer)
      let binary = ''
      const chunk = 0x8000
      for (let offset = 0; offset < bytes.length; offset += chunk) {
        binary += String.fromCharCode(...bytes.subarray(offset, offset + chunk))
      }
      resolve(btoa(binary))
    }
    reader.readAsArrayBuffer(file)
  })
}

async function handleSubmit(): Promise<void> {
  if (!formValid.value || submitting.value) return
  submitting.value = true
  submitError.value = ''
  submittedJobId.value = ''
  try {
    // C-10：docs 列表逐份 base64（后端分节拼接单次送 LLM）
    const docs = await Promise.all(
      docFiles.map(async (row) => ({
        doc_name: row.name,
        doc_content_b64: await readFileAsBase64(row.file),
      })),
    )
    const payload: JobSubmitPayload = {
      docs,
      repos: repos.map((row) => ({
        name: row.name.trim(),
        path: row.path.trim(),
        branch: row.branch.trim() || 'main',
        // C-09：基线分支可选（空 = 不比对；非空 → code_review 派生变更范围视图）
        ...(row.baselineBranch.trim() ? { baseline_branch: row.baselineBranch.trim() } : {}),
        is_fallback: row.isFallback,
        // C-13：仓库语言（java → java-parser；python → 内置 AST 扫描 Django/FastAPI/Flask）
        language: row.language,
      })),
      rest_url: form.restUrl.trim(),
      resume: form.resume,
    }
    const trimmedJobId = form.jobId.trim()
    if (trimmedJobId && !trimmedJobId.startsWith('job-')) {
      submitError.value = '任务 ID 须以 job- 开头（横切 ID 约定）；留空可自动生成'
      return
    }
    if (trimmedJobId) payload.job_id = trimmedJobId
    const result = await submitJob(payload)
    submittedJobId.value = result.job_id
  } catch (err) {
    submitError.value = err instanceof Error ? err.message : '提交失败'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="config">
    <form class="card" @submit.prevent="handleSubmit">
      <!-- 需求文档（三格式 F-REQ-01~03；C-10 可多份） -->
      <section class="field-group">
        <header class="group-title">
          需求文档
          <span class="group-note"
            >可多份（C-10）；支持 md / docx / xlsx（F-REQ-01~03），Schema
            校验由后端统一执行（F-REQ-04）</span
          >
        </header>
        <label class="doc-upload" for="doc-file">
          <input id="doc-file" type="file" accept=".md,.docx,.xlsx" multiple @change="pickDocs" />
          <span class="doc-placeholder">选择需求文档文件（可多选，至少 1 份）…</span>
        </label>
        <ul v-if="docFiles.length" class="doc-list">
          <li v-for="(row, index) in docFiles" :key="row.name" class="doc-item">
            <span class="mono">{{ row.name }}</span>
            <span class="doc-size mono">{{ Math.round(row.size / 1024) }} KB</span>
            <button class="icon-btn" type="button" @click="removeDoc(index)">移除</button>
          </li>
        </ul>
        <p v-if="docError" class="field-error" role="alert">{{ docError }}</p>
        <p class="switch-note">
          多文档在后端按文件名分节拼接后单次送 LLM 解析（需求条目统一编号）；至少上传 1 份。
        </p>
      </section>

      <!-- 被测仓库（C-10：≥1；R-CODE-01 本地路径口径；D5：兜底分列） -->
      <section class="field-group">
        <header class="group-title">
          被测仓库
          <span class="group-note"
            >至少 1
            个（C-10；验收演示仍双仓库口径）；本地绝对路径口径（R-CODE-01，检出正确分支后指向工作区目录）；兑底仓库结果单独分列（D5）</span
          >
        </header>

        <div class="repo-head">
          <span>仓库名</span><span>本地绝对路径</span><span>语言</span><span>分支</span
          ><span>基线分支</span><span>兑底</span><span />
        </div>

        <div v-for="(row, index) in repos" :key="index" class="repo-row">
          <input
            v-model="row.name"
            type="text"
            class="mono"
            placeholder="user-service"
            :aria-label="`仓库 ${index + 1} 名称`"
          />
          <input
            v-model="row.path"
            type="text"
            class="mono"
            placeholder="/Users/zz/code/user-service"
            :aria-label="`仓库 ${index + 1} 路径`"
          />
          <select
            v-model="row.language"
            class="mono repo-language"
            :aria-label="`仓库 ${index + 1} 语言`"
          >
            <option value="java">Java</option>
            <option value="python">Python</option>
          </select>
          <input
            v-model="row.branch"
            type="text"
            class="mono"
            placeholder="main"
            :aria-label="`仓库 ${index + 1} 分支`"
          />
          <input
            v-model="row.baselineBranch"
            type="text"
            class="mono"
            placeholder="可选，如 main"
            :aria-label="`仓库 ${index + 1} 基线分支`"
          />
          <input
            v-model="row.isFallback"
            type="checkbox"
            :aria-label="`仓库 ${index + 1} 兜底标记`"
          />
          <button
            class="icon-btn"
            type="button"
            :disabled="repos.length <= 1"
            @click="removeRepo(index)"
          >
            移除
          </button>
        </div>

        <button class="ghost-btn" type="button" @click="addRepo">+ 添加仓库</button>
        <p v-if="!reposValid" class="field-error">
          每个仓库需有名称与绝对路径（以 / 开头），且仓库数 ≥ 1。
        </p>
        <p class="switch-note">
          基线分支（C-09，可选）：非空时后端按 merge-base 做双分支 diff，CodeReview
          页展示变更范围视图；留空 = 不比对。仓库须为本地 Git 仓库且分支须存在。语言（C-13）：Java
          仓走 java-parser；Python 仓（Django / FastAPI / Flask）走内置 AST 扫描，同口径产出。
        </p>
      </section>

      <!-- 被测服务 + 断点续跑 -->
      <section class="field-group">
        <header class="group-title">执行配置</header>
        <label class="field">
          <span class="field-label">被测服务地址（REST）</span>
          <input
            v-model="form.restUrl"
            type="text"
            class="mono"
            placeholder="http://127.0.0.1:9000"
          />
        </label>
        <label class="field">
          <span class="field-label">任务 ID（可选，留空自动生成；须以 job- 开头）</span>
          <input v-model="form.jobId" type="text" class="mono" placeholder="job-web-…" />
        </label>
        <label class="switch-row">
          <input v-model="form.resume" type="checkbox" />
          <span>
            断点续跑（F-ORCH-02）：检测到历史产物时跳过已完成节点；关闭则清空该 job 目录重跑
          </span>
        </label>
        <p class="switch-note">
          协议范围由源码扫描自动识别（R-CODE-01，非表单参数）；鉴权/造数等前置数据须在流程启动前预置于
          fixtures（D8），不在本表单收集。
        </p>
      </section>

      <!-- 提交 -->
      <div class="submit-row">
        <button class="primary-btn" type="submit" :disabled="!formValid || submitting">
          {{ submitting ? '提交中…' : '启动全流程（F-ORCH-01）' }}
        </button>
        <span v-if="submittedJobId" class="submit-ok" role="status">
          已提交：<span class="mono">{{ submittedJobId }}</span> —— 到「流水线总览」页查看进度
        </span>
        <span v-else class="submit-note">与 CLI 走同一 PipelineRunner（Web 与命令行入口等价）</span>
      </div>
      <p v-if="submitError" class="field-error" role="alert">{{ submitError }}</p>
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

.field-error {
  margin: var(--space-1) 0 0;
  font-size: var(--text-sm);
  color: var(--warning);
}

/* 文档上传 */
.doc-upload {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4);
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-md);
  cursor: pointer;
  background: var(--surface-2);
  transition: border-color 0.15s;
}

.doc-upload:hover,
.doc-upload:focus-within {
  border-color: var(--accent);
}

.doc-upload input[type='file'] {
  color: var(--text-2);
  font-size: var(--text-sm);
}

/* C-10：已选文档清单 */
.doc-list {
  list-style: none;
  margin: var(--space-2) 0 0;
  padding: 0;
}

.doc-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  margin-bottom: var(--space-1);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  color: var(--accent-strong);
}

.doc-size {
  margin-left: auto;
  font-size: var(--text-xs);
  color: var(--text-3);
}

.doc-placeholder {
  font-size: var(--text-sm);
  color: var(--text-3);
}

/* 仓库表格 */
.repo-head,
.repo-row {
  display: grid;
  grid-template-columns: 150px 1fr 92px 90px 90px 40px 56px;
  gap: var(--space-2);
  align-items: center;
}

.repo-head {
  font-size: var(--text-xs);
  color: var(--text-3);
  padding: 0 var(--space-1) var(--space-1);
}

.repo-row {
  padding: var(--space-2) var(--space-1);
  margin-bottom: var(--space-2);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.repo-row input[type='text'] {
  width: 100%;
  padding: var(--space-2) var(--space-3);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-1);
  font-size: var(--text-sm);
}

.repo-row input[type='text']:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-dim);
  outline: none;
}

.repo-row input[type='checkbox'] {
  accent-color: var(--warning);
}

/* C-13：语言选择（与文本框同视觉口径） */
.repo-language {
  width: 100%;
  padding: var(--space-2) var(--space-3);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-1);
  font-size: var(--text-sm);
}

.repo-language:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-dim);
  outline: none;
}

@media (max-width: 860px) {
  .repo-head,
  .repo-row {
    grid-template-columns: 1fr 1fr;
  }
}

/* 断点续跑开关 */
.switch-row {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  font-size: var(--text-sm);
  color: var(--text-2);
  cursor: pointer;
}

.switch-row input {
  accent-color: var(--accent);
}

.switch-note {
  margin: var(--space-2) 0 0;
  font-size: var(--text-xs);
  color: var(--text-3);
}

/* 按钮 */
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

/* 提交区 */
.submit-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
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

.submit-ok {
  font-size: var(--text-sm);
  color: var(--accent-strong);
}

.submit-note {
  font-size: var(--text-xs);
  color: var(--text-3);
}
</style>
