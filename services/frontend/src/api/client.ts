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
  JobSubmitPayload,
  JobSubmitResponse,
  MetricsPayload,
  MissingFeature,
  RegressionIndexPayload,
  RemovedFile,
  RepoInventory,
  TestCaseEntry,
  UnmatchedChangedFile,
  RequirementItem,
  RetestResponse,
  ReviewLogResponse,
  ReviewMapping,
  DerivedRisk,
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

export function retestFull(jobId: string): Promise<RetestResponse> {
  return request(`/api/v1/jobs/${jobId}/retest`, { method: 'POST', body: '{}' })
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

// C-10：源码扫描清单（独立展示页数据源）
export function getCodeInventory(jobId: string): Promise<{ repositories: RepoInventory[] }> {
  return request(`/api/v1/jobs/${jobId}/artifacts/code_inventory.json`)
}

// C-10：用例清单（独立展示页数据源）
export function getTestcases(jobId: string): Promise<{
  cases: TestCaseEntry[]
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
  change_scope?: ChangeScopeEntry[] // C-09：旧产物无此键 → undefined → 页面隐藏视图四
  removed_files?: RemovedFile[]
  unmatched_changed_files?: UnmatchedChangedFile[]
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
