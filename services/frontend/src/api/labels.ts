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
  missing: '未实现',
  undetermined: '无法判断',
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
