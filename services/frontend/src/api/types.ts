/** REST API 类型（与 orchestrator api.py 契约对齐；数值全部来自后端，前端不计算）。 */

export type NodeStatus = 'completed' | 'running' | 'manual_intervention' | 'pending'

export interface NodeState {
  name: string
  status: NodeStatus
  updated_at?: string
  attempts?: number
  error?: { code: string; message: string } | null
}

export interface DegradedRecord {
  node: string
  reason: string
}

export interface JobListItem {
  job_id: string
  updated_at: string | null
  running: boolean
  completed: number
  degraded: number
  has_report: boolean
}

export interface JobStatus {
  job_id: string
  running: boolean
  updated_at: string | null
  nodes: NodeState[]
  degraded: DegradedRecord[]
  artifacts: string[]
}

export interface JobSubmitResponse {
  job_id: string
  status: string
}

export interface RetestResponse {
  job_id: string
  mode: 'full' | 'single'
  defect_id?: string
  retested?: string[]
  archived?: number
  warnings?: string[]
}

export interface ReviewLogResponse {
  job_id: string
  timestamp: string
  decision: 'approved' | 'rejected'
  reviewer: string
  note: string
  rerun: string | null
}

// C-14：重跑入队响应（from_node 缺省 = req_parse 全量级联失效）
export interface RerunResponse {
  job_id: string
  status: 'rerun-queued'
  from_node: string
}

export interface RepoConfig {
  name: string
  path: string
  branch: string
  baseline_branch?: string // C-09：基线分支（非空 → 双分支 diff 变更范围视图）
  is_fallback: boolean
  include_paths?: string[]
  language?: 'java' | 'python' // C-13：仓库语言（java → java-parser；python → 内置 AST 扫描）
}

// C-10：多需求文档（docs 列表；后端兼容旧单文档字段，前端统一走新口径）
export interface DocInput {
  doc_name: string
  doc_content_b64: string
}

export interface JobSubmitPayload {
  job_id?: string
  docs: DocInput[]
  repos: RepoConfig[] // C-10：≥1 个仓库（原 ≥2 放宽）
  rest_url: string
  resume?: boolean
}

/* ---- 产物形状（只声明页面用到的字段；后端 Schema 为唯一事实源） ---- */

export interface RequirementItem {
  id: string
  title: string
  description: string
  priority?: string
  stub_note?: string
  source_files?: string[] // C-10：全部来源文档路径
}

export interface CodeLocation {
  repo: string
  file: string
  start_line?: number
  end_line?: number
}

export interface InterfaceDef {
  id: string
  candidate_id?: string
  protocol: string
  signature: { method: string; path: string; summary?: string }
  params?: Array<{ name: string; type?: string; required?: boolean; in?: string }>
  code_location?: CodeLocation
  is_fallback?: boolean
}

export interface InterfaceCandidate {
  candidate_id: string
  protocol: string
  signature: string
  code_location: CodeLocation
  is_fallback?: boolean
  confidence?: number
}

export interface ReviewMapping {
  requirement_id: string
  interface_id?: string | null
  status: 'implemented' | 'partial' | 'missing' | 'undetermined'
  confidence: number
  evidence?: { code_location?: CodeLocation; note?: string }
}

export interface DerivedRisk {
  requirement_id: string
  description: string
}

export interface MissingFeature {
  requirement_id: string
  title: string
}

// C-09 视图四：变更范围（双分支 diff 程序化派生，不经 LLM）
export interface ChangeScopeEntry {
  interface_id: string
  repo: string
  file: string
  change: 'added' | 'modified'
  covered_by: string[] // 空 = 变更接口未被任何需求覆盖（变更风险信号）
}

export interface RemovedFile {
  repo: string
  file: string
}

// 变更但未命中接口定义文件（业务逻辑层变更——行为风险信号）
export interface UnmatchedChangedFile {
  repo: string
  file: string
  change: 'added' | 'modified'
}

// C-10：源码扫描清单（code_inventory.json；仓库四态 X-CODE-01~03）
export interface RepoInventory {
  name: string
  path: string
  branch: string
  baseline_branch?: string
  changed_files?: { added: string[]; modified: string[]; removed: string[] }
  is_fallback: boolean
  status: 'ok' | 'empty' | 'truncated' | 'error'
  file_count: number
  truncated_scope?: string[]
  encoding_skipped?: string[]
  error?: { code: string; message: string; details?: Record<string, unknown> }
  files?: Array<{ repo: string; module: string; path: string }>
}

// C-10：用例清单（testcases.json；F-GEN-01 三类 / F-GEN-04 溯源）
export interface TestCaseEntry {
  id: string
  interface_id: string
  requirement_id?: string
  category: 'normal' | 'boundary' | 'exception'
  title: string
  params?: Record<string, unknown>
  expected: string
  script_path: string
  loadable: boolean
  low_confidence?: boolean
}

export interface ExecResult {
  case_id: string
  interface_id: string
  passed: boolean
  timeout?: boolean
  duration_ms?: number
  failure_kind?: 'assertion' | 'environment' | null
  failure_reason?: string
  skipped_reason?: string
  timestamp?: string
}

export interface ExecSummary {
  total: number
  passed: number
  failed_assertion: number
  failed_environment: number
  skipped?: number
}

export interface DefectEntry {
  id: string
  case_id: string
  interface_id: string
  failure_reason: string
  reproduce?: { script_path?: string; params?: Record<string, unknown> }
  status: 'discovered' | 'retesting' | 'passed' | 'closed_suggested' | 'retained'
  based_on_new_version?: boolean
  is_fallback?: boolean
  history: Array<{ event: string; timestamp: string }>
}

export interface RegressionIndexPayload {
  suites: RegressionSuite[]
  drift_warnings?: Array<{ case_id: string; detail: string }>
}

export interface RegressionSuite {
  protocol: string
  scripts: Array<{
    case_id: string
    script_path: string
    requirement_id: string
    interface_id: string
    archived_at: string
    version?: number
  }>
}

export interface MetricsPayload {
  job_id: string
  automation_rate: {
    value: number | null
    numerator: number
    denominator: number
    data_complete: boolean
  }
  nodes: Array<{
    name: string
    status: string
    degraded?: boolean
    degrade_reason?: string
    intervention_reason?: string
    automation_path?: string
  }>
  caveat?: string
  measured_at?: string
}
