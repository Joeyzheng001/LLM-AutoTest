---
version: 1.0
spec: M-REV-01
owner: QA
changelog:
  - 1.0 | 2026-09-17 | 服务端评审经验
sampling:
  temperature: 0.2
---

已验证经验：Session 有效期、Token 失效、权限控制、事务一致性和缓存行为通常不在接口层暴露。评审必须给出服务端配置或实现证据；无证据只能得出无法判断，不能基于接口返回格式推断结论。

