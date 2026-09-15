# 联调证据物：job-demo-defect-0004（G1/G5 判定依据）

| 项 | 内容 |
|---|---|
| 产生时间 | 2026-09-15 |
| 复现命令 | `orchestrator run --job-id job-demo-defect-0004 --doc deploy/golden/requirements.md --repos <绝对路径 repos 配置> --rest-url http://127.0.0.1:9000`（服务依赖见下） |
| LLM 模式 | 本地 OpenAI 兼容桩（`deploy/llm-stub/stub_server.py`，docs/13 C-04）——**4 个语义节点的 LLM 输出为桩产生**，其余全真实（真 CLI / 真 java-parser / 真被测服务执行 / 真缺陷闭环） |
| 环境依赖 | java-parser :8080；user-service :9000（DEF-001 缺陷版）；LLM 桩 :8090 |

## 两幕操作（AC3 缺陷闭环演示）

### 第一幕：埋入缺陷 → discovered → retained

1. `deploy/demo-repos/user-service/defects/UserService.DEF-001.java` 覆盖主干 `UserService.java`（age+1），重启 user-service（重置内存数据，见 docs/13 P-09）；
2. 单命令跑 9 节点（新 job_id——java-parser 幂等键下失败终态黏滞，见 P-09）；
3. 结果：18 用例 = 11 通过 + 1 断言失败 + 6 环境失败（order-center 服务未部署，X-EXEC-01 同类如实分列，不计缺陷——R-EXEC-03/R-DEF-01）；
4. 缺陷清单：`DEF-001 retained`（TC-009：HTTP 200 语义断言失败 age=29（期望 28））。

### 第二幕：还原主干 → retest → closed_suggested + 回归归档

1. 还原主干 `UserService.java`，重启 user-service；
2. `orchestrator run --job-id job-demo-defect-0004 ... --force-node retest_regression`（旧 `regression_index.json` 移入 `archive/retest_regression/`，留痕红线）；
3. 结果：`DEF-001 closed_suggested`（历史：discovered → retest_failed → retained → retest_passed → close_suggested，R-DEF-03 可审计）；
4. 回归归档：`scripts/regression/rest/test_TC_009.py` + `regression_index.json` 溯源（REQ-001 / IF-REST-005 / archived_at）。

## G1–G5 判定对照

| 目标 | 本证据物支持 |
|---|---|
| G1 一条命令跑通 | 9/9 节点单命令完成；断言二态明确（assertion/environment 分列） |
| G2 golden 比对 | `check_golden.py` 退出码 0（5/5 条目命中预置基准） |
| G3 ≥2 仓库 | `code_inventory.json`：user-service（7 文件）+ order-center（1 文件） |
| G5 缺陷闭环 | 两幕完整生命周期 + 回归归档（见上） |

> metrics.json 的 `data_complete=false` 为设计行为（X-MTR-02：报告时点快照，下游节点未到达不输出误导数值）。
> 真实 GLM-5.3 网关到位后仅换 `LLM_BASE_URL` 重跑并替换本目录（docs/13 待办 C-04）。
