<script setup lang="ts">
// 任务配置页（07 页 01 · 真实提交）：多文档上传（C-10，base64）+ 本地仓库路径（R-CODE-01 口径）+
// 被测服务地址 + 断点续跑开关 + 鉴权配置（C-15，可选）→ POST /api/v1/jobs（后台驱动同一 PipelineRunner，F-ORCH-01）。
// 红线：协议范围由源码扫描自动识别（非表单参数）；鉴权走表单兜底（C-15，D8 升版），
// 接口前置夹具可在高级配置区编辑；密码、Token、模型 Key 仍由后端安全存储管理。
import { computed, onMounted, reactive, ref, watch } from 'vue'
import {
  getJobConfiguration,
  getRepositoryRoot,
  preflightJob,
  previewBranchDiff,
  submitJob,
  updateJobConfiguration,
  verifyAuthentication,
  verifyLlmConnection,
} from '../api/client'
import { navigateTo } from '../api/navigation'
import {
  DEFAULT_RELAY_MODEL,
  LLM_MODEL_GROUPS,
  LLM_PROTOCOL_OPTIONS,
  isKnownLlmModel,
  llmModelWarning,
} from '../api/llmModels'
import type {
  DiffPreviewResponse,
  FixtureStep,
  JobConfigurationUpdatePayload,
  JobSubmitPayload,
} from '../api/types'

const props = defineProps<{ initialJobId?: string }>()

interface DiffPreviewState {
  loading: boolean
  result: DiffPreviewResponse | null
  error: string
}

interface RepoRow {
  name: string
  address: string // 统一仓库地址：本地绝对路径 或 Git 地址（提交时自动识别 source）
  serviceBaseUrl: string
  branch: string
  commit: string // 提交号（多值，逗号/换行/空格分隔）
  baselineBranch: string
  diffMode: 'branch' | 'commit' // 变更范围来源：分支对比 或 提交号
  accessToken: string
  accessTokenConfigured: boolean
  language: 'java' | 'python' // C-13：仓库语言（扫描器分流）
  isFallback: boolean
  diffPreview: DiffPreviewState | null // 差异预览（仅前端状态，不提交）
}

// 每行是一对需求版本：被测版本必填，基线版本可选；单版本保持原文处理。
interface VersionDocRow {
  target: File | null
  baseline: File | null
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
  usernameField: 'username',
  passwordField: 'password',
  passwordTransform: 'plain' as 'plain' | 'base64_shift_2',
  tokenPath: 'auto',
  authorizationScheme: 'Token' as 'Token' | 'Bearer' | 'raw',
  responseHeaderPathsJson: '',
})
const verifyingAuth = ref(false)
const authVerificationMessage = ref('')
const verifyingLlm = ref(false)
const llmVerificationMessage = ref('')
const llmVerificationOk = ref(false)
const authSessionId = ref('')
const savedOnly = ref(false)

// 真实模型配置：随任务提交；浏览器只记住非敏感模型参数，API Key 由服务端 SecretStore 复用。
const LLM_BROWSER_CONFIG_KEY = 'llm-autotest.llm-config.v1'
const DRAFT_BROWSER_CONFIG_KEY = 'llm-autotest.task-draft.v1'
// 新任务默认复用部门服务级 LLM 配置；只有需要覆盖模型/Key 时才启用任务级配置。
const llmEnabled = ref(false)
const rememberLlm = ref(true)
const llm = reactive({
  apiKey: '',
  model: DEFAULT_RELAY_MODEL,
  protocol: 'openai-chat' as 'openai-chat' | 'openai-responses' | 'anthropic-messages',
  // 任务级覆盖默认不猜测官方地址；未启用覆盖时自动复用服务端中转站配置。
  baseUrl: '',
  maxTokens: 16384,
})
const selectedLlmModelWarning = computed(() => llmModelWarning(llm.model))
const hasKnownLlmModel = computed(() => isKnownLlmModel(llm.model))
const e2eEnabled = ref(false)
const restInsecureTls = ref(true)
const e2e = reactive({
  // 部门部署不绑定任何公网地址；由任务配置填写内网被测前端地址。
  baseUrl: '',
  generateCases: false,
  executeCases: false,
  insecureTls: true,
  authMode: 'reuse' as 'reuse' | 'browser',
})
const fixturesJson = ref('')
const fixturePlaceholder =
  '[{"name":"seed-user","phase":"setup","method":"POST","path":"/fixtures/users","body":{"id":1},"expected":"status=201","required":true}]'
const responseHeaderPathsPlaceholder =
  '{"usertoken":"body.token","userid":"body.userId","username":"body.userName"}'

onMounted(() => {
  void loadRepositoryRoot()
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(LLM_BROWSER_CONFIG_KEY) ?? 'null')
    if (!saved || typeof saved !== 'object') return
    const config = saved as Record<string, unknown>
    if (typeof config.model === 'string') llm.model = config.model
    if (typeof config.baseUrl === 'string') llm.baseUrl = config.baseUrl
    if (
      config.protocol === 'openai-chat' ||
      config.protocol === 'openai-responses' ||
      config.protocol === 'anthropic-messages'
    )
      llm.protocol = config.protocol
    if (typeof config.maxTokens === 'number') llm.maxTokens = config.maxTokens
    // 兼容并清理旧版本曾写入 localStorage 的 API Key；从此不再恢复或重新保存。
    if ('apiKey' in config) {
      delete config.apiKey
      localStorage.setItem(LLM_BROWSER_CONFIG_KEY, JSON.stringify(config))
    }
  } catch {
    // 私密浏览/损坏缓存时继续使用空配置，不阻断任务提交。
  }
})

async function loadRepositoryRoot(): Promise<void> {
  try {
    const result = await getRepositoryRoot()
    if (result.root.trim()) repositoryRoot.value = result.root.trim()
  } catch {
    // 兼容旧服务或未授权场景；提交时仍由后端进行最终路径校验。
  }
}

function saveLlmConfig(): void {
  try {
    if (!rememberLlm.value) {
      localStorage.removeItem(LLM_BROWSER_CONFIG_KEY)
      return
    }
    localStorage.setItem(
      LLM_BROWSER_CONFIG_KEY,
      JSON.stringify({
        model: llm.model,
        baseUrl: llm.baseUrl.trim(),
        protocol: llm.protocol,
        maxTokens: llm.maxTokens,
      }),
    )
  } catch {
    // 浏览器拒绝存储时只影响预填，不影响本次模型调用。
  }
}

const draftSavedAt = ref('')

function saveDraft(): void {
  try {
    localStorage.setItem(
      DRAFT_BROWSER_CONFIG_KEY,
      JSON.stringify({
        form: { ...form },
        repos: repos.map((repo) => {
          const { accessToken, accessTokenConfigured, diffPreview, ...safeRepo } = repo
          void accessToken
          void accessTokenConfigured
          void diffPreview
          return safeRepo
        }),
        auth: {
          loginUrl: auth.loginUrl,
          username: auth.username,
          insecureTls: auth.insecureTls,
          usernameField: auth.usernameField,
          passwordField: auth.passwordField,
          passwordTransform: auth.passwordTransform,
          tokenPath: auth.tokenPath,
          authorizationScheme: auth.authorizationScheme,
          responseHeaderPathsJson: auth.responseHeaderPathsJson,
        },
        authEnabled: authEnabled.value,
        llm: {
          model: llm.model,
          baseUrl: llm.baseUrl,
          protocol: llm.protocol,
          maxTokens: llm.maxTokens,
        },
        llmEnabled: llmEnabled.value,
        e2e: { ...e2e },
        e2eEnabled: e2eEnabled.value,
        fixturesJson: fixturesJson.value,
        restInsecureTls: restInsecureTls.value,
        savedAt: new Date().toISOString(),
      }),
    )
    draftSavedAt.value = new Date().toLocaleString()
  } catch {
    submitError.value = '本地草稿保存失败，请检查浏览器存储权限'
  }
}

function restoreDraft(): void {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(DRAFT_BROWSER_CONFIG_KEY) ?? 'null')
    if (!raw || typeof raw !== 'object') return
    const draft = raw as Record<string, unknown>
    if (draft.form && typeof draft.form === 'object') Object.assign(form, draft.form)
    if (Array.isArray(draft.repos) && draft.repos.length) {
      repos.splice(
        0,
        repos.length,
        ...draft.repos.map((repo) => ({
          ...(repo as RepoRow),
          accessToken: '',
          accessTokenConfigured: false,
          diffPreview: null,
        })),
      )
    }
    if (draft.auth && typeof draft.auth === 'object') Object.assign(auth, draft.auth)
    authEnabled.value = Boolean(draft.authEnabled)
    if (draft.llm && typeof draft.llm === 'object') Object.assign(llm, draft.llm)
    llmEnabled.value = Boolean(draft.llmEnabled)
    if (draft.e2e && typeof draft.e2e === 'object') Object.assign(e2e, draft.e2e)
    e2eEnabled.value = Boolean(draft.e2eEnabled)
    if (typeof draft.fixturesJson === 'string') fixturesJson.value = draft.fixturesJson
    restInsecureTls.value = Boolean(draft.restInsecureTls)
    draftSavedAt.value =
      typeof draft.savedAt === 'string' ? new Date(draft.savedAt).toLocaleString() : ''
  } catch {
    submitError.value = '本地草稿读取失败，已保留当前填写内容'
  }
}

const docError = ref('')
const documentRows = reactive<VersionDocRow[]>([{ target: null, baseline: null }])
const repos = reactive<RepoRow[]>([
  {
    name: 'user-service',
    address: '',
    serviceBaseUrl: '',
    branch: 'main',
    commit: '',
    baselineBranch: '',
    diffMode: 'branch',
    accessToken: '',
    accessTokenConfigured: false,
    language: 'java',
    isFallback: false,
    diffPreview: null,
  },
])

const submitting = ref(false)
const submitError = ref('')
const submittedJobId = ref('')
const editingJobId = ref('')
const existingDocs = ref<string[]>([])
const existingAuthPassword = ref(false)
const existingLlmKey = ref(false)
const repositoryRoot = ref('/opt/repos')
const loadingConfiguration = ref(false)
const preflightResult = ref<{
  ready: boolean
  checks: Array<{ name: string; status: 'ok' | 'error' | 'skipped'; message: string }>
} | null>(null)
const activeStep = ref('documents')

const configSteps = [
  { id: 'documents', label: '需求与仓库', hint: '文档版本、仓库地址' },
  { id: 'environment', label: '被测系统', hint: 'REST 地址、登录、E2E 页面' },
  { id: 'credentials', label: '模型配置', hint: '中转站与输出设置' },
  { id: 'confirm', label: '确认提交', hint: '校验并启动' },
] as const

function goToStep(stepId: string): void {
  activeStep.value = stepId
  document
    .getElementById(`config-step-${stepId}`)
    ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function addRepo(): void {
  repos.push({
    name: '',
    address: '',
    serviceBaseUrl: '',
    branch: 'main',
    commit: '',
    baselineBranch: '',
    diffMode: 'branch',
    accessToken: '',
    accessTokenConfigured: false,
    language: 'java',
    isFallback: false,
    diffPreview: null,
  })
}

function removeRepo(index: number): void {
  repos.splice(index, 1)
}

async function previewDiff(index: number): Promise<void> {
  const row = repos[index]
  if (!row) return
  const address = row.address.trim()
  if (!address) {
    row.diffPreview = { loading: false, result: null, error: '请先填写仓库地址' }
    return
  }
  const base = {
    name: row.name.trim() || address,
    address,
    branch: row.branch.trim() || 'main',
    ...(row.accessToken.trim() ? { access_token: row.accessToken.trim() } : {}),
  }
  if (row.diffMode === 'commit') {
    const commits = row.commit.split(/[,，\s]+/).filter(Boolean)
    if (!commits.length) {
      row.diffPreview = { loading: false, result: null, error: '请先填写至少一个提交号' }
      return
    }
    row.diffPreview = { loading: true, result: null, error: '' }
    try {
      const result = await previewBranchDiff({ ...base, commits })
      row.diffPreview = { loading: false, result, error: result.error || '' }
    } catch (error) {
      row.diffPreview = {
        loading: false,
        result: null,
        error: error instanceof Error ? error.message : '预览失败',
      }
    }
    return
  }
  const baseline = row.baselineBranch.trim()
  if (!baseline) {
    row.diffPreview = { loading: false, result: null, error: '请先填写基线分支' }
    return
  }
  row.diffPreview = { loading: true, result: null, error: '' }
  try {
    const result = await previewBranchDiff({ ...base, baseline_branch: baseline })
    row.diffPreview = { loading: false, result, error: result.error || '' }
  } catch (error) {
    row.diffPreview = {
      loading: false,
      result: null,
      error: error instanceof Error ? error.message : '预览失败',
    }
  }
}

function addDocumentRow(): void {
  documentRows.push({ target: null, baseline: null })
}

function removeDocumentRow(index: number): void {
  if (documentRows.length > 1) documentRows.splice(index, 1)
}

function isAbsoluteRepositoryPath(value: string): boolean {
  // POSIX / macOS / Linux，Windows 盘符路径，以及 UNC 网络共享路径。
  return /^(\/|[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)/.test(value.trim())
}

function classifyRepoSource(value: string): 'git' | 'local' {
  // 与后端契约一致：明确的 Git URL / scp-like 地址判为 git，否则按本地绝对路径处理。
  const v = value.trim()
  if (/^(https?|ssh|git|file):\/\//.test(v) || v.startsWith('git@')) return 'git'
  return 'local'
}

function parseResponseHeaderPaths(): Record<string, string> {
  const raw = auth.responseHeaderPathsJson.trim()
  if (!raw) return {}
  const parsed: unknown = JSON.parse(raw)
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('响应字段→请求头映射必须是 JSON 对象')
  }
  const entries = Object.entries(parsed)
  const headerName = /^[A-Za-z][A-Za-z0-9-]{0,63}$/
  const responsePath = /^[A-Za-z_][A-Za-z0-9_.-]{0,127}$/
  if (
    entries.length > 8 ||
    entries.some(
      ([header, path]) =>
        typeof path !== 'string' || !headerName.test(header) || !responsePath.test(path),
    )
  ) {
    throw new Error('响应字段→请求头映射格式不合法')
  }
  return Object.fromEntries(entries) as Record<string, string>
}

async function verifyAuthBeforeSave(): Promise<void> {
  if (!authValid.value || !authEnabled.value) return
  verifyingAuth.value = true
  authVerificationMessage.value = ''
  try {
    const result = await verifyAuthentication(
      {
        login_url: auth.loginUrl.trim(),
        username: auth.username.trim(),
        password: auth.password,
        insecure_tls: auth.insecureTls,
        username_field: auth.usernameField.trim() || 'username',
        password_field: auth.passwordField.trim() || 'password',
        password_transform: auth.passwordTransform,
        token_path: auth.tokenPath.trim() || 'auto',
        authorization_scheme: auth.authorizationScheme,
        response_header_paths: parseResponseHeaderPaths(),
      },
      editingJobId.value || undefined,
    )
    authSessionId.value = result.session_id
    const label =
      result.credential_type === 'token'
        ? 'Token'
        : result.credential_type === 'cookie'
          ? 'Cookie'
          : '请求头'
    authVerificationMessage.value = `鉴权成功，已建立本次任务的${label}会话`
  } catch (err) {
    authSessionId.value = ''
    authVerificationMessage.value = err instanceof Error ? `鉴权失败：${err.message}` : '鉴权失败'
  } finally {
    verifyingAuth.value = false
  }
}

async function verifyLlmBeforeSave(): Promise<void> {
  if (verifyingLlm.value || (llmEnabled.value && !llmValid.value)) return
  verifyingLlm.value = true
  llmVerificationMessage.value = ''
  llmVerificationOk.value = false
  try {
    const result = await verifyLlmConnection(
      llmEnabled.value
        ? {
            api_key: llm.apiKey.trim(),
            model: llm.model,
            base_url: llm.baseUrl.trim(),
            protocol: llm.protocol,
            max_tokens: llm.maxTokens,
          }
        : undefined,
      // 编辑已有任务时带 jobId：key 留空则由服务端复用 SecretStore 已存密钥，
      // 不强迫用户为了点一次「测试」重新粘贴。
      editingJobId.value || undefined,
    )
    llmVerificationOk.value = true
    llmVerificationMessage.value = `中转站可用 · ${result.protocol} · ${result.model} · ${result.duration_ms}ms${result.tokens == null ? '' : ` · ${result.tokens} tokens`}`
  } catch (err) {
    llmVerificationMessage.value =
      err instanceof Error ? `中转站不可用：${err.message}` : '中转站不可用'
  } finally {
    verifyingLlm.value = false
  }
}

const reposValid = computed(
  () =>
    repos.length >= 1 &&
    repos.every((row) => {
      const address = row.address.trim()
      const isGit = classifyRepoSource(address) === 'git'
      return (
        row.name.trim() !== '' &&
        address !== '' &&
        (!row.serviceBaseUrl.trim() || /^https?:\/\//.test(row.serviceBaseUrl.trim())) &&
        (isGit
          ? /^(https?|ssh|git|file):\/\//.test(address) || address.startsWith('git@')
          : isAbsoluteRepositoryPath(address))
      )
    }),
)

const documentsValid = computed(
  () =>
    documentRows.some((row) => row.target !== null) ||
    (editingJobId.value !== '' && existingDocs.value.length >= 1),
)

const restValid = computed(() => form.restUrl.trim().startsWith('http'))

const responseHeaderPathsValid = computed(() => {
  try {
    parseResponseHeaderPaths()
    return true
  } catch {
    return false
  }
})

const authContractValid = computed(() => {
  const fieldName = /^[A-Za-z_][A-Za-z0-9_.-]{0,63}$/
  const tokenPath = /^(auto|[A-Za-z_][A-Za-z0-9_.-]{0,127})$/
  return (
    fieldName.test(auth.usernameField.trim()) &&
    fieldName.test(auth.passwordField.trim()) &&
    auth.usernameField.trim() !== auth.passwordField.trim() &&
    tokenPath.test(auth.tokenPath.trim() || 'auto') &&
    responseHeaderPathsValid.value
  )
})

// C-15：开启鉴权时三项必填 + login_url 须为 http(s) + 登录契约字段合法。
const authValid = computed(
  () =>
    !authEnabled.value ||
    (auth.loginUrl.trim().startsWith('http') &&
      auth.username.trim() !== '' &&
      (auth.password !== '' || (editingJobId.value !== '' && existingAuthPassword.value)) &&
      authContractValid.value),
)

const llmValid = computed(
  () =>
    !llmEnabled.value ||
    (llm.model.trim() !== '' &&
      llm.baseUrl.trim().startsWith('http') &&
      llm.maxTokens >= 1024 &&
      llm.maxTokens <= 16384),
)

const e2eValid = computed(
  () =>
    !e2eEnabled.value ||
    (e2e.baseUrl.trim().startsWith('http') && (!e2e.executeCases || e2e.generateCases)),
)

const fixturesValid = computed(() => {
  const raw = fixturesJson.value.trim()
  if (!raw) return true
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return false
    return parsed.every((item) => {
      if (!item || typeof item !== 'object') return false
      const record = item as Record<string, unknown>
      const methodValid = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].includes(String(record.method))
      const objectFieldValid = (key: string) => {
        const value = record[key]
        return (
          value === undefined ||
          (typeof value === 'object' && value !== null && !Array.isArray(value))
        )
      }
      return (
        typeof record.name === 'string' &&
        record.name.trim() !== '' &&
        (record.phase === 'setup' || record.phase === 'teardown') &&
        methodValid &&
        typeof record.path === 'string' &&
        record.path.startsWith('/') &&
        typeof record.expected === 'string' &&
        record.expected.trim() !== '' &&
        (record.required === undefined || typeof record.required === 'boolean') &&
        objectFieldValid('query') &&
        objectFieldValid('body')
      )
    })
  } catch {
    return false
  }
})

function formatFixturesJson(): void {
  const raw = fixturesJson.value.trim()
  if (!raw) return
  try {
    fixturesJson.value = JSON.stringify(JSON.parse(raw), null, 2)
    submitError.value = ''
  } catch {
    submitError.value = '夹具 JSON 格式不合法，无法格式化'
  }
}

function clearFixtures(): void {
  fixturesJson.value = ''
  submitError.value = ''
}

// 变更范围必填（C-09）：接口候选 = 本次代码变更涉及的接口。没有变更范围就只能
// 全量还原，那不是本次任务的范围——服务端也会 400 拒绝，这里提前挡住并说明原因。
const changeScopeValid = computed(() =>
  repos.every((row) =>
    row.diffMode === 'commit' ? row.commit.trim() !== '' : row.baselineBranch.trim() !== '',
  ),
)

const formValid = computed(
  () =>
    documentsValid.value &&
    reposValid.value &&
    changeScopeValid.value &&
    restValid.value &&
    authValid.value &&
    llmValid.value &&
    e2eValid.value &&
    fixturesValid.value,
)

const formInvalidReasons = computed(() => {
  const reasons: string[] = []
  if (!documentsValid.value) {
    reasons.push('每行至少选择一份被测版本需求文档')
  }
  if (!reposValid.value) reasons.push('每个仓库需填写名称和仓库地址')
  if (!changeScopeValid.value)
    reasons.push('每个仓库必须配置变更范围（基线分支或提交号），否则无法圈定本次改动的接口')
  if (!restValid.value) reasons.push('填写合法的 REST 服务地址')
  if (!authValid.value) reasons.push('补全鉴权地址、用户名和密码')
  if (!llmValid.value) {
    reasons.push('填写合法的 LLM 中转站 API Key、模型别名和 Base URL，或关闭本次模型配置')
  }
  if (!e2eValid.value) reasons.push('E2E 地址须为 http(s)，执行 E2E 前需开启用例生成')
  if (!fixturesValid.value)
    reasons.push('前置夹具必须是 JSON 数组，且每项包含 name、phase、method、path、expected')
  return reasons
})

function pickDocumentVersion(event: Event, index: number, role: 'target' | 'baseline'): void {
  const file = (event.target as HTMLInputElement).files?.[0]
  docError.value = ''
  const input = event.target as HTMLInputElement
  if (!file) return
  const suffix = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
  if (!['.md', '.docx', '.xlsx'].includes(suffix)) {
    docError.value = `文档格式不支持（仅 md/docx/xlsx）：${file.name}`
  } else {
    documentRows[index][role] = file
  }
  input.value = '' // 允许重选同一文件
}

async function loadConfiguration(jobId: string): Promise<void> {
  if (!jobId || jobId === editingJobId.value || loadingConfiguration.value) return
  loadingConfiguration.value = true
  submitError.value = ''
  try {
    const config = await getJobConfiguration(jobId)
    editingJobId.value = config.job_id
    form.jobId = config.job_id
    form.restUrl = config.rest_url
    restInsecureTls.value = config.rest_insecure_tls
    form.resume = config.resume
    existingDocs.value = config.documents
    documentRows.splice(
      0,
      documentRows.length,
      ...(config.document_pairs.length
        ? config.document_pairs.map(() => ({ target: null, baseline: null }))
        : [{ target: null, baseline: null }]),
    )
    repos.splice(
      0,
      repos.length,
      ...config.repos.map((repo) => ({
        name: repo.name,
        address: repo.source === 'git' ? repo.remote_url || '' : repo.path,
        serviceBaseUrl: repo.service_base_url || '',
        branch: repo.branch || 'main',
        commit: (repo.commits ?? []).join(', ') || repo.commit || '',
        baselineBranch: repo.baseline_branch || '',
        diffMode: ((repo.commits?.length ?? 0) > 0 ? 'commit' : 'branch') as 'branch' | 'commit',
        accessToken: '',
        accessTokenConfigured: repo.access_token_configured ?? false,
        language: repo.language || 'java',
        isFallback: repo.is_fallback,
        diffPreview: null,
      })),
    )
    authEnabled.value = config.auth !== null
    existingAuthPassword.value = config.auth?.password_configured ?? false
    Object.assign(auth, {
      loginUrl: config.auth?.login_url ?? '',
      username: config.auth?.username ?? '',
      password: '',
      insecureTls: config.auth?.insecure_tls ?? true,
      usernameField: config.auth?.username_field ?? 'username',
      passwordField: config.auth?.password_field ?? 'password',
      passwordTransform: config.auth?.password_transform ?? 'plain',
      tokenPath: config.auth?.token_path ?? 'auto',
      authorizationScheme: config.auth?.authorization_scheme ?? 'Token',
      responseHeaderPathsJson:
        config.auth?.response_header_paths && Object.keys(config.auth.response_header_paths).length
          ? JSON.stringify(config.auth.response_header_paths, null, 2)
          : '',
    })
    llmEnabled.value = config.llm !== null
    existingLlmKey.value = config.llm?.api_key_configured ?? false
    if (config.llm)
      Object.assign(llm, {
        model: config.llm.model,
        baseUrl: config.llm.base_url,
        protocol: config.llm.protocol,
        maxTokens: config.llm.max_tokens,
      })
    e2eEnabled.value = config.e2e !== null
    Object.assign(e2e, {
      baseUrl: config.e2e?.base_url ?? e2e.baseUrl,
      generateCases: config.e2e?.generate_cases ?? false,
      executeCases: config.e2e?.execute_cases ?? false,
      insecureTls: config.e2e?.insecure_tls ?? true,
      authMode: config.e2e?.auth_mode ?? 'browser',
    })
    fixturesJson.value = config.fixtures?.length ? JSON.stringify(config.fixtures, null, 2) : ''
  } catch (err) {
    submitError.value =
      err instanceof Error ? `加载任务配置失败：${err.message}` : '加载任务配置失败'
  } finally {
    loadingConfiguration.value = false
  }
}

watch(
  () => props.initialJobId,
  (jobId) => void loadConfiguration(jobId ?? ''),
  { immediate: true },
)

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

async function buildPayload(): Promise<{ payload: JobSubmitPayload; documentCount: number }> {
  let fixtures: FixtureStep[] = []
  const rawFixtures = fixturesJson.value.trim()
  if (rawFixtures) {
    try {
      const parsed: unknown = JSON.parse(rawFixtures)
      if (!Array.isArray(parsed)) throw new Error('前置夹具必须是 JSON 数组')
      fixtures = parsed as FixtureStep[]
    } catch {
      throw new Error('前置夹具 JSON 格式不合法')
    }
  }
  const documentPairs = await Promise.all(
    documentRows
      .filter((row) => row.target !== null)
      .map(async (row) => ({
        target: {
          doc_name: row.target!.name,
          doc_content_b64: await readFileAsBase64(row.target!),
        },
        ...(row.baseline
          ? {
              baseline: {
                doc_name: row.baseline.name,
                doc_content_b64: await readFileAsBase64(row.baseline),
              },
            }
          : {}),
      })),
  )
  const payload: JobSubmitPayload = {
    docs: [],
    ...(documentPairs.length ? { document_pairs: documentPairs } : {}),
    repos: repos.map((row) => {
      const address = row.address.trim()
      const isGit = classifyRepoSource(address) === 'git'
      // C-09 扩展：提交号支持多个（逗号/换行/空格分隔），按提交 diff 并集圈定变更范围
      const commits = row.commit.split(/[,，\s]+/).filter(Boolean)
      return {
        name: row.name.trim(),
        source: isGit ? 'git' : 'local',
        path: isGit ? '' : address,
        ...(isGit ? { remote_url: address } : {}),
        ...(row.serviceBaseUrl.trim() ? { service_base_url: row.serviceBaseUrl.trim() } : {}),
        branch: row.branch.trim() || 'main',
        // 变更范围来源由 diffMode 决定：提交号模式提交 commits，分支模式提交 baseline_branch
        ...(row.diffMode === 'commit' && commits.length ? { commits } : {}),
        ...(row.diffMode === 'branch' && row.baselineBranch.trim()
          ? { baseline_branch: row.baselineBranch.trim() }
          : {}),
        is_fallback: row.isFallback,
        // C-13：仓库语言（java → java-parser；python → 内置 AST 扫描 Django/FastAPI/Flask）
        language: row.language,
        ...(row.accessToken.trim() ? { access_token: row.accessToken.trim() } : {}),
      }
    }),
    rest_url: form.restUrl.trim(),
    rest_insecure_tls: restInsecureTls.value,
    resume: form.resume,
    // C-15：鉴权配置（可选；开启才随提交发送，凭据只落本机 job_input.json）
    ...(authEnabled.value
      ? {
          auth: {
            login_url: auth.loginUrl.trim(),
            username: auth.username.trim(),
            password: auth.password,
            insecure_tls: auth.insecureTls,
            username_field: auth.usernameField.trim() || 'username',
            password_field: auth.passwordField.trim() || 'password',
            password_transform: auth.passwordTransform,
            token_path: auth.tokenPath.trim() || 'auto',
            authorization_scheme: auth.authorizationScheme,
            response_header_paths: parseResponseHeaderPaths(),
          },
          ...(authSessionId.value ? { auth_session_id: authSessionId.value } : {}),
        }
      : {}),
    ...(llmEnabled.value
      ? {
          llm: {
            api_key: llm.apiKey.trim(),
            model: llm.model,
            base_url: llm.baseUrl.trim(),
            protocol: llm.protocol,
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
            auth_mode: e2e.authMode,
          },
        }
      : {}),
    ...(rawFixtures ? { fixtures } : {}),
  }
  return { payload, documentCount: documentPairs.length }
}

async function handlePreflight(): Promise<void> {
  if (!formValid.value || submitting.value) return
  submitting.value = true
  submitError.value = ''
  preflightResult.value = null
  try {
    const { payload } = await buildPayload()
    preflightResult.value = await preflightJob(payload)
  } catch (err) {
    submitError.value = err instanceof Error ? `配置预检失败：${err.message}` : '配置预检失败'
  } finally {
    submitting.value = false
  }
}

async function handleSubmit(runAfterSave = true): Promise<void> {
  if (!formValid.value || submitting.value) return
  submitting.value = true
  submitError.value = ''
  submittedJobId.value = ''
  savedOnly.value = Boolean(editingJobId.value && !runAfterSave)
  try {
    if (llmEnabled.value) saveLlmConfig()
    const { payload: commonPayload, documentCount } = await buildPayload()
    const trimmedJobId = form.jobId.trim()
    if (trimmedJobId && !trimmedJobId.startsWith('job-')) {
      submitError.value = '任务 ID 须以 job- 开头（横切 ID 约定）；留空可自动生成'
      return
    }
    let result: { job_id: string }
    if (editingJobId.value) {
      const payload: JobConfigurationUpdatePayload = {
        ...commonPayload,
        llm_enabled: llmEnabled.value,
        rest_insecure_tls: restInsecureTls.value,
        resume: form.resume,
        keep_existing_docs: documentCount === 0,
        run_after_save: runAfterSave,
        // 编辑页拥有夹具的完整控制权：空白也要明确清空旧夹具。
        replace_fixtures: true,
      }
      result = await updateJobConfiguration(editingJobId.value, payload)
    } else {
      const payload: JobSubmitPayload = { ...commonPayload }
      if (trimmedJobId) payload.job_id = trimmedJobId
      result = await submitJob(payload)
    }
    submittedJobId.value = result.job_id
    if (runAfterSave || !editingJobId.value) navigateTo('overview', result.job_id)
  } catch (err) {
    submitError.value = err instanceof Error ? err.message : '提交失败'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="config">
    <nav class="config-steps" aria-label="任务配置步骤">
      <button
        v-for="(step, index) in configSteps"
        :key="step.id"
        type="button"
        class="config-step"
        :class="{ active: activeStep === step.id }"
        @click="goToStep(step.id)"
      >
        <span class="step-index">{{ index + 1 }}</span>
        <span class="step-copy"
          ><strong>{{ step.label }}</strong
          ><small>{{ step.hint }}</small></span
        >
      </button>
    </nav>
    <form class="card" @submit.prevent="handleSubmit()">
      <div v-if="editingJobId" class="edit-banner" role="status">
        <div>
          <strong>正在编辑完整任务配置</strong>
          <span class="mono">{{ editingJobId }}</span>
        </div>
        <p>保存后会归档旧结果，并从「需求解析」开始重新运行整个流程。</p>
      </div>
      <!-- 需求文档（三格式 F-REQ-01~03；C-10 可多份） -->
      <section id="config-step-documents" class="field-group">
        <header class="group-title">
          需求文档版本
          <span class="group-note">每行一组被测/基线文档；仅上传被测版本时按原需求文档处理</span>
        </header>
        <div v-for="(row, index) in documentRows" :key="index" class="doc-version-row">
          <label class="doc-upload">
            <span>被测版本（必填）</span>
            <strong class="mono">{{ row.target?.name ?? '选择文件…' }}</strong>
            <input
              class="doc-file-input"
              type="file"
              accept=".md,.docx,.xlsx"
              @change="pickDocumentVersion($event, index, 'target')"
            />
          </label>
          <label class="doc-upload baseline-upload">
            <span>基线版本（可选）</span>
            <strong class="mono">{{ row.baseline?.name ?? '不选择则作为新需求处理' }}</strong>
            <input
              class="doc-file-input"
              type="file"
              accept=".md,.docx,.xlsx"
              @change="pickDocumentVersion($event, index, 'baseline')"
            />
          </label>
          <button
            class="icon-btn"
            type="button"
            :disabled="documentRows.length <= 1"
            @click="removeDocumentRow(index)"
          >
            移除
          </button>
        </div>
        <button class="ghost-btn" type="button" @click="addDocumentRow">+ 添加一行版本文档</button>
        <p v-if="editingJobId && existingDocs.length" class="preserved-docs">
          当前保留：<span v-for="name in existingDocs" :key="name" class="mono">{{ name }}</span>
        </p>
        <p v-if="docError" class="field-error" role="alert">{{ docError }}</p>
        <p v-if="!documentsValid" class="field-error" role="status">
          请至少选择一份被测版本需求文档。
        </p>
        <p class="switch-note">
          同时上传基线与被测文档时，系统先生成“需求版本差异分析”，后续评审、Code Review
          和用例生成只处理新增/修改范围；删除项进入影响核查与回归风险。
        </p>
      </section>

      <section id="config-step-environment" class="field-group">
        <header class="group-title">被测系统访问与登录</header>
        <div class="access-panel">
          <h3 class="subsection-title">接口测试地址</h3>
          <label class="field">
            <span class="field-label">被测服务地址（REST）</span>
            <input
              v-model="form.restUrl"
              type="text"
              class="mono"
              placeholder="http://127.0.0.1:9000"
            />
          </label>
          <p class="field-help">
            接口用例会将接口路径拼到此地址后发送请求。例如基础地址为
            <code>https://test.example.com</code>、接口路径为
            <code>/api/orders/</code>，完整请求地址就是
            <code>https://test.example.com/api/orders/</code
            >。这不是浏览器页面地址，也不是登录接口地址。
          </p>
          <p v-if="!restValid" class="field-error" role="status">
            请输入以 http:// 或 https:// 开头的被测服务地址。
          </p>
          <label class="switch-row">
            <input v-model="restInsecureTls" type="checkbox" />
            <span>跳过接口 HTTPS 证书校验（测试环境自签名证书）</span>
          </label>
          <p class="switch-note">
            此设置用于接口连通性探测、接口用例执行和缺陷复测；登录验证与 E2E
            浏览器各自使用独立的证书设置。
          </p>
        </div>

        <div class="access-panel">
          <h3 class="subsection-title">访问与登录</h3>
          <label class="switch-row">
            <input v-model="authEnabled" type="checkbox" />
            <span>被测服务需要登录</span>
          </label>
          <template v-if="authEnabled">
            <p class="field-help">
              配置一次账号。接口测试通过登录接口取得 Token 或 Cookie；E2E
              可复用同源会话，也可让浏览器按用例步骤登录。
            </p>
            <label class="field">
              <span class="field-label">登录接口地址（POST）</span>
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
                  :placeholder="
                    editingJobId && existingAuthPassword ? '留空则保留原密码' : '••••••••'
                  "
                />
              </label>
            </div>
            <details class="auth-contract">
              <summary>登录接口兼容设置</summary>
              <p class="field-help">
                默认请求体为 <code>{ username, password }</code>，并自动识别常见 Token 位置。
                若从浏览器网络请求看到字段或返回结构不同，请在这里调整。
              </p>
              <div class="two-col">
                <label class="field">
                  <span class="field-label">用户名字段</span>
                  <input v-model="auth.usernameField" class="mono" placeholder="username" />
                </label>
                <label class="field">
                  <span class="field-label">密码字段</span>
                  <input v-model="auth.passwordField" class="mono" placeholder="password" />
                </label>
                <label class="field">
                  <span class="field-label">密码提交方式</span>
                  <select v-model="auth.passwordTransform">
                    <option value="plain">原样提交（默认）</option>
                    <option value="base64_shift_2">
                      Base64 编码后字符偏移 2（兼容历史登录页）
                    </option>
                  </select>
                </label>
                <label class="field">
                  <span class="field-label">Token 路径</span>
                  <input
                    v-model="auth.tokenPath"
                    class="mono"
                    placeholder="auto 或 data.accessToken"
                  />
                </label>
                <label class="field">
                  <span class="field-label">Token 注入格式</span>
                  <select v-model="auth.authorizationScheme">
                    <option value="Token">Authorization: Token &lt;token&gt;</option>
                    <option value="Bearer">Authorization: Bearer &lt;token&gt;</option>
                    <option value="raw">Authorization: &lt;token&gt;</option>
                  </select>
                </label>
              </div>
              <label class="field auth-header-mappings">
                <span class="field-label">响应字段 → 后续请求头映射（可选）</span>
                <textarea
                  v-model="auth.responseHeaderPathsJson"
                  class="mono"
                  rows="3"
                  :placeholder="responseHeaderPathsPlaceholder"
                  spellcheck="false"
                />
              </label>
              <p class="switch-note">
                当前接口如请求体为 <code>{ userName, password }</code>，请将“用户名字段”改为
                <code>userName</code>。
                若登录页抓包中的密码与输入值不同，请选择对应的密码提交方式；若仍无法自动识别，请根据登录响应填写如
                <code>body.token</code> 的路径。 若后续接口不使用标准
                Authorization，可填写响应字段到请求头的映射。
              </p>
            </details>
            <label class="switch-row">
              <input v-model="auth.insecureTls" type="checkbox" />
              <span>跳过登录接口 HTTPS 证书校验（测试环境自签名证书）</span>
            </label>
            <div class="inline-action-row">
              <button
                class="ghost-btn"
                type="button"
                :disabled="!authValid || verifyingAuth"
                @click="verifyAuthBeforeSave"
              >
                {{ verifyingAuth ? '正在验证登录…' : '测试登录并保存会话' }}
              </button>
              <span
                v-if="authVerificationMessage"
                :class="
                  authVerificationMessage.startsWith('鉴权成功') ? 'inline-success' : 'field-error'
                "
              >
                {{ authVerificationMessage }}
              </span>
            </div>
            <p v-if="!authValid" class="field-error">
              开启登录后，登录接口地址须为
              http(s)，用户名和密码必填；字段名只能包含字母、数字、下划线、点或短横线，且用户名字段与密码字段不能相同。响应字段映射须为合法
              JSON 对象。
            </p>
            <p v-if="editingJobId && !auth.password && existingAuthPassword" class="switch-note">
              密码不会回传；留空会保留当前任务中已加密保存的密码。
            </p>
            <p class="switch-note">
              登录会话由服务端暂存并用于后续执行；密码不会显示或回传到浏览器。接口测试使用登录接口返回的会话；E2E
              是否复用该会话由下方登录方式决定。
            </p>
          </template>
          <p v-else class="field-help">关闭后，接口用例和 E2E 用例都按未登录状态访问。</p>
        </div>

        <div class="access-panel e2e-panel">
          <div class="subsection-heading">
            <div>
              <h3 class="subsection-title">前端 E2E 功能测试</h3>
              <p class="field-help">E2E 使用浏览器页面地址，与 REST 接口基础地址分开配置。</p>
            </div>
            <label class="switch-row">
              <input v-model="e2eEnabled" type="checkbox" />
              <span>启用（可选）</span>
            </label>
          </div>
          <template v-if="e2eEnabled">
            <label class="field">
              <span class="field-label">前端站点基础地址</span>
              <input
                v-model="e2e.baseUrl"
                type="url"
                class="mono"
                placeholder="https://10.50.16.19:8443"
              />
            </label>
            <p class="field-help">
              填写站点根地址，不要加 <code>/login/</code> 等页面路径；浏览器会按探测到的页面路由（如
              <code>/login</code>）访问。
            </p>
            <div class="e2e-options">
              <label class="switch-row">
                <input v-model="e2e.generateCases" type="checkbox" />
                <span>生成 E2E 测试用例</span>
              </label>
              <label class="switch-row">
                <input v-model="e2e.executeCases" type="checkbox" :disabled="!e2e.generateCases" />
                <span>执行 E2E 用例（Playwright）</span>
              </label>
              <label class="switch-row">
                <input v-model="e2e.insecureTls" type="checkbox" />
                <span>跳过浏览器 HTTPS 证书校验（测试环境自签名证书）</span>
              </label>
            </div>
            <fieldset v-if="authEnabled" class="e2e-auth-mode">
              <legend class="field-label">E2E 登录方式</legend>
              <label class="switch-row">
                <input v-model="e2e.authMode" type="radio" value="reuse" />
                <span>
                  复用已验证的 Token / Cookie（推荐）
                  <small>认证信息只附加到与 E2E 页面同源的请求。</small>
                </span>
              </label>
              <label class="switch-row">
                <input v-model="e2e.authMode" type="radio" value="browser" />
                <span>
                  浏览器执行登录步骤
                  <small>适合页面域名和接口域名不同，或要验证登录流程本身的场景。</small>
                </span>
              </label>
            </fieldset>
            <p v-if="authEnabled" class="switch-note">
              {{
                e2e.authMode === 'reuse'
                  ? 'E2E 会复用刚才验证的会话；浏览器测试不会重复执行登录表单步骤。'
                  : 'E2E 会在浏览器内按用例步骤登录；接口会话不会传入浏览器。'
              }}
            </p>
            <p v-else class="switch-note">
              E2E 用例会按页面当前状态执行；需要登录的场景请同时启用上方访问与登录配置。
            </p>
            <p v-if="!e2eValid" class="field-error" role="status">
              E2E 页面地址须为 http(s)；若要执行 E2E，请先开启用例生成。
            </p>
          </template>
        </div>
      </section>

      <!-- 被测仓库（本地目录或 Git 地址；填基线分支后 git diff 拿差异代码） -->
      <section class="field-group">
        <header class="group-title">
          被测仓库
          <span class="group-note">填本地目录或 Git 地址，按分支对比或提交号圈定差异代码</span>
        </header>
        <p class="switch-note repo-location-note">
          填部署服务器已挂载的本地绝对路径，或内部 Git
          地址；通过「变更范围来源」选择分支对比或提交号， 系统会 git diff
          拿差异代码，只分析改动部分。当前服务允许的目录根为 <code>{{ repositoryRoot }}</code
          >。
        </p>

        <div v-for="(row, index) in repos" :key="index" class="repo-card">
          <div class="repo-card-head">
            <input
              v-model="row.name"
              type="text"
              class="mono repo-name"
              placeholder="仓库名，如 user-service"
              :aria-label="`仓库 ${index + 1} 名称`"
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

          <label class="field">
            <span class="field-label">仓库地址（本地绝对路径 或 Git 地址）</span>
            <input
              v-model="row.address"
              type="text"
              class="mono"
              placeholder="git@git.htffund.com/team/service.git  或  /opt/repos/service"
              :aria-label="`仓库 ${index + 1} 地址`"
            />
          </label>

          <div class="diff-branch-panel">
            <div class="diff-branch-title">变更范围</div>
            <p class="diff-branch-hint">
              选择用分支对比还是提交号圈定 Code Review 的差异代码；留空则扫描全仓
            </p>
            <label class="field">
              <span class="field-label">变更范围来源</span>
              <select
                v-model="row.diffMode"
                class="mono"
                :aria-label="`仓库 ${index + 1} 变更范围来源`"
              >
                <option value="branch">分支对比</option>
                <option value="commit">提交号</option>
              </select>
            </label>
            <div v-if="row.diffMode === 'branch'" class="two-col">
              <label class="field">
                <span class="field-label">当前分支（被测）</span>
                <input
                  v-model="row.branch"
                  type="text"
                  class="mono"
                  placeholder="feature/xxx"
                  :aria-label="`仓库 ${index + 1} 当前分支`"
                />
              </label>
              <label class="field">
                <span class="field-label">基线分支（对比基准）</span>
                <input
                  v-model="row.baselineBranch"
                  type="text"
                  class="mono"
                  placeholder="main / master"
                  :aria-label="`仓库 ${index + 1} 基线分支`"
                />
              </label>
            </div>
            <label v-else class="field">
              <span class="field-label">提交号（多个用逗号分隔）</span>
              <input
                v-model="row.commit"
                type="text"
                class="mono"
                placeholder="如 a1b2c3,d4e5f6"
                :aria-label="`仓库 ${index + 1} 提交号（多个用逗号分隔）`"
              />
            </label>
            <button
              type="button"
              class="ghost-btn diff-preview-btn"
              :disabled="row.diffPreview?.loading"
              @click="previewDiff(index)"
            >
              {{ row.diffPreview?.loading ? '对比中…' : '预览差异' }}
            </button>
            <p v-if="row.diffPreview?.error" class="diff-preview-error">
              {{ row.diffPreview.error }}
            </p>
            <div v-else-if="row.diffPreview?.result" class="diff-preview-result">
              <div class="diff-preview-summary">
                <template v-if="row.diffPreview.result.commits?.length">
                  提交 {{ row.diffPreview.result.commits.join(', ') }}：新增
                  {{ row.diffPreview.result.added.length }} · 修改
                  {{ row.diffPreview.result.modified.length }} · 删除
                  {{ row.diffPreview.result.removed.length }}
                </template>
                <template v-else>
                  对比 {{ row.diffPreview.result.baseline }}…{{
                    row.diffPreview.result.target
                  }}：新增 {{ row.diffPreview.result.added.length }} · 修改
                  {{ row.diffPreview.result.modified.length }} · 删除
                  {{ row.diffPreview.result.removed.length }}
                </template>
              </div>
              <ul v-if="row.diffPreview.result.added.length" class="diff-file-list">
                <li
                  v-for="f in row.diffPreview.result.added.slice(0, 50)"
                  :key="f"
                  class="diff-file added"
                >
                  {{ f }}
                </li>
              </ul>
              <ul v-if="row.diffPreview.result.modified.length" class="diff-file-list">
                <li
                  v-for="f in row.diffPreview.result.modified.slice(0, 50)"
                  :key="f"
                  class="diff-file modified"
                >
                  {{ f }}
                </li>
              </ul>
              <ul v-if="row.diffPreview.result.removed.length" class="diff-file-list">
                <li
                  v-for="f in row.diffPreview.result.removed.slice(0, 50)"
                  :key="f"
                  class="diff-file removed"
                >
                  {{ f }}
                </li>
              </ul>
            </div>
          </div>

          <details class="repo-advanced">
            <summary>高级设置（语言 · 访问令牌 · 兜底 · 接口服务地址）</summary>
            <div class="repo-advanced-grid">
              <label class="field">
                <span class="field-label">语言</span>
                <select v-model="row.language" class="mono" :aria-label="`仓库 ${index + 1} 语言`">
                  <option value="java">Java</option>
                  <option value="python">Python</option>
                </select>
              </label>
              <label class="field">
                <span class="field-label">访问令牌（可选）</span>
                <input
                  v-model="row.accessToken"
                  type="password"
                  class="mono"
                  :placeholder="row.accessTokenConfigured ? '已配置，留空复用' : '访问令牌（可选）'"
                  :aria-label="`仓库 ${index + 1} 访问令牌`"
                />
              </label>
              <label class="field">
                <span class="field-label">接口服务地址（可选）</span>
                <input
                  v-model="row.serviceBaseUrl"
                  type="url"
                  class="mono"
                  placeholder="http://127.0.0.1:9000（留空使用任务级 REST 地址）"
                  :aria-label="`仓库 ${index + 1} 接口服务地址（可选）`"
                />
              </label>
              <label class="switch-row repo-fallback">
                <input v-model="row.isFallback" type="checkbox" />
                <span>兜底仓库</span>
              </label>
            </div>
          </details>
        </div>

        <button class="ghost-btn" type="button" @click="addRepo">+ 添加仓库</button>
        <p v-if="!reposValid" class="field-error">
          每个仓库需有名称和地址；地址为本地绝对路径或合法 Git 地址；接口服务地址如填写须为
          http(s)；仓库数 ≥ 1。
        </p>
        <p class="switch-note">
          通过「变更范围来源」下拉框选择：分支对比（按基线分支 diff
          圈定差异代码）或提交号（按多个提交的 diff
          并集圈定，一个分支多个功能改造时更精确）；留空则按全仓处理。远程仓库会按地址+版本建立缓存，
          后续重跑复用同一检出结果。接口服务地址用于多仓库/多服务任务，留空则使用任务级 REST 地址。
        </p>
      </section>

      <!-- 任务运行选项 -->
      <section class="field-group">
        <header class="group-title">任务运行选项</header>
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
          协议范围由源码扫描自动识别（R-CODE-01，非表单参数）；如需固定造数，可在下方「接口前置夹具」编辑。
        </p>
      </section>

      <section class="field-group">
        <header class="group-title">
          接口前置夹具（可选）
          <span class="group-note">setup/teardown 造数步骤；保存任务时可清空旧夹具</span>
        </header>
        <label class="field">
          <span class="field-label">夹具 JSON</span>
          <textarea
            v-model="fixturesJson"
            class="mono fixture-editor"
            rows="8"
            spellcheck="false"
            :placeholder="fixturePlaceholder"
          />
        </label>
        <div class="inline-action-row">
          <button
            class="ghost-btn"
            type="button"
            :disabled="!fixturesJson.trim()"
            @click="formatFixturesJson"
          >
            格式化 JSON
          </button>
          <button
            class="ghost-btn"
            type="button"
            :disabled="!fixturesJson.trim()"
            @click="clearFixtures"
          >
            清空夹具
          </button>
        </div>
        <p v-if="!fixturesValid" class="field-error" role="status">
          夹具必须是 JSON 数组；每项至少包含 name、phase、method、path、expected，path 需以 / 开头。
        </p>
        <p class="switch-note">
          夹具只在接口用例执行前调用，不会发送给大模型；留空表示不配置夹具。支持
          query、body、required 等字段。
        </p>
      </section>

      <section id="config-step-credentials" class="field-group">
        <header class="group-title">大模型配置</header>
        <label class="switch-row">
          <input v-model="llmEnabled" type="checkbox" />
          <span>使用本次填写的 LLM 中转站配置（关闭则复用服务级配置）</span>
        </label>
        <template v-if="llmEnabled">
          <label class="switch-row">
            <input v-model="rememberLlm" type="checkbox" @change="saveLlmConfig" />
            <span>在此浏览器记住非敏感模型配置（不保存 API Key）</span>
          </label>
          <label class="field">
            <span class="field-label">LLM 中转站 API Key</span>
            <input
              v-model="llm.apiKey"
              type="password"
              autocomplete="new-password"
              :placeholder="
                editingJobId && existingLlmKey && !llm.apiKey
                  ? '已保存密钥（留空即复用，无需重填）'
                  : 'sk-…'
              "
            />
          </label>
          <!-- 编辑态已存密钥：把「留空即复用」提到输入框紧邻处。
               此前提示只出现在表单底部，用户看到空白输入框会误以为密钥丢失、
               被迫重新粘贴（key 不回显是安全红线，不能靠回显解决）。 -->
          <p v-if="editingJobId && existingLlmKey && !llm.apiKey" class="switch-note key-reuse-hint">
            ✓ 已保存密钥（服务端加密存储，不回显）。<strong>留空即可复用</strong>，无需重新填写。
          </p>
          <p
            v-else-if="editingJobId && existingLlmKey && llm.apiKey"
            class="switch-note key-reuse-hint"
          >
            将用本次填写的新 Key 覆盖已存密钥（留空则继续沿用旧密钥）。
          </p>
          <div class="two-col">
            <label class="field">
              <span class="field-label">调用协议</span>
              <select v-model="llm.protocol">
                <option
                  v-for="option in LLM_PROTOCOL_OPTIONS"
                  :key="option.value"
                  :value="option.value"
                >
                  {{ option.label }}
                </option>
              </select>
            </label>
            <label class="field">
              <span class="field-label">模型别名</span>
              <select v-model="llm.model">
                <option v-if="!hasKnownLlmModel" :value="llm.model">
                  {{ llm.model }}（已有/自定义别名）
                </option>
                <optgroup v-for="group in LLM_MODEL_GROUPS" :key="group.label" :label="group.label">
                  <option v-for="option in group.options" :key="option.value" :value="option.value">
                    {{ option.value }} · {{ option.label }}
                  </option>
                </optgroup>
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
            <span class="field-label">中转站 Base URL</span>
            <input
              v-model="llm.baseUrl"
              type="url"
              class="mono"
              placeholder="https://llm.htffund.com/v1"
            />
          </label>
          <p v-if="selectedLlmModelWarning" class="field-warning" role="status">
            {{ selectedLlmModelWarning }}
          </p>
          <p v-if="!llmValid" class="field-error">
            请填写调用协议、模型别名、Base URL 和 1024–16384 的 Token 上限；API Key
            留空时复用服务级或已有任务密钥。
          </p>
          <p v-if="editingJobId && !llm.apiKey && existingLlmKey" class="switch-note">
            未重新填写 API Key，将复用服务端安全存储中的密钥。
          </p>
          <p class="switch-note">
            当前默认复用部署服务器的 LLM 中转站配置；需要临时切换模型别名或 API Key
            时再开启本次任务配置。
          </p>
          <p class="switch-note">
            浏览器只保存协议、模型、地址和 Token 上限；API Key
            不进入浏览器持久化存储，提交后由服务端 SecretStore
            安全复用。重跑已有任务时留空即可复用服务端密钥。需求解析、接口还原、Code Review、
            用例生成会按章节或实体分批调用，再由程序校验并合并。
          </p>
        </template>
        <div class="inline-action-row">
          <button
            class="ghost-btn"
            type="button"
            :disabled="verifyingLlm || (llmEnabled && !llmValid)"
            @click="verifyLlmBeforeSave"
          >
            {{
              verifyingLlm
                ? '正在测试中转站…'
                : llmEnabled
                  ? '测试本次中转站配置'
                  : '测试服务级中转站'
            }}
          </button>
          <span
            v-if="llmVerificationMessage"
            :class="llmVerificationOk ? 'inline-success' : 'field-error'"
          >
            {{ llmVerificationMessage }}
          </span>
        </div>
        <p class="switch-note">
          测试只发送固定的无业务提示，单次最多等待 30
          秒、无自动重试；不会创建任务、保存模型正文或暴露 API Key。
        </p>
      </section>

      <!-- 提交 -->
      <div id="config-step-confirm" class="submit-row">
        <button class="ghost-btn" type="button" :disabled="submitting" @click="saveDraft">
          保存本地草稿
        </button>
        <button class="ghost-btn" type="button" :disabled="submitting" @click="restoreDraft">
          恢复草稿
        </button>
        <button
          class="ghost-btn"
          type="button"
          :disabled="!formValid || submitting"
          @click="handlePreflight"
        >
          {{ submitting && !submittedJobId ? '检查中…' : '配置预检' }}
        </button>
        <button
          v-if="editingJobId"
          class="ghost-btn"
          type="button"
          :disabled="!formValid || submitting"
          @click="handleSubmit(false)"
        >
          {{ submitting ? '正在保存…' : '仅保存配置' }}
        </button>
        <button class="primary-btn" type="submit" :disabled="!formValid || submitting">
          {{
            submitting
              ? '保存并启动中…'
              : editingJobId
                ? '保存配置并全量重新运行'
                : '启动全流程（F-ORCH-01）'
          }}
        </button>
        <span v-if="submittedJobId" class="submit-ok" role="status">
          已{{ savedOnly ? '保存配置' : editingJobId ? '保存并重新运行' : '提交' }}：<span
            class="mono"
            >{{ submittedJobId }}</span
          >
          <template v-if="!savedOnly">—— 已跳转到「流水线总览」</template>
        </span>
        <span v-else class="submit-note">与 CLI 走同一 PipelineRunner（Web 与命令行入口等价）</span>
      </div>
      <p v-if="!formValid" class="submit-blockers" role="status">
        暂不能启动：{{ formInvalidReasons.join('；') }}。
      </p>
      <p v-if="submitError" class="field-error" role="alert">{{ submitError }}</p>
      <p v-if="draftSavedAt" class="submit-note" role="status">
        草稿仅保存在当前浏览器（{{ draftSavedAt }}），不会保存密码、API Key 或 Git Token。
      </p>
      <div v-if="preflightResult" class="preflight-result" :data-ready="preflightResult.ready">
        <strong>{{
          preflightResult.ready ? '配置预检通过，可以提交' : '配置预检未通过，请先修正问题'
        }}</strong>
        <ul>
          <li v-for="check in preflightResult.checks" :key="check.name" :data-status="check.status">
            <span>{{ check.name }}</span
            ><span>{{ check.message }}</span>
          </li>
        </ul>
      </div>
    </form>
  </div>
</template>

<style scoped>
.config {
  max-width: 860px;
  margin: 0 auto;
}

.config-steps {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--space-2);
  margin-bottom: var(--space-3);
  padding: var(--space-2);
  background: color-mix(in srgb, var(--surface) 94%, transparent);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  backdrop-filter: blur(10px);
}

.config-step {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
  padding: var(--space-2) var(--space-3);
  color: var(--text-2);
  text-align: left;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
}

.config-step:hover,
.config-step.active {
  color: var(--accent-strong);
  background: var(--accent-dim);
  border-color: var(--accent-glow);
}

.step-index {
  display: grid;
  flex: 0 0 24px;
  width: 24px;
  height: 24px;
  place-items: center;
  color: var(--accent-strong);
  font-size: var(--text-xs);
  font-weight: 700;
  background: var(--surface);
  border: 1px solid var(--border-strong);
  border-radius: 50%;
}

.step-copy {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 2px;
}

.step-copy strong,
.step-copy small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.step-copy strong {
  font-size: var(--text-sm);
}

.step-copy small {
  color: var(--text-3);
  font-size: var(--text-xs);
}

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  padding: var(--space-5);
}

.edit-banner {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-3);
  margin-bottom: var(--space-5);
  padding: var(--space-3) var(--space-4);
  background: var(--accent-dim);
  border: 1px solid var(--accent-glow);
  border-radius: var(--radius-md);
}

.edit-banner div {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.edit-banner p,
.preserved-docs {
  margin: 0;
  color: var(--text-2);
  font-size: var(--text-sm);
}

.preserved-docs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-top: var(--space-2);
}

.preserved-docs .mono {
  padding: 2px 6px;
  background: var(--surface-2);
  border-radius: 4px;
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

.access-panel {
  margin: 0 0 var(--space-4);
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
}

.access-panel:last-child {
  margin-bottom: 0;
}

.subsection-title {
  margin: 0 0 var(--space-3);
  color: var(--text-1);
  font-size: var(--text-md);
  font-weight: 600;
}

.field-help {
  margin: 0 0 var(--space-3);
  color: var(--text-3);
  font-size: var(--text-sm);
  line-height: 1.6;
}

.field-help code {
  color: var(--text-2);
  overflow-wrap: anywhere;
}

.subsection-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-4);
  margin-bottom: var(--space-3);
}

.subsection-heading .subsection-title {
  margin-bottom: var(--space-1);
}

.subsection-heading .field-help {
  margin-bottom: 0;
}

.e2e-options {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3) var(--space-4);
  margin-top: var(--space-2);
}

.e2e-options .switch-row:last-child {
  grid-column: 1 / -1;
}

.e2e-auth-mode {
  display: grid;
  gap: var(--space-2);
  margin: var(--space-3) 0 0;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.e2e-auth-mode legend {
  padding: 0 var(--space-1);
}

.e2e-auth-mode .switch-row {
  align-items: flex-start;
}

.e2e-auth-mode small {
  display: block;
  margin-top: 2px;
  color: var(--text-3);
  font-size: var(--text-xs);
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

.auth-contract {
  margin: 0 0 var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-elev);
}

.auth-contract summary {
  color: var(--text-2);
  cursor: pointer;
  font-size: var(--text-sm);
  font-weight: 600;
}

.auth-contract[open] summary {
  margin-bottom: var(--space-3);
}

.auth-contract .field:last-of-type {
  margin-bottom: 0;
}

.field-label {
  font-size: var(--text-sm);
  color: var(--text-2);
}

.fixture-editor {
  width: 100%;
  min-height: 160px;
  padding: var(--space-3);
  line-height: 1.5;
  resize: vertical;
  white-space: pre;
  overflow: auto;
}

.field-error {
  margin: var(--space-1) 0 0;
  font-size: var(--text-sm);
  color: var(--warning);
}

.field-warning {
  margin: var(--space-1) 0 0;
  font-size: var(--text-sm);
  color: var(--warning);
}

.inline-action-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-top: var(--space-2);
}

.inline-success {
  color: var(--success);
  font-size: var(--text-sm);
}

/* 文档上传 */
.doc-upload {
  position: relative;
  display: flex;
  flex-direction: column;
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
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  opacity: 0;
  cursor: pointer;
}

.doc-version-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
  gap: var(--space-2);
  align-items: stretch;
  margin-bottom: var(--space-2);
}

.doc-version-row .doc-upload {
  justify-content: center;
  min-height: 72px;
}

.doc-version-row .doc-upload span {
  color: var(--text-3);
  font-size: var(--text-xs);
}

.doc-version-row .doc-upload strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-sm);
  font-weight: 500;
}

.baseline-upload {
  background: var(--surface);
}

/* 对比分支面板 */
.diff-branch-panel {
  margin: var(--space-3) 0;
  padding: var(--space-3);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.diff-branch-title {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-1);
}

.diff-branch-hint {
  margin: var(--space-1) 0 var(--space-3);
  font-size: var(--text-xs);
  color: var(--text-3);
}

.diff-preview-btn {
  margin-bottom: var(--space-2);
}

.diff-preview-error {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-xs);
  color: var(--danger);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.diff-preview-result {
  font-size: var(--text-xs);
}

.diff-preview-summary {
  margin-bottom: var(--space-2);
  color: var(--text-2);
}

.diff-file-list {
  list-style: none;
  margin: 0 0 var(--space-2);
  padding: 0;
  max-height: 180px;
  overflow-y: auto;
}

.diff-file {
  padding: 2px var(--space-2);
  font-family: var(--font-mono);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.diff-file.added {
  color: var(--success);
}

.diff-file.modified {
  color: var(--warning);
}

.diff-file.removed {
  color: var(--danger);
}

.doc-upload:hover,
.doc-upload:focus-within {
  border-color: var(--accent);
}

@media (max-width: 760px) {
  .doc-version-row {
    grid-template-columns: 1fr;
  }
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

/* 仓库卡片 */
.repo-card {
  margin-bottom: var(--space-3);
  padding: var(--space-3);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.repo-card-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}

.repo-card-head .repo-name {
  flex: 1;
  font-weight: 600;
}

.repo-card input[type='text'],
.repo-card input[type='url'],
.repo-card input[type='password'],
.repo-card select {
  width: 100%;
  padding: var(--space-2) var(--space-3);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-1);
  font-size: var(--text-sm);
}

.repo-card input[type='text']:focus,
.repo-card input[type='url']:focus,
.repo-card input[type='password']:focus,
.repo-card select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-dim);
  outline: none;
}

.repo-card input[type='checkbox'] {
  accent-color: var(--warning);
}

/* 高级设置折叠 */
.repo-advanced {
  margin-top: var(--space-1);
}

.repo-advanced summary {
  cursor: pointer;
  font-size: var(--text-sm);
  color: var(--text-2);
}

.repo-advanced[open] summary {
  margin-bottom: var(--space-2);
}

.repo-advanced-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-2) var(--space-3);
}

.repo-advanced-grid .field {
  margin-bottom: 0;
}

.repo-fallback {
  align-self: end;
}

@media (max-width: 860px) {
  .config-steps {
    grid-template-columns: repeat(2, 1fr);
  }

  .repo-advanced-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 560px) {
  .config-steps {
    grid-template-columns: 1fr;
  }

  .submit-row {
    align-items: stretch;
  }

  .submit-row button {
    width: 100%;
  }

  .repo-card-head {
    flex-direction: column;
    align-items: stretch;
  }

  .diff-branch-panel .two-col {
    grid-template-columns: 1fr;
  }

  .subsection-heading {
    flex-direction: column;
  }

  .e2e-options {
    grid-template-columns: 1fr;
  }

  .e2e-options .switch-row:last-child {
    grid-column: auto;
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

/* 已存密钥复用提示：紧贴输入框，用成功色与常规说明区分开 */
.key-reuse-hint {
  color: var(--success, var(--text-2));
  font-weight: 500;
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
  padding: var(--space-3);
  background: color-mix(in srgb, var(--surface) 96%, transparent);
  border-top: 1px solid var(--border);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: 0 -8px 24px color-mix(in srgb, var(--shadow) 12%, transparent);
  backdrop-filter: blur(10px);
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

.preflight-result {
  margin-top: var(--space-3);
  padding: var(--space-3) var(--space-4);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text-2);
  font-size: var(--text-sm);
}

.preflight-result[data-ready='true'] {
  border-color: color-mix(in srgb, var(--success) 55%, var(--border));
}

.preflight-result[data-ready='false'] {
  border-color: color-mix(in srgb, var(--warning) 55%, var(--border));
}

.preflight-result ul {
  display: grid;
  gap: var(--space-1);
  margin: var(--space-2) 0 0;
  padding: 0;
  list-style: none;
}

.preflight-result li {
  display: grid;
  grid-template-columns: 120px 1fr;
  gap: var(--space-2);
}

.preflight-result li[data-status='ok'] span:first-child {
  color: var(--success);
}

.preflight-result li[data-status='error'] span:first-child {
  color: var(--warning);
}
</style>
