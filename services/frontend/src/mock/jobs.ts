/**
 * Mock 数据：口径与 schemas/ + state.json 一致（_fake 语义：仅开发期展示）。
 * 红线（.rules/frontend-code-style）：数值一律来自后端 API，前端不做计算——
 * automationRate 等字段在后端程序化汇总（F-MTR-03），此处 mock 直供展示值。
 */

export type NodeStatus = 'completed' | 'running' | 'manual_intervention' | 'pending'

export interface PipelineNode {
  name: string
  label: string
  status: NodeStatus
}

export interface DegradedRecord {
  node: string
  reason: string
}

export interface JobSummary {
  id: string
  title: string
  createdAt: string
  /** 后端汇总值（completed/9）；数据不完整时为 null（X-MTR-02） */
  automationRate: number | null
  degraded: DegradedRecord[]
  hasFallbackRepo: boolean
  nodes: PipelineNode[]
}

const NODE_LABELS: Array<[string, string]> = [
  ['req_parse', '需求解析'],
  ['code_scan', '源码扫描'],
  ['interface_reconstruct', '接口还原'],
  ['code_review', 'Code Review'],
  ['testcase_generate', '用例生成'],
  ['auto_execute', '自动执行'],
  ['test_report', '测试报告'],
  ['defect_list', '缺陷清单'],
  ['retest_regression', '回归沉淀'],
]

function buildNodes(statuses: NodeStatus[]): PipelineNode[] {
  return NODE_LABELS.map(([name, label], index) => ({
    name,
    label,
    status: statuses[index] ?? 'pending',
  }))
}

export const mockJobs: JobSummary[] = [
  {
    id: 'job-3f2a1b8c-0000-4000-8000-000000000001',
    title: '用户服务 V2.3 回归',
    createdAt: '2026-09-15T02:00:00Z',
    automationRate: 7 / 9,
    hasFallbackRepo: false,
    degraded: [{ node: 'code_scan', reason: '仓库不可达，转为人工扫描' }],
    nodes: buildNodes([
      'completed',
      'manual_intervention',
      'completed',
      'completed',
      'completed',
      'completed',
      'running',
      'pending',
      'pending',
    ]),
  },
  {
    id: 'job-7c9e4d21-0000-4000-8000-000000000002',
    title: '订单中心冒烟（含兜底仓库）',
    createdAt: '2026-09-15T06:30:00Z',
    automationRate: 3 / 9,
    hasFallbackRepo: true,
    degraded: [],
    nodes: buildNodes([
      'completed',
      'completed',
      'running',
      'pending',
      'pending',
      'pending',
      'pending',
      'pending',
      'pending',
    ]),
  },
  {
    id: 'job-a1b2c3d4-0000-4000-8000-000000000003',
    title: '支付网关演示',
    createdAt: '2026-09-15T08:10:00Z',
    automationRate: null,
    hasFallbackRepo: false,
    degraded: [],
    nodes: buildNodes([
      'running',
      'pending',
      'pending',
      'pending',
      'pending',
      'pending',
      'pending',
      'pending',
      'pending',
    ]),
  },
]
