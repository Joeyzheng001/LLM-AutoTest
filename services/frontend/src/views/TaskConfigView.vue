<script setup lang="ts">
// 任务配置页（07 页 01 · 真实提交）：多文档上传（C-10，base64）+ 本地仓库路径（R-CODE-01 口径）+
// 被测服务地址 + 断点续跑开关 + 鉴权配置（C-15，可选）→ POST /api/v1/jobs（后台驱动同一 PipelineRunner，F-ORCH-01）。
// 红线：协议范围由源码扫描自动识别（非表单参数）；鉴权走表单兜底（C-15，D8 升版），
// 其余 fixtures 前置数据仍在流程启动前预置，不在本表单收集。
import { computed, onMounted, reactive, ref } from 'vue'
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

// C-15：鉴权配置（可选区块；默认收起，开启后执行前登录被测服务注入凭据头）
const authEnabled = ref(false)
const auth = reactive({
  loginUrl: '',
  username: '',
  password: '',
  insecureTls: true,
})

// 真实模型配置：随任务提交；可由用户选择仅保存在当前浏览器，避免重复填写。
const LLM_BROWSER_CONFIG_KEY = 'llm-autotest.llm-config.v1'
const llmEnabled = ref(true)
const rememberLlm = ref(true)
const llm = reactive({
  apiKey: '',
  model: 'MiniMax-M3',
  baseUrl: 'https://api.minimaxi.com/v1',
  maxTokens: 16384,
})
const e2eEnabled = ref(false)
const e2e = reactive({
  baseUrl: 'http://47.83.181.191:8080',
  generateCases: false,
  executeCases: false,
  insecureTls: true,
})

onMounted(() => {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(LLM_BROWSER_CONFIG_KEY) ?? 'null')
    if (!saved || typeof saved !== 'object') return
    const config = saved as Record<string, unknown>
    if (typeof config.apiKey === 'string') llm.apiKey = config.apiKey
    if (typeof config.model === 'string') llm.model = config.model
    if (typeof config.baseUrl === 'string') llm.baseUrl = config.baseUrl
    if (typeof config.maxTokens === 'number') llm.maxTokens = config.maxTokens
  } catch {
    // 私密浏览/损坏缓存时继续使用空配置，不阻断任务提交。
  }
})

function saveLlmConfig(): void {
  try {
    if (!rememberLlm.value) {
      localStorage.removeItem(LLM_BROWSER_CONFIG_KEY)
      return
    }
    localStorage.setItem(
      LLM_BROWSER_CONFIG_KEY,
      JSON.stringify({
        apiKey: llm.apiKey.trim(),
        model: llm.model,
        baseUrl: llm.baseUrl.trim(),
        maxTokens: llm.maxTokens,
      }),
    )
  } catch {
    // 浏览器拒绝存储时只影响预填，不影响本次模型调用。
  }
}

const docFiles = reactive<DocRow[]>([])
const docError = ref('')
const docInput = ref<HTMLInputElement | null>(null)
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

// C-15：开启鉴权时三项必填 + login_url 须为 http(s)
const authValid = computed(
  () =>
    !authEnabled.value ||
    (auth.loginUrl.trim().startsWith('http') &&
      auth.username.trim() !== '' &&
      auth.password !== ''),
)

const llmValid = computed(
  () =>
    !llmEnabled.value ||
    (llm.apiKey.trim().length >= 8 &&
      llm.model.trim() !== '' &&
      llm.baseUrl.trim().startsWith('http') &&
      llm.maxTokens >= 1024 &&
      llm.maxTokens <= 16384),
)

const formValid = computed(
  () =>
    docFiles.length >= 1 &&
    reposValid.value &&
    form.restUrl.startsWith('http') &&
    authValid.value &&
    llmValid.value,
)

const formInvalidReasons = computed(() => {
  const reasons: string[] = []
  if (docFiles.length < 1) reasons.push('请选择至少一份需求文档')
  if (!reposValid.value) reasons.push('补全仓库名称和本地绝对路径')
  if (!form.restUrl.trim().startsWith('http')) reasons.push('填写合法的 REST 服务地址')
  if (!authValid.value) reasons.push('补全鉴权地址、用户名和密码')
  if (!llmValid.value) {
    reasons.push('填写合法的 MiniMax API Key、模型和 Base URL，或关闭本次模型配置')
  }
  return reasons
})

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

function openDocPicker(): void {
  // showPicker 是 Chromium 的直接用户激活通道；其他浏览器退回原生 click。
  const input = docInput.value
  if (!input) {
    docError.value = '文件选择器尚未就绪，请刷新页面后重试。'
    return
  }
  try {
    if (typeof input.showPicker === 'function') input.showPicker()
    else input.click()
  } catch {
    // Safari / 受限 WebView 不支持 showPicker 时，click 仍是标准后备。
    input.click()
  }
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
    if (llmEnabled.value) saveLlmConfig()
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
      // C-15：鉴权配置（可选；开启才随提交发送，凭据只落本机 job_input.json）
      ...(authEnabled.value
        ? {
            auth: {
              login_url: auth.loginUrl.trim(),
              username: auth.username.trim(),
              password: auth.password,
              insecure_tls: auth.insecureTls,
            },
          }
        : {}),
      ...(llmEnabled.value
        ? {
            llm: {
              api_key: llm.apiKey.trim(),
              model: llm.model,
              base_url: llm.baseUrl.trim(),
              max_tokens: llm.maxTokens,
            },
          }
        : {}),
      ...(e2eEnabled.value
        ? {
            e2e: {
              base_url: e2e.baseUrl.trim(),
              generate_cases: e2e.generateCases,
              execute_cases: e2e.executeCases,
              insecure_tls: e2e.insecureTls,
            },
          }
        : {}),
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
        <input
          id="doc-file"
          ref="docInput"
          class="doc-file-input"
          type="file"
          accept=".md,.docx,.xlsx"
          multiple
          @change="pickDocs"
        />
        <button class="doc-upload" type="button" @click="openDocPicker">
          选择需求文档文件（可多选，至少 1 份）…
        </button>
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

      <section class="field-group">
        <header class="group-title">前端 E2E 功能测试（可选）</header>
        <label class="switch-row"><input v-model="e2eEnabled" type="checkbox" /><span>启用前端功能测试配置</span></label>
        <template v-if="e2eEnabled">
          <label class="field"><span class="field-label">前端访问地址</span><input v-model="e2e.baseUrl" type="url" placeholder="http://47.83.181.191:8080" /></label>
          <label class="switch-row"><input v-model="e2e.generateCases" type="checkbox" /><span>生成 E2E 测试用例</span></label>
          <label class="switch-row"><input v-model="e2e.executeCases" type="checkbox" :disabled="!e2e.generateCases" /><span>执行 E2E 用例（Playwright）</span></label>
          <label class="switch-row"><input v-model="e2e.insecureTls" type="checkbox" /><span>跳过 HTTPS 证书校验（测试环境自签名证书）</span></label>
          <p class="switch-note">默认关闭，不影响接口用例；执行前会使用真实浏览器访问上述地址。</p>
        </template>
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
          协议范围由源码扫描自动识别（R-CODE-01，非表单参数）；鉴权可由下方「鉴权配置」提供（C-15，D8
          升版：fixtures 优先、表单兜底）；造数等其余前置数据仍预置于 fixtures，不在本表单收集。
        </p>
      </section>

      <section class="field-group">
        <header class="group-title">大模型配置</header>
        <label class="switch-row">
          <input v-model="llmEnabled" type="checkbox" />
          <span>使用本次填写的 MiniMax 配置运行语义节点</span>
        </label>
        <template v-if="llmEnabled">
          <label class="switch-row">
            <input v-model="rememberLlm" type="checkbox" @change="saveLlmConfig" />
            <span>在此浏览器记住模型配置和 API Key</span>
          </label>
          <label class="field">
            <span class="field-label">MiniMax API Key</span>
            <input
              v-model="llm.apiKey"
              type="password"
              autocomplete="new-password"
              placeholder="sk-…"
            />
          </label>
          <div class="two-col">
            <label class="field">
              <span class="field-label">模型</span>
              <select v-model="llm.model">
                <option value="MiniMax-M3">MiniMax M3</option>
                <option value="MiniMax-M2.7">MiniMax M2.7</option>
                <option value="MiniMax-M2.5">MiniMax M2.5</option>
              </select>
            </label>
            <label class="field">
              <span class="field-label">单次最大输出 Token</span>
              <input
                v-model.number="llm.maxTokens"
                type="number"
                min="1024"
                max="16384"
                step="1024"
              />
            </label>
          </div>
          <label class="field">
            <span class="field-label">OpenAI 兼容地址</span>
            <input v-model="llm.baseUrl" type="url" class="mono" />
          </label>
          <p v-if="!llmValid" class="field-error">
            请填写有效 API Key、模型、服务地址和 1024–16384 的 Token 上限。
          </p>
          <p class="switch-note">
            勾选“记住”后，密钥仅保存在当前浏览器的本地存储，不会写入 job_input、产物或 LLM 调用留痕；
            取消勾选会立即清除。需求解析、接口还原、Code Review、用例生成会按章节或实体分批调用，再由程序校验并合并。
          </p>
        </template>
      </section>

      <!-- C-15：鉴权配置（可选；执行前登录被测服务注入凭据头，数值/凭据不经 LLM） -->
      <section class="field-group">
        <header class="group-title">鉴权配置（可选，C-15）</header>
        <label class="switch-row">
          <input v-model="authEnabled" type="checkbox" />
          <span>被测服务需要登录（执行/复测前自动登录并注入凭据头）</span>
        </label>
        <template v-if="authEnabled">
          <label class="field">
            <span class="field-label">登录接口地址（POST，JSON {username, password}）</span>
            <input
              v-model="auth.loginUrl"
              type="text"
              class="mono"
              placeholder="https://10.50.16.19:8443/api/auth/login/"
            />
          </label>
          <div class="two-col">
            <label class="field">
              <span class="field-label">用户名</span>
              <input v-model="auth.username" type="text" autocomplete="off" placeholder="admin" />
            </label>
            <label class="field">
              <span class="field-label">密码</span>
              <input
                v-model="auth.password"
                type="password"
                autocomplete="new-password"
                placeholder="••••••••"
              />
            </label>
          </div>
          <label class="switch-row">
            <input v-model="auth.insecureTls" type="checkbox" />
            <span>跳过证书校验（https 自签证书场景；被测环境常为自签）</span>
          </label>
          <p v-if="!authValid" class="field-error">
            开启鉴权后：登录地址须为 http(s)、用户名与密码必填
          </p>
          <p class="switch-note">
            登录响应含 token 字段 → Authorization: Token 注入；无 token 则回带 Set-Cookie
            会话。凭据仅随 job 落盘本机（job_input.json，不外露），不经 LLM。
          </p>
        </template>
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
      <p v-if="!formValid" class="submit-blockers" role="status">
        暂不能启动：{{ formInvalidReasons.join('；') }}。
      </p>
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

/* C-15：鉴权用户名/密码并排 */
.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
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

.doc-file-input {
  position: fixed;
  width: 1px;
  height: 1px;
  opacity: 0;
  pointer-events: none;
}

.doc-upload:hover,
.doc-upload:focus-within {
  border-color: var(--accent);
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

.submit-blockers {
  margin: var(--space-2) 0 0;
  color: var(--warning);
  font-size: var(--text-xs);
  line-height: 1.6;
}

.submit-note {
  font-size: var(--text-xs);
  color: var(--text-3);
}
</style>
