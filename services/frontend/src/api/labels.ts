/** 节点/状态中文标签（与 state.json 九节点口径、Schema 枚举一一对应）。 */

import type { NodeStatus } from './types'

export const NODE_LABELS: Record<string, string> = {
  req_parse: '需求解析',
  code_scan: '源码扫描',
  interface_reconstruct: '接口还原',
  code_review: 'Code Review',
  testcase_generate: '用例生成',
  auto_execute: '自动执行',
  test_report: '测试报告',
  defect_list: '缺陷清单',
  retest_regression: '回归沉淀',
}

export const STATUS_LABELS: Record<NodeStatus, string> = {
  completed: '已完成',
  running: '运行中',
  manual_intervention: '人工介入',
  pending: '待运行',
}

export const MAPPING_STATUS_LABELS: Record<string, string> = {
  implemented: '已实现',
  partial: '部分实现',
  missing: '已确认未实现',
  undetermined: '待核查',
}

export const EVIDENCE_QUALITY_LABELS: Record<string, string> = {
  behavior_path: '行为链路证据',
  interface_only: '仅接口证据',
  insufficient: '证据不足',
  none: '暂无证据',
}

export const DEFECT_STATUS_LABELS: Record<string, string> = {
  discovered: '发现',
  retesting: '复测中',
  passed: '复测通过',
  closed_suggested: '关闭建议',
  retained: '保留',
}

export const DEFECT_EVENT_LABELS: Record<string, string> = {
  discovered: '发现',
  retest_started: '复测开始',
  retest_passed: '复测通过',
  retest_failed: '复测失败',
  close_suggested: '关闭建议',
  retained: '保留',
}

// C-10：仓库扫描四态（X-CODE-01~03）与用例三类（F-GEN-01）
export const REPO_STATUS_LABELS: Record<string, string> = {
  ok: '正常',
  empty: '空仓库（X-CODE-02，流程继续）',
  truncated: '截断扫描（X-CODE-03）',
  error: '扫描失败（X-CODE-01）',
}

export const CASE_CATEGORY_LABELS: Record<string, string> = {
  normal: '正常',
  boundary: '边界',
  exception: '异常',
}
