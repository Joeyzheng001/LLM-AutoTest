# 埋入缺陷说明（X-DEF-03 / D4：乙→丙交付物）

验收闭环依赖人工埋入缺陷（AC3）。埋入位置**固定为业务逻辑层**，以独立分支承载，
不进入被测仓库主干。

## DEF-001：用户年龄错误（已埋）

| 项 | 内容 |
|---|---|
| 仓库 | `user-service` |
| 接口 | `GET /api/v1/users/{id}` |
| 位置 | `UserService#getUser`（业务逻辑层） |
| 变体 | `defects/UserService.DEF-001.java`（`getUser` 返回 `age + 1`） |
| 预期失败用例 | `GET /api/v1/users/1` → 期望 `age == 28`，实际返回 `29` → **断言失败**（assertion 类，非环境类） |
| 流转预期 | 断言失败 → 缺陷清单 DEF-###（discovered）→ 复测仍失败（retained）→ 还原主干版本 → 复测通过（closed_suggested）+ 脚本归档 `scripts/regression/rest/` |

## 演示步骤（分支语义，埋入变更不落主干）

```bash
cd deploy/demo-repos/user-service
git init -q && git add -A && git commit -qm "main：无缺陷基线"

# 埋入缺陷（独立分支，主干不动）
git checkout -qb defect/DEF-001
cp defects/UserService.DEF-001.java src/main/java/com/demo/user/UserService.java

# 起被测服务（缺陷版）→ 跑流水线 → 断言失败进缺陷清单 + 复测 retained
# 还原主干 → 再跑 retest（断点续跑/force-node）→ closed_suggested + 归档
git checkout -q main
```

> 说明：演示仓库本体是普通目录（避免嵌套 git 仓库），验收演示按上述脚本现场
> `git init` 建立分支语义；缺陷变体文件独立存放于 `defects/`，主干源码不含缺陷。
