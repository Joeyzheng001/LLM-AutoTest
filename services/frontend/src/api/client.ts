/** orchestrator REST API 客户端（dev 经 vite 代理 → :8000；prod 同源）。 */

import type {
  ChangeScopeEntry,
  DefectEntry,
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
  DerivedRisk,
  AuthConfig,
  AuthVerificationResponse,
} from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
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

export function getJobStatus(jobId: string): Promise<JobStatus> {
  return request(`/api/v1/jobs/${jobId}`)
}

export function submitJob(payload: JobSubmitPayload): Promise<JobSubmitResponse> {
  return request('/api/v1/jobs', { method: 'POST', body: JSON.stringify(payload) })
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
  const response = await fetch(`/api/v1/jobs/${jobId}/report`)
  if (!response.ok) throw new Error('报告尚未生成')
  return response.text()
}

/* ---- 产物读取（JSON 端点返回对象） ---- */

export function getRequirementItems(jobId: string): Promise<{
  items: RequirementItem[]
  source_files?: string[]
}> {
  return request(`/api/v1/jobs/${jobId}/artifacts/requirement_items.json`)
}

export function getRequirementReview(jobId: string): Promise<RequirementReview> {
  return request(`/api/v1/jobs/${jobId}/artifacts/requirement_review.json`)
}

/** 读取已脱敏的 LLM 调用留痕；不包含 API Key、输入内容或模型原文。 */
export async function getLlmCallLogs(jobId: string): Promise<LlmCallLog[]> {
  const response = await fetch(`/api/v1/jobs/${jobId}/logs`)
  if (!response.ok) return []
  const payload = (await response.json()) as { logs?: LlmCallLog[] }
  return Array.isArray(payload.logs) ? payload.logs : []
}

export function exportJobResults(jobId: string): void {
  const anchor = document.createElement('a')
  anchor.href = `/api/v1/jobs/${encodeURIComponent(jobId)}/export`
  anchor.download = `${jobId}-results.zip`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
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

export function getInterfaceCandidates(
  jobId: string,
): Promise<{ candidates: InterfaceCandidate[] }> {
  return request(`/api/v1/jobs/${jobId}/artifacts/interface_candidates.json`)
}

export function getInterfaces(jobId: string): Promise<{ interfaces: InterfaceDef[] }> {
  return request(`/api/v1/jobs/${jobId}/artifacts/interfaces.json`)
}

export function getReviewMapping(jobId: string): Promise<{
  mappings: ReviewMapping[]
  risks: DerivedRisk[]
  missing_features: MissingFeature[]
  deterministic_checks?: import('./types').DeterministicReviewCheck[]
  change_scope?: ChangeScopeEntry[] // C-09：旧产物无此键 → undefined → 页面隐藏视图四
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
