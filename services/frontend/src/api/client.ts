/** orchestrator REST API 客户端（dev 经 vite 代理 → :8000；prod 同源）。 */

import type {
  ChangeScopeEntry,
  DefectEntry,
  DiffPreviewResponse,
  ExecResult,
  ExecSummary,
  InterfaceCandidate,
  InterfaceDef,
  JobListItem,
  JobStatus,
  JobConfiguration,
  JobConfigurationUpdatePayload,
  JobConfigurationUpdateResponse,
  JobSubmitPayload,
  JobSubmitResponse,
  PreflightResponse,
  LlmCallLog,
  MetricsPayload,
  MissingFeature,
  RegressionIndexPayload,
  RemovedFile,
  RepoInventory,
  RerunPayload,
  RerunResponse,
  StopJobResponse,
  TestCaseEntry,
  E2eTestCaseEntry,
  UnmatchedChangedFile,
  RequirementItem,
  RequirementReview,
  RetestResponse,
  ReviewLogResponse,
  ReviewMapping,
  ReviewCallChain,
  DerivedRisk,
  AuthConfig,
  AuthVerificationResponse,
  LlmConfig,
  LlmVerificationResponse,
} from './types'

const ACCESS_TOKEN_KEY = 'llm-autotest.access-token.session'
const PROJECT_ID_KEY = 'llm-autotest.project-id.session'

export interface AccessContext {
  token: string
  projectId: string
}

export interface AccessMode {
  mode: 'single_user' | 'token'
  user: string
}

export function getAccessContext(): AccessContext {
  try {
    return {
      token: sessionStorage.getItem(ACCESS_TOKEN_KEY) ?? '',
      projectId: sessionStorage.getItem(PROJECT_ID_KEY) ?? '',
    }
  } catch {
    return { token: '', projectId: '' }
  }
}

export function setAccessContext(context: AccessContext): void {
  const token = context.token.trim()
  const projectId = context.projectId.trim()
  if (!token || !projectId) throw new Error('访问令牌和项目标识必须同时填写')
  try {
    sessionStorage.setItem(ACCESS_TOKEN_KEY, token)
    sessionStorage.setItem(PROJECT_ID_KEY, projectId)
  } catch {
    throw new Error('当前浏览器不允许保存会话访问配置')
  }
}

export function clearAccessContext(): void {
  try {
    sessionStorage.removeItem(ACCESS_TOKEN_KEY)
    sessionStorage.removeItem(PROJECT_ID_KEY)
  } catch {
    // 私密浏览器限制不影响本地兼容模式请求。
  }
}

function accessHeaders(): Record<string, string> {
  const context = getAccessContext()
  return context.token && context.projectId
    ? { Authorization: `Bearer ${context.token}`, 'X-Project-Id': context.projectId }
    : {}
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  headers.set('Content-Type', 'application/json')
  for (const [key, value] of Object.entries(accessHeaders())) headers.set(key, value)
  const response = await fetch(path, {
    ...init,
    headers,
  })
  if (!response.ok) {
    let detail = `${response.status}`
    try {
      const body = await response.json()
      if (typeof body?.detail === 'string') detail = body.detail
    } catch {
      /* 非 JSON 错误体：退回状态码 */
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

export function listJobs(): Promise<{ jobs: JobListItem[] }> {
  return request('/api/v1/jobs')
}

/** 入口模式不要求 Token，用于决定是否展示多用户访问配置。 */
export function getAccessMode(): Promise<AccessMode> {
  return request('/api/v1/system/access-mode')
}

export function getJobStatus(jobId: string): Promise<JobStatus> {
  return request(`/api/v1/jobs/${jobId}`)
}

export function submitJob(payload: JobSubmitPayload): Promise<JobSubmitResponse> {
  return request('/api/v1/jobs', { method: 'POST', body: JSON.stringify(payload) })
}

export function preflightJob(payload: JobSubmitPayload): Promise<PreflightResponse> {
  return request('/api/v1/preflight', { method: 'POST', body: JSON.stringify(payload) })
}

/** 配置阶段真实登录；响应不含 Token，仅返回内存会话标识。 */
export function verifyAuthentication(
  auth: AuthConfig,
  jobId?: string,
): Promise<AuthVerificationResponse> {
  return request('/api/v1/auth/verify', {
    method: 'POST',
    body: JSON.stringify({ ...auth, ...(jobId ? { job_id: jobId } : {}) }),
  })
}

/** 以极小无业务提示验证中转站协议、模型和凭据；不创建任务或保存模型正文。
 *  编辑已有任务时传 jobId：api_key 留空则由服务端复用该任务已存密钥。 */
export function verifyLlmConnection(
  llm?: LlmConfig,
  jobId?: string,
): Promise<LlmVerificationResponse> {
  return request('/api/v1/llm/verify', {
    method: 'POST',
    body: JSON.stringify({ ...(llm ? { llm } : {}), ...(jobId ? { job_id: jobId } : {}) }),
  })
}

export function getRepositoryRoot(): Promise<{ root: string; exists: boolean }> {
  return request('/api/v1/system/repository-root')
}

/** 预览仓库「基线 → 当前」分支差异，不创建 job、不写产物。 */
export function previewBranchDiff(payload: {
  name: string
  address: string
  branch: string
  baseline_branch?: string
  commits?: string[]
  access_token?: string
}): Promise<DiffPreviewResponse> {
  return request('/api/v1/repos/diff-preview', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function getJobConfiguration(jobId: string): Promise<JobConfiguration> {
  return request(`/api/v1/jobs/${jobId}/configuration`)
}

export function updateJobConfiguration(
  jobId: string,
  payload: JobConfigurationUpdatePayload,
): Promise<JobConfigurationUpdateResponse> {
  return request(`/api/v1/jobs/${jobId}/configuration`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function retestFull(jobId: string): Promise<RetestResponse> {
  return request(`/api/v1/jobs/${jobId}/retest`, { method: 'POST', body: '{}' })
}

// C-14：重跑（fromNode 缺省 = 全量；目标节点及其下游级联失效后重跑）
export function rerunJob(jobId: string, payload: RerunPayload = {}): Promise<RerunResponse> {
  return request(`/api/v1/jobs/${jobId}/rerun`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/** 停止后台任务：保留已有产物，当前节点回到待运行以便稍后续跑。 */
export function stopJob(jobId: string): Promise<StopJobResponse> {
  return request(`/api/v1/jobs/${jobId}/stop`, { method: 'POST', body: '{}' })
}

export function retestSingle(jobId: string, defectId: string): Promise<RetestResponse> {
  return request(`/api/v1/jobs/${jobId}/retest`, {
    method: 'POST',
    body: JSON.stringify({ defect_id: defectId }),
  })
}

export function postReviewLog(
  jobId: string,
  decision: 'approved' | 'rejected',
  reviewer: string,
  note: string,
): Promise<ReviewLogResponse> {
  return request(`/api/v1/jobs/${jobId}/review-log`, {
    method: 'POST',
    body: JSON.stringify({ decision, reviewer, note }),
  })
}

export async function getReportMd(jobId: string): Promise<string> {
  const response = await fetch(`/api/v1/jobs/${jobId}/report`, { headers: accessHeaders() })
  if (!response.ok) throw new Error('报告尚未生成')
  return response.text()
}

/* ---- 产物读取（JSON 端点返回对象） ---- */

export function getRequirementItems(jobId: string): Promise<{
  items: RequirementItem[]
  source_files?: string[]
  source_catalog?: import('./types').RequirementSource[]
}> {
  return request(`/api/v1/jobs/${jobId}/artifacts/requirement_items.json`)
}

export function getRequirementReview(jobId: string): Promise<RequirementReview> {
  return request(`/api/v1/jobs/${jobId}/artifacts/requirement_review.json`)
}

/** 读取已脱敏的 LLM 调用留痕；不包含 API Key、输入内容或模型原文。 */
export async function getLlmCallLogs(jobId: string): Promise<LlmCallLog[]> {
  const response = await fetch(`/api/v1/jobs/${jobId}/logs`, { headers: accessHeaders() })
  if (!response.ok) return []
  const payload = (await response.json()) as { logs?: LlmCallLog[] }
  return Array.isArray(payload.logs) ? payload.logs : []
}

export async function exportJobResults(jobId: string): Promise<void> {
  const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}/export`, {
    headers: accessHeaders(),
  })
  if (!response.ok) throw new Error(`导出失败：${response.status}`)
  const blob = await response.blob()
  const objectUrl = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = objectUrl
  anchor.download = `${jobId}-results.zip`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
}

// C-10：源码扫描清单（独立展示页数据源）
export function getCodeInventory(jobId: string): Promise<{ repositories: RepoInventory[] }> {
  return request(`/api/v1/jobs/${jobId}/artifacts/code_inventory.json`)
}

// C-10：用例清单（独立展示页数据源）
export function getTestcases(jobId: string): Promise<{
  cases: TestCaseEntry[]
  e2e_cases?: E2eTestCaseEntry[]
  warnings?: string[]
}> {
  return request(`/api/v1/jobs/${jobId}/artifacts/testcases.json`)
}

export function getInterfaceCandidates(jobId: string): Promise<{
  candidates: InterfaceCandidate[]
  warnings?: string[]
  capabilities?: import('./types').InterfaceScanCapabilities
}> {
  return request(`/api/v1/jobs/${jobId}/artifacts/interface_candidates.json`)
}

export function getInterfaces(jobId: string): Promise<{ interfaces: InterfaceDef[] }> {
  return request(`/api/v1/jobs/${jobId}/artifacts/interfaces.json`)
}

export function getChangeSet(jobId: string): Promise<{ removed_files?: RemovedFile[] }> {
  return request(`/api/v1/jobs/${jobId}/artifacts/change_set.json`)
}

export function getReviewMapping(jobId: string): Promise<{
  mappings: ReviewMapping[]
  scan_coverage?: import('./types').ReviewScanCoverage
  risks: DerivedRisk[]
  missing_features: MissingFeature[]
  evidence_catalog?: import('./types').ReviewEvidence[]
  call_chains?: ReviewCallChain[]
  deterministic_checks?: import('./types').DeterministicReviewCheck[]
  change_scope?: ChangeScopeEntry[]
  change_scope_available?: boolean
  removed_files?: RemovedFile[]
  unmatched_changed_files?: UnmatchedChangedFile[]
  incremental_findings?: import('./types').IncrementalFinding[]
}> {
  return request(`/api/v1/jobs/${jobId}/artifacts/review_mapping.json`)
}

export function getExecution(jobId: string): Promise<{
  results: ExecResult[]
  summary: ExecSummary
  degraded_protocols?: Array<{ protocol: string; mode: string; reason?: string }>
}> {
  return request(`/api/v1/jobs/${jobId}/artifacts/execution.json`)
}

export async function getEvidenceBlob(jobId: string, evidencePath: string): Promise<Blob> {
  const encodedPath = evidencePath.split('/').map(encodeURIComponent).join('/')
  const response = await fetch(`/api/v1/jobs/${jobId}/artifacts/${encodedPath}`, {
    headers: accessHeaders(),
  })
  if (!response.ok) throw new Error(`执行证据加载失败：${response.status}`)
  return response.blob()
}

export function getDefects(
  jobId: string,
): Promise<{ defects: DefectEntry[]; warnings?: string[] }> {
  return request(`/api/v1/jobs/${jobId}/artifacts/defects.json`)
}

export function getRegressionIndex(jobId: string): Promise<RegressionIndexPayload> {
  return request(`/api/v1/jobs/${jobId}/artifacts/regression_index.json`)
}

export function getMetrics(jobId: string): Promise<MetricsPayload> {
  return request(`/api/v1/jobs/${jobId}/artifacts/metrics.json`)
}
