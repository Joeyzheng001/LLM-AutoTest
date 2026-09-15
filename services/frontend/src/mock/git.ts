/**
 * Git 仓库与分支 mock 服务：模拟从 Git 服务（内部 GitLab）拉取仓库列表与分支。
 * 红线：仅开发期演示；正式数据对接 orchestrator REST API（D3 冻结后替换）。
 * 口径：每个被测仓库需指定「被测分支 + 基线分支」——code_review 节点按两分支
 * 差异做变更定位（07 §3.1 演示叙事的输入通道）。
 */

export interface GitRepoInfo {
  /** 仓库唯一名（与 java-parser RepositoryInput.name 对齐） */
  name: string
  /** 克隆地址 */
  url: string
  defaultBranch: string
  /** 可选分支（远端已有） */
  branches: string[]
}

const MOCK_REPOS: GitRepoInfo[] = [
  {
    name: 'user-service',
    url: 'http://172.25.200.46/demo/user-service.git',
    defaultBranch: 'release/v2.3',
    branches: ['release/v2.3', 'release/v2.2', 'develop', 'master'],
  },
  {
    name: 'order-center',
    url: 'http://172.25.200.46/demo/order-center.git',
    defaultBranch: 'develop',
    branches: ['develop', 'master', 'feature/order-export'],
  },
  {
    name: 'payment-gateway',
    url: 'http://172.25.200.46/demo/payment-gateway.git',
    defaultBranch: 'master',
    branches: ['master', 'hotfix/timeout'],
  },
  {
    name: 'fallback-demo',
    url: 'http://172.25.200.46/demo/fallback-demo.git',
    defaultBranch: 'master',
    branches: ['master', 'bugfix/assert-fail'],
  },
]

/** 模拟网络延迟（毫秒） */
const FAKE_LATENCY_MS = 400

export function fetchGitRepos(gitServerUrl: string): Promise<GitRepoInfo[]> {
  // gitServerUrl 记录意图：正式实现按该地址调 GitLab API；mock 忽略取值
  void gitServerUrl
  return new Promise((resolve) => {
    setTimeout(
      () => resolve(MOCK_REPOS.map((repo) => ({ ...repo, branches: [...repo.branches] }))),
      FAKE_LATENCY_MS,
    )
  })
}
