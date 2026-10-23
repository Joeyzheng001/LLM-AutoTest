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
  rest_url?: string | null
  nodes: NodeState[]
  degraded: DegradedRecord[]
  artifacts: string[]
  runtime_error?: { code?: string; message?: string } | string | null
  e2e?: {
    base_url: string
    generate_cases: boolean
    execute_cases: boolean
    insecure_tls?: boolean
  } | null
}

export interface JobSubmitResponse {
  job_id: string
  status: string
}

export interface PreflightResponse {
  ready: boolean
  checks: Array<{ name: string; status: 'ok' | 'error' | 'skipped'; message: string }>
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

export interface RerunPayload {
  from_node?: string
  llm?: LlmConfig
  rest_url?: string
  e2e?: E2eConfig
}

export interface StopJobResponse {
  job_id: string
  status: 'stopped'
  stopped_nodes: string[]
}

export interface LlmCallLog {
  call_id?: string
  run_id?: string
  run_scope?: 'current' | 'archive'
  run_file?: string
  timestamp: string
  node: string
  prompt_file?: string
  duration_ms?: number | null
  tokens?: number | null
  ok?: boolean
  event?:
    | 'started'
    | 'e2e_started'
    | 'e2e_case_started'
    | 'e2e_case_completed'
    | 'run_started'
    | 'node_started'
    | 'node_finished'
    | 'node_error'
    | 'node_skipped'
  batch?: { index: number; total: number; requirements?: number; claims?: number }
  index?: number
  total?: number
  case_id?: string
  message?: string
  code?: string
  error?: string | null
}

export interface RepoConfig {
  name: string
  source?: 'local' | 'git'
  path: string
  remote_url?: string | null
  branch: string
  commit?: string | null
  baseline_branch?: string // C-09：基线分支（非空 → 双分支 diff 变更范围视图）
  /** C-09 扩展：按提交号圈定变更范围（多个提交 diff 并集）；空 = 不圈定。 */
  commits?: string[]
  is_fallback: boolean
  include_paths?: string[]
  /** C-07：接口执行按 code_location.repo 路由到该仓库对应服务；为空回退任务级 rest_url。 */
  service_base_url?: string | null
  language?: 'java' | 'python' // C-13：仓库语言（java → java-parser；python → 内置 AST 扫描）
  access_token?: string
  access_token_configured?: boolean
}

/** 差异预览结果（POST /api/v1/repos/diff-preview，分支对比或提交号并集） */
export interface DiffPreviewResponse {
  name: string
  baseline: string | null
  target: string
  commits?: string[] // 提交号模式时回显
  added: string[]
  modified: string[]
  removed: string[]
  error?: string
}

// C-10：多需求文档（docs 列表；后端兼容旧单文档字段，前端统一走新口径）
export interface DocInput {
  doc_name: string
  doc_content_b64: string
}

export interface RequirementDocumentPairInput {
  target: DocInput
  baseline?: DocInput
}

export interface RequirementDocumentPairView {
  target_name: string
  baseline_name: string | null
}

// C-15：鉴权配置（可选；执行前登录被测服务注入凭据头，数值/凭据不经 LLM）
export interface AuthConfig {
  login_url: string
  username: string
  password: string
  insecure_tls?: boolean // https 自签场景默认跳过证书校验
  /** 登录请求 JSON 中的用户名字段；默认 username，例如部分系统使用 userName。 */
  username_field?: string
  /** 登录请求 JSON 中的密码字段；默认 password。 */
  password_field?: string
  /** plain 为原样提交；base64_shift_2 兼容部分历史 Web 登录页的密码传输转换。 */
  password_transform?: 'plain' | 'base64_shift_2'
  /** auto 自动识别常见 Token 路径；也可填写 data.accessToken 等 dotted path。 */
  token_path?: string
  /** 提取到 Token 后的 Authorization 注入格式；raw 表示不添加前缀。 */
  authorization_scheme?: 'Token' | 'Bearer' | 'raw'
  /** 非标准服务：登录响应字段到后续接口请求头的映射，例如 usertoken → body.token。 */
  response_header_paths?: Record<string, string>
}

export interface AuthVerificationResponse {
  authenticated: true
  session_id: string
  credential_type: 'token' | 'cookie' | 'headers'
}

export interface LlmConfig {
  api_key: string
  model: string
  base_url: string
  protocol: 'openai-chat' | 'openai-responses' | 'anthropic-messages'
  max_tokens: number
}

export interface LlmVerificationResponse {
  reachable: true
  source: 'service' | 'task'
  protocol: LlmConfig['protocol']
  model: string
  tokens: number | null
  duration_ms: number
}
export interface E2eConfig {
  base_url: string
  generate_cases: boolean
  execute_cases: boolean
  insecure_tls?: boolean
  auth_mode?: 'reuse' | 'browser'
  max_concurrency?: number
  max_routes?: number
  browser_channel?: string
  auto_login?: boolean
  roles?: Record<string, AuthConfig & { password_configured?: boolean }>
  data_fixtures?: Record<string, unknown>
  files?: Record<string, string>
}

/** 接口测试前置夹具；请求正文和控制地址不送入 LLM，场景选择元数据可参与源码预期判断。 */
export interface FixtureStep {
  name: string
  phase: 'setup' | 'teardown'
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  path: string
  base_url?: string
  query?: Record<string, unknown>
  body?: Record<string, unknown>
  expected: string
  required: boolean
  scope?: 'job' | 'case'
  control_kind?: 'downstream_response' | 'application_config' | 'service_registry'
  interface_id?: string
  scenario_contains?: string
}

export interface JobSubmitPayload {
  job_id?: string
  docs: DocInput[]
  document_pairs?: RequirementDocumentPairInput[]
  repos: RepoConfig[] // C-10：≥1 个仓库（原 ≥2 放宽）
  rest_url: string
  rest_insecure_tls?: boolean
  resume?: boolean
  auth?: AuthConfig // C-15：鉴权配置（可选）
  db_readonly?: ReadOnlyDatabaseConfig
  auth_session_id?: string
  llm?: LlmConfig // 运行时模型配置；密钥不在任务产物中保存
  e2e?: E2eConfig
  fixtures?: FixtureStep[]
  auto_record_fixtures?: boolean
  generate_unit_tests?: boolean
  observe_runtime_contracts?: boolean
}

export interface ReadOnlyDatabaseConfig {
  database_type: 'mysql' | 'postgresql' | 'oracle' | 'sqlserver'
  host: string
  port: number
  database: string
  schema_name?: string
  username: string
  password: string
}

/** 完整任务配置读取结果；密码和 API Key 永不返回，只说明能否复用。 */
export interface JobConfiguration {
  job_id: string
  documents: string[]
  document_pairs: RequirementDocumentPairView[]
  repos: RepoConfig[]
  rest_url: string
  rest_insecure_tls: boolean
  resume: boolean
  auth: (Omit<AuthConfig, 'password'> & { password_configured: boolean }) | null
  db_readonly: (Omit<ReadOnlyDatabaseConfig, 'password'> & { password_configured: boolean }) | null
  llm:
    | (Omit<LlmConfig, 'api_key'> & {
        api_key_configured: boolean
        api_key_encrypted?: boolean
      })
    | null
  e2e: E2eConfig | null
  fixtures: FixtureStep[]
  auto_record_fixtures: boolean
  generate_unit_tests: boolean
  observe_runtime_contracts: boolean
}

export interface JobConfigurationUpdatePayload {
  docs: DocInput[]
  document_pairs?: RequirementDocumentPairInput[]
  keep_existing_docs: boolean
  repos: RepoConfig[]
  rest_url: string
  rest_insecure_tls: boolean
  resume: boolean
  run_after_save?: boolean
  /** false 明确切换为服务级 LLM 配置并清除任务级覆盖。 */
  llm_enabled?: boolean
  auth?: Omit<AuthConfig, 'password'> & { password?: string }
  db_readonly?: Omit<ReadOnlyDatabaseConfig, 'password'> & { password?: string }
  db_readonly_enabled?: boolean
  auth_session_id?: string
  llm?: Omit<LlmConfig, 'api_key'> & { api_key?: string }
  e2e?: E2eConfig
  fixtures?: FixtureStep[]
  replace_fixtures?: boolean
  auto_record_fixtures?: boolean
  generate_unit_tests?: boolean
  observe_runtime_contracts?: boolean
}

export interface JobConfigurationUpdateResponse {
  job_id: string
  status: 'configuration-saved-and-rerun-queued' | 'configuration-saved'
  from_node: 'req_parse' | null
}

/* ---- 产物形状（只声明页面用到的字段；后端 Schema 为唯一事实源） ---- */

export interface RequirementItem {
  id: string
  title: string
  description: string
  priority?: string
  stub_note?: string
  source_files?: string[] // C-10：全部来源文档路径
  source_refs?: string[]
  source_location?: { section?: string; line?: number }
  claims?: RequirementClaim[]
}

export interface RequirementClaim {
  claim_id: string
  kind: string
  statement: string
  acceptance_criteria?: string[]
  source_refs?: string[]
}

export interface RequirementSource {
  source_id: string
  document: string
  section?: string
  paragraph_index: number
  fragment_index: number
  excerpt: string
  excerpt_truncated?: boolean
  content_hash?: string
  pair_index?: number
  version?: 'baseline' | 'target'
}

export interface RequirementReviewIssue {
  type: '缺失信息' | '歧义' | '冲突' | '不可测' | '其它' | '建议'
  priority: '高' | '中' | '低'
  location: string
  question: string
  reason: string
  impact: string
  dimension?: string
  claim_ids?: string[]
  source_refs?: string[]
}

export interface RequirementReviewMindmap {
  主题: string
  子主题: RequirementReviewMindmap[]
}

export interface RequirementReview {
  score: number
  quality_grade: 'A' | 'B' | 'C' | 'D'
  summary: string
  dimension_scores: Record<string, { weight: number; score: number; reason: string }>
  quality_assessment: { grade: 'A' | 'B' | 'C' | 'D'; grade_name: string; summary_reason: string }
  issues: RequirementReviewIssue[]
  readable_issue_points: string[]
  completeness_issues: string[]
  consistency_issues: string[]
  untestable_items: string[]
  risk_flags: Array<{ type: string; desc: string }>
  testable_features: string[]
  mindmap: RequirementReviewMindmap
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
  signature: {
    method?: string
    path?: string
    service_method?: string
    summary?: string
  }
  params?: InterfaceParam[]
  returns?: { type?: string; description?: string }
  parameter_evidence?: Array<Record<string, unknown>>
  method_body_truncated?: boolean
  method_body_char_count?: number
  method_body_context?: Array<{ start_line: number; end_line: number; text: string }>
  call_graph?: {
    status?: 'complete' | 'partial'
    resolved_calls?: number
    resolved_nodes?: number
    unresolved_calls?: number
    max_depth?: number
  }
  call_graph_incomplete?: boolean
  unresolved?: Array<Record<string, unknown>>
  code_location?: CodeLocation
  is_fallback?: boolean
}

export interface InterfaceParamField {
  name: string
  type?: string
  required?: boolean
  required_evidence?: string
  description?: string
  default?: unknown
  constraints?: string[]
  observed_values?: unknown[]
  source_refs?: Array<{ file?: string; line?: number }>
}

export interface InterfaceParam {
  name: string
  type?: string
  required?: boolean
  required_evidence?: string
  in?: string
  default?: unknown
  fields?: InterfaceParamField[]
  constraints?: string[]
  observed_values?: unknown[]
  source_refs?: Array<{ file?: string; line?: number }>
}

export interface InterfaceCandidate {
  candidate_id: string
  protocol: string
  signature: string
  code_location: CodeLocation
  is_fallback?: boolean
  confidence?: number
  method_body_truncated?: boolean
  method_body_char_count?: number
  call_graph?: {
    status?: 'complete' | 'partial'
    resolved_calls?: number
    resolved_nodes?: number
    unresolved_calls?: number
    max_depth?: number
  }
  call_graph_incomplete?: boolean
  unresolved?: Array<Record<string, unknown>>
}

export interface InterfaceScanCapabilities {
  requested?: string[]
  supported?: string[]
  known?: string[]
  unsupported?: string[]
}

export interface ReviewMapping {
  requirement_id: string
  interface_id?: string | null
  interface_ids?: string[]
  claim_ids?: string[]
  claim_counts?: {
    total: number
    implemented: number
    partial: number
    missing: number
    undetermined: number
  }
  status: 'implemented' | 'partial' | 'missing' | 'undetermined'
  confidence: number
  /** 确定性门槛封顶前的原始置信度；仅当封顶实际压低数值时存在 */
  raw_confidence?: number
  confidence_capped?: boolean
  evidence_quality?: 'behavior_path' | 'interface_only' | 'insufficient' | 'none'
  evidence?: { refs?: string[]; code_location?: CodeLocation; note?: string }
  claim_assessments?: ReviewClaimAssessment[]
}

export interface FieldValueContract {
  interface_id: string
  parameter: string
  field: string
  type: string
  required?: boolean
  required_evidence?: string
  value_rule_status: 'coded_values' | 'examples_only' | 'unspecified'
  description: string
  description_origin?: 'source' | 'inferred' | 'unknown'
  documented_values: Array<{ code: string; label: string }>
  example_codes: string[]
  constraints?: string[]
  source_refs: Array<{ file?: string; line?: number }>
}

export interface ReviewClaimAssessment {
  claim_id: string
  status: 'implemented' | 'partial' | 'missing' | 'undetermined'
  confidence: number
  /** 确定性门槛封顶前的原始置信度；仅当封顶实际压低数值时存在 */
  raw_confidence?: number
  confidence_capped?: boolean
  interface_ids: string[]
  evidence_refs: string[]
  met_conditions: string[]
  unmet_conditions: string[]
  verification_needed: string[]
}

export interface ReviewEvidence {
  evidence_id: string
  category: string
  repo: string
  file: string
  start_line: number
  end_line: number
  excerpt: string
  excerpt_truncated?: boolean
  interface_ids?: string[]
  role?: string
}

export interface OracleStateCondition {
  kind: 'downstream_response' | 'application_config' | 'service_registry' | 'test_data'
  description: string
  mode: 'integration' | 'controlled'
  evidence_ref: string
  quote: string
}

export interface ReviewTestOracle {
  oracle_id: string
  interface_id: string
  category: 'normal' | 'boundary' | 'exception'
  scenario: string
  control_kinds?: Array<'downstream_response' | 'application_config' | 'service_registry'>
  branch_outcome?: boolean
  expected_behavior?: { description: string; evidence_ref: string; quote: string }
  state_conditions?: OracleStateCondition[]
  execution_readiness?: 'ready' | 'conditional' | 'blocked'
  execution_blockers?: string[]
  preconditions: Array<{ path: string; op: string; value?: unknown }>
  expected_status: string
  assertions: Array<{ path: string; op: string; value?: unknown }>
  evidence_refs: string[]
  fixture_plan?: {
    kind: 'create_query_soft_delete'
    add_interface_id: string
    query_interface_id: string
    delete_interface_id: string
    cleanup_mode: 'soft_delete'
    source_refs: string[]
  }
}

export interface ReviewBranchCandidate {
  interface_id: string
  branch_id: string
  branch_ids?: string[]
  node_kind: 'if' | 'catch' | 'return'
  evidence_ref: string
  quote: string
  control_kind:
    'request' | 'downstream_response' | 'application_config' | 'service_registry' | 'unresolved'
  required_controls: Array<'downstream_response' | 'application_config' | 'service_registry'>
  source_refs?: string[]
  oracle_ids: string[]
  oracle_outcomes: boolean[]
}

export interface ReviewCallChain {
  interface_id: string
  entry: {
    repo?: string
    file?: string
    role?: string
    method_name?: string
    start_line?: number | null
    end_line?: number | null
  }
  callees: Array<
    | string
    | {
        file?: string
        role?: string
        method_name?: string
        start_line?: number | null
        end_line?: number | null
        depth?: number | null
        call_line?: number | null
      }
  >
  edges?: Array<{
    caller_file: string
    caller_role?: string
    caller_method?: string
    caller_start_line: number
    caller_end_line?: number
    callee_file: string
    callee_role?: string
    callee_method?: string
    callee_start_line: number
    callee_end_line?: number
    depth: number
    call_line?: number
  }>
  status?: 'complete' | 'partial'
  resolved_calls?: number
  resolved_nodes?: number
  unresolved_calls?: number
  unresolved?: Array<Record<string, unknown>>
  max_depth?: number
  /** 调用链分析方法标注（旧产物可能仍为 confidence 键，故可选） */
  analysis_method?: 'static_recursive' | 'legacy_static_one_hop'
}

export interface ReviewScanCoverage {
  complete: boolean
  review_evidence_limited: boolean
  interface_evidence_truncated?: boolean
  interface_evidence_limit?: number
  interface_evidence_count?: number
  interface_evidence_missing?: string[]
  call_chain_truncated?: boolean
  call_chain_limit?: number
  interfaces_total?: number
  scan_integrity_complete?: boolean
  scan_integrity_reasons?: string[]
  repositories: Array<{
    repo: string
    status: string
    truncated_scope: string[]
  }>
}

export interface DeterministicReviewCheck {
  requirement_id: string
  claim_id?: string
  rule_id: string
  category: string
  status: 'evidence_found' | 'needs_verification'
  description: string
  evidence_ids?: string[]
}

export interface CoveredClaimGroup {
  requirement_id: string
  claim_ids: string[]
  status: 'implemented' | 'partial' | 'missing' | 'undetermined'
}

// C-108 视图二：LLM 影响分析（接口编号经程序校验，分析文本为模型生成）
export interface ChangeImpactAnalysis {
  summary: string
  impact_surface?: string[]
  test_suggestion?: string
  risk_level: 'P0' | 'P1' | 'P2'
}

// C-108：LLM 变更总评（LLM 失败时后端整键缺省）
export interface ChangeSummary {
  nature: string
  overall_risk: 'P0' | 'P1' | 'P2'
  regression_advice: string
}

// C-108：盲区文件归因（引用编号经程序校验，仅保留真实存在的接口/需求）
export interface UnmatchedAttribution {
  likely_interfaces?: string[]
  likely_requirements?: string[]
  rationale?: string
}

// C-09 视图二：变更范围（事实字段双分支 diff 程序化派生；impact_analysis 由 LLM 生成，C-108）
export interface ChangeScopeEntry {
  interface_id: string
  repo: string
  file: string
  change: 'added' | 'modified'
  covered_by: string[] // 空 = 变更接口未被任何需求覆盖（变更风险信号）
  covered_claims?: CoveredClaimGroup[]
  matched_via?: 'definition' | 'related_file'
  related_changed_files?: Array<{ file: string; role: string; change: 'added' | 'modified' }>
  evidence_level?: 'body' | 'callee' | 'file' | 'related_file' | 'none'
  risk_level?: 'P0' | 'P1' | 'P2' // 程序派生的回归优先级下限（LLM 失败时的兜底）
  impact_analysis?: ChangeImpactAnalysis
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
  attribution?: UnmatchedAttribution
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
export interface OracleDiagnostic {
  status:
    | 'framework_boundary'
    | 'control_required'
    | 'response_evidence_missing'
    | 'candidate_invalid'
    | 'resolved'
    | 'replaced'
    | 'execution_pending'
    | 'environment_pending'
  interface_id: string
  scenario: string
  message: string
  summary?: string
  oracle_ids?: string[]
  action: string
  control_kinds: Array<'downstream_response' | 'application_config' | 'service_registry'>
}

export interface TestCaseEntry {
  oracle_basis?: string
  oracle_id?: string
  oracle_status?: 'verified' | 'unverified'
  id: string
  interface_id: string
  requirement_id?: string
  claim_id?: string
  testpoint_id?: string
  oracle_scenario?: string
  expected_behavior?: { description: string; evidence_ref: string; quote: string }
  state_conditions?: OracleStateCondition[]
  execution_readiness?: 'ready' | 'conditional' | 'blocked'
  execution_blockers?: string[]
  request_conditions?: Array<{ path: string; op: string; value?: unknown }>
  oracle_control_kinds?: Array<'downstream_response' | 'application_config' | 'service_registry'>
  category: 'normal' | 'boundary' | 'exception'
  title: string
  params?: Record<string, unknown>
  expected: string
  assertions?: Array<{
    path: string
    op:
      | 'equals'
      | 'not_equals'
      | 'exists'
      | 'not_exists'
      | 'null_or_missing'
      | 'not_blank'
      | 'equals_request'
      | 'in'
      | 'not_in'
      | 'non_empty'
      | 'contains'
      | 'greater_than'
      | 'less_than'
    value?: unknown
  }>
  script_path: string
  loadable: boolean
  low_confidence?: boolean
}

export interface E2eTestStep {
  action: string
  intent?: string
  frame?: { url?: string; name?: string } | null
  page?: string
  variable?: string
  resource?: string
  basis?: { rule_id: string; quote: string }
  selector?: string
  value?: unknown
}

export interface E2eTestCaseEntry {
  id: string
  case_type: 'e2e'
  contract_version?: '2.0'
  design_id?: string
  rule_refs?: string[]
  scenario_type?: string
  priority?: string
  role?: string
  setup?: E2eTestStep[]
  precondition_checks?: E2eTestStep[]
  cleanup?: E2eTestStep[]
  execution_blockers?: string[]
  execution_readiness?: string
  requirement_id: string
  claim_id?: string
  testpoint_id?: string
  title: string
  preconditions?: string[]
  steps: E2eTestStep[]
  expected: string
}

export interface UnitTestOutcome {
  case_id: string
  status:
    | 'passed'
    | 'failed'
    | 'not_run'
    | 'build_failed'
    | 'generation_failed'
    | 'fixture_failed'
    | 'unsupported'
    | 'source_not_covered'
  test_class?: string
  source_artifact?: string
  log_artifact?: string
  source?: string
  reused_verified_source?: boolean
  reason?: string
  tests?: number
  failures?: number
  errors?: number
  skipped?: number
  covered_evidence_lines?: number
  interface_id?: string
  title?: string
  target_source?: string
  target_class?: string
  target_method?: string
  source_kind?: 'interface_control' | 'source_branch'
  scenario_type?: string
  branch_ids?: string[]
  evidence_refs?: string[]
  linked_interface_case_ids?: string[]
  gap_kind?: 'mock_or_config' | 'component_detail' | 'no_http_case'
  gap_reason?: string
  request_params?: Record<string, unknown>
  mock_conditions?: Array<Record<string, unknown> | string>
  expected?: unknown
}

export interface UnitTestCasePlan {
  case_id: string
  interface_id: string
  oracle_id?: string
  title?: string
  repo: string
  source_sha?: string
  module?: string
  test_framework?: string
  test_class?: string
  target_source?: string
  target_class?: string
  target_method?: string
  source_kind?: 'interface_control' | 'source_branch'
  scenario_type?: string
  branch_ids?: string[]
  evidence_refs?: string[]
  linked_interface_case_ids?: string[]
  gap_kind?: 'mock_or_config' | 'component_detail' | 'no_http_case'
  gap_reason?: string
  request_params?: Record<string, unknown>
  mock_conditions?: Array<Record<string, unknown> | string>
  expected?: unknown
  status: 'generated' | 'generation_failed' | 'unsupported'
  reason?: string
  source_artifact?: string
  source?: string
}

export interface UnitCoverageModule {
  module: string
  case_id: string
  line_coverage: {
    covered: number
    missed: number
    rate: number | null
  }
}

export interface UnitCoverageCounter {
  covered: number
  missed: number
  rate: number | null
}

export interface UnitSuiteCoverage {
  module: string
  status: 'completed' | 'partial' | 'unavailable'
  reason?: string
  scope: 'generated_tests_only'
  tests_executed?: number
  tests_failed?: number
  line_coverage?: UnitCoverageCounter | null
  branch_coverage?: UnitCoverageCounter | null
  classes?: Array<{
    name: string
    source_file: string
    case_ids: string[]
    line_coverage: UnitCoverageCounter | null
    branch_coverage: UnitCoverageCounter | null
    missed_lines?: number[]
  }>
}

export interface UnitTestCoverage {
  status: string
  reason?: string
  cases_total: number
  cases_passed: number
  cases_failed: number
  repositories: Array<{
    repo: string
    source_sha?: string
    status: string
    reason?: string
    modules?: UnitCoverageModule[]
    suite_coverage?: {
      scope: 'generated_tests_only'
      modules: UnitSuiteCoverage[]
    }
    cases: UnitTestOutcome[]
  }>
}

export interface ExecResult {
  unit_test?: UnitTestOutcome
  run_id?: string
  scenario_type?: string
  outcome?: string
  step_results?: BrowserStepResult[]
  failed_step_id?: string
  evidence_complete?: boolean
  evidence_errors?: string[]
  evidence_files?: string[]
  network_requests?: number
  trace_ref?: string
  oracle_status?: 'verified' | 'unverified'
  case_id: string
  interface_id: string
  case_type?: 'api' | 'e2e'
  requirement_id?: string
  claim_id?: string
  testpoint_id?: string
  title?: string
  category?: 'normal' | 'boundary' | 'exception' | 'e2e'
  assertion_scope?:
    | 'business'
    | 'response_structure'
    | 'framework_boundary'
    | 'status_only'
    | 'ui_steps'
    | 'unverified'
  assertion_results?: Array<{
    path: string
    op: string
    expected?: unknown
    actual?: unknown
    found: boolean
    passed: boolean
  }>
  evidence?: {
    kind: 'http_response' | 'browser'
    method?: string
    path?: string
    page_url?: string
    page_title?: string
    screenshot_ref?: string
    console_errors?: string[]
    request_failures?: string[]
    network_ref?: string
    console_ref?: string
    capture_error?: string
  }
  request?: {
    method?: string
    url?: string
    headers?: Record<string, unknown>
    query_params?: unknown
    body?: unknown
    attempted?: boolean
  }
  passed: boolean
  timeout?: boolean
  duration_ms?: number
  failure_kind?: 'assertion' | 'environment' | null
  failure_reason?: string
  expected?: string
  actual_status?: number
  actual_body?: unknown
  skipped_reason?: string
  execution_state?: 'executed' | 'not_executed' | 'observed_unjudged' | 'reused_response'
  expectation_source?: 'user_confirmed_environment' | 'local_dependency_bytecode'
  execution_blockers?: string[]
  missing_control_kinds?: string[]
  source_case_id?: string
  source_params?: Record<string, unknown>
  branch_observation?: {
    status: 'unconfirmed' | 'not_reached' | 'matched_observable_path'
    unknown_conditions: string[]
  }
  timestamp?: string
}

export interface ExecSummary {
  total: number
  passed: number
  failed_assertion: number
  failed_environment: number
  skipped?: number
  actual_requests?: number
  observed_unjudged?: number
  not_executed?: number
  reused_response?: number
  by_type?: Record<'api' | 'e2e', Omit<ExecSummary, 'by_type'>>
}

export interface JobRun {
  id: string
  is_current: boolean
  executed_at: string | null
  archived_at: string | null
  has_report: boolean
  summary: Pick<
    ExecSummary,
    | 'total'
    | 'passed'
    | 'failed_assertion'
    | 'failed_environment'
    | 'observed_unjudged'
    | 'not_executed'
  >
}

export interface DefectEntry {
  execution_snapshot?: ExecResult
  title?: string
  id: string
  case_id: string
  interface_id: string
  requirement_id?: string
  claim_id?: string
  testpoint_id?: string
  failure_reason: string
  triage?: {
    kind: 'input_construction' | 'assertion_or_setup' | 'product_suspect' | 'needs_evidence'
    confidence: 'high' | 'medium' | 'low'
    reason: string
  }
  interface?: { protocol?: string; method?: string; path?: string; summary?: string }
  testcase?: { title?: string; category?: string; expected?: string }
  actual?: { status?: number; body?: unknown; assertion_results?: ExecResult['assertion_results'] }
  evidence?: ExecResult['evidence']
  reproduce?: { script_path?: string; params?: Record<string, unknown> }
  status: 'discovered' | 'retesting' | 'passed' | 'closed_suggested' | 'retained'
  based_on_new_version?: boolean
  is_fallback?: boolean
  history: Array<{ event: string; timestamp: string }>
}

export interface RegressionIndexPayload {
  suites: RegressionSuite[]
  empty_reason?: string
  drift_warnings?: Array<{ case_id: string; detail: string }>
}

export interface RegressionSuite {
  protocol: string
  scripts: Array<{
    case_id: string
    script_path: string
    requirement_id: string
    claim_id?: string
    testpoint_id?: string
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

export interface BrowserStepResult {
  step_id: string
  phase: 'setup' | 'precondition_checks' | 'steps' | 'cleanup'
  intent: string
  action: string
  status: 'running' | 'passed' | 'failed'
  step?: E2eTestStep
  actual?: unknown
  error?: string
  duration_ms?: number
  before_ref?: string
  after_ref?: string
  before_state_ref?: string
  after_state_ref?: string
  frame?: { url?: string; name?: string } | null
  assertion?: NonNullable<ExecResult['assertion_results']>[number]
  recovery?: unknown
}

export interface E2eDesign {
  progress?: { intent?: string; pages_read?: number; pages_total?: number }
  rules?: Array<{ rule_id: string; requirement_id: string; statement: string }>
  run_id: string
  status: string
  cases: E2eTestCaseEntry[]
  scenarios?: Array<{
    scenario_id: string
    title: string
    scenario_type: string
    design_status?: string
    reason?: string
  }>
  gaps?: Array<{ rule_id?: string; scenario_id?: string; stage?: string; reason: string }>
  coverage?: { rules: number; rules_with_cases: number; scenarios: number; cases: number }
  exploration_pending?: Array<{ url?: string; state?: string; reason: string }>
}
export interface E2eExecution {
  planned_cases?: number
  status?: string
  run_id: string
  design_run_id: string
  results: ExecResult[]
  summary: ExecSummary & {
    pending?: number
    unjudged?: number
    blocked?: number
    automation_errors?: number
    executed_cases?: number
    steps?: number
    assertions?: number
    evidence_incomplete?: number
    outcomes?: Record<string, number>
  }
  report_ref?: string
  scope?: 'all' | 'selected'
}
export interface E2eOperationStatus {
  job_id: string
  running: boolean
  configuration?: E2eConfig | null
  operation?: {
    operation: string
    status: string
    error?: string
    progress?: { event: string; case_id: string; step_id: string; intent?: string; status?: string }
  }
}
