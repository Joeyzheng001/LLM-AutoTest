---
kind: external_dependency
name: 企业私有 GitLab 仓库与 CI/CD 托管平台
slug: gitlab-htffund
category: external_dependency
category_hints:
    - vendor_identity
    - client_constraint
scope:
    - '**'
source_files:
    - deploy/docker-compose.yml
    - .github/workflows/ci.yml
---

### 身份与角色
- 本项目代码、MumuSpec 十四文档、Spec 模板与规则均托管在内部 GitLab：`http://gitlab.htffund.com/zhengzhong/llm-autotest.git`。
- 通过 `.github/workflows/ci.yml` 接入 GitHub Actions，但实际代码源是上述 GitLab；本地开发使用 `git push` 推送至该远端。

### 认证方式
- 支持两种凭据：Personal Access Token（需 `api` 或 `write_repository` scope）和用户名+密码/Token 内嵌 URL。
- HTTPS 443 端口不通，必须使用 HTTP 80 端口并通过 `credential.http://gitlab.htffund.com.allow=always` 允许不安全连接；SSH 22 端口同样被防火墙拦截。
- 项目由 `zhengzhong` 创建，开发者需要被添加为 **Developer** 或 **Maintainer** 才能推送。

### 关键约束
- 远程 URL 必须指向 `http://gitlab.htffund.com/...`（非 HTTPS），且需在本地配置 credential helper 允许 HTTP。
- 推送失败常见原因：Token 过期/权限不足、账号不是项目成员、当前目录不是 git 仓库。