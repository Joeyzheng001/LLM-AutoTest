import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  clearAccessContext,
  getAccessMode,
  getAccessContext,
  getLlmCallLogs,
  getReportMd,
  listJobs,
  setAccessContext,
} from './client'

class MemorySessionStorage implements Storage {
  private readonly values = new Map<string, string>()

  get length(): number {
    return this.values.size
  }

  clear(): void {
    this.values.clear()
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null
  }

  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null
  }

  removeItem(key: string): void {
    this.values.delete(key)
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value)
  }
}

describe('共享访问上下文', () => {
  const storage = new MemorySessionStorage()

  afterEach(() => {
    storage.clear()
    vi.unstubAllGlobals()
  })

  it('只保存当前会话的令牌和项目，并可清除', () => {
    vi.stubGlobal('sessionStorage', storage)

    setAccessContext({ token: 'token-a', projectId: 'fund' })
    expect(getAccessContext()).toEqual({ token: 'token-a', projectId: 'fund' })

    clearAccessContext()
    expect(getAccessContext()).toEqual({ token: '', projectId: '' })
  })

  it('为 API 请求注入 Bearer Token 和项目头', async () => {
    vi.stubGlobal('sessionStorage', storage)
    setAccessContext({ token: 'token-a', projectId: 'fund' })
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ jobs: [] }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await listJobs()

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toBe('Bearer token-a')
    expect(headers.get('X-Project-Id')).toBe('fund')
  })

  it('报告和运行日志读取也复用同一认证上下文', async () => {
    vi.stubGlobal('sessionStorage', storage)
    setAccessContext({ token: 'token-a', projectId: 'fund' })
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, text: async () => '# report' })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ logs: [] }) })
    vi.stubGlobal('fetch', fetchMock)

    await getReportMd('job-1')
    await getLlmCallLogs('job-1')

    for (const call of fetchMock.mock.calls) {
      const headers = new Headers((call[1] as RequestInit).headers)
      expect(headers.get('Authorization')).toBe('Bearer token-a')
      expect(headers.get('X-Project-Id')).toBe('fund')
    }
  })

  it('单用户入口模式可在未配置令牌时读取', async () => {
    vi.stubGlobal('sessionStorage', storage)
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ mode: 'single_user', user: 'admin' }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(getAccessMode()).resolves.toEqual({ mode: 'single_user', user: 'admin' })
    const headers = new Headers((fetchMock.mock.calls[0]?.[1] as RequestInit).headers)
    expect(headers.get('Authorization')).toBeNull()
  })
})
