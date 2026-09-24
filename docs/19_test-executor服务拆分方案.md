# 19 · test-executor 服务拆分方案

> 状态：方案设计（待评审定稿后实施）
> 关联：docs/12 §二产物写权、docs/08 架构、docs/11 分工任务书
> 背景：单体过渡期——4 个丙泳道节点逻辑当前运行在 orchestrator 进程内，
> test-executor 服务仅存 `result.py` 领域模型骨架。

---

## 1. 现状与问题

### 1.1 现状

流水线 9 节点中，4 个「丙泳道」节点（用例生成 / 执行 / 缺陷 / 回归）的逻辑
全部在 `orchestrator/src/orchestrator/nodes/` 内，由 `runner.py::_invoke_node`
**进程内直接调用**：

| 节点 | 产物（写方） | 当前实现位置 |
|---|---|---|
| testcase_generate | testcases.json（test-executor） | orchestrator/nodes/testcase_generate.py |
| auto_execute | execution.json（test-executor） | orchestrator/nodes/auto_execute.py |
| defect_list | defects.json（test-executor） | orchestrator/nodes/defect_list.py |
| retest_regression | regression_index.json（test-executor） | orchestrator/nodes/retest_regression.py |

orchestrator 进程内用 `te_store = ArtifactStore(root, schemas_dir, "test-executor")`
**模拟** test-executor 的写权身份（`artifacts.py::_WRITE_OWNERS` 已把上述 4 份产物
划归 test-executor）。

而 `services/test-executor` 目前只有 `result.py`（FailureKind / CaseResult /
ExecutionSummary 失败二态领域模型），**没有 FastAPI 应用、没有节点端点、没有节点逻辑**。

### 1.2 问题

1. **职责错位**：丙泳道节点逻辑寄居在甲/乙的 orchestrator 进程里，`result.py`
   与执行逻辑物理分离，服务边界名存实亡。
2. **无法独立伸缩/部署**：执行类节点（尤其 auto_execute 的无人值守脚本运行）
   无法独立扩缩容、独立重启。
3. **拆分成本随体量累积**：节点逻辑与 orchestrator 的 engine/llm 模块已深度交织，
   拖得越久迁移面越大。

---

## 2. 目标架构

```
┌─────────────┐   HTTP（节点执行任务）   ┌──────────────────┐
│ orchestrator │ ──────────────────────▶ │  test-executor    │
│ 甲/乙泳道    │                          │  丙泳道 4 节点    │
│ 5 节点       │ ◀────── 产物卷 ────────▶ │  FastAPI 服务     │
└─────────────┘                          └──────────────────┘
                    ▲ 共享产物卷（同一 artifacts_root，写权二分）
```

- **orchestrator**：保留 5 节点（req_parse / code_scan / interface_reconstruct /
  code_review / test_report），持有 `state.json` 节点状态，负责流水线编排。
- **test-executor**：承载 4 个丙节点（testcase_generate / auto_execute / defect_list /
  retest_regression），暴露节点执行端点，直接写产物卷。
- **协作方式**：orchestrator 编排到丙节点时，向 test-executor 发节点执行任务；
  test-executor 完成后写产物；orchestrator 通过**产物判据**（`open_job` 对账，
  机制已存在）确认节点完成。

---

## 3. 关键设计决策

### 3.1 共享代码抽离（最大工作量项）

4 个丙节点依赖以下 orchestrator 模块，需被 test-executor 复用：

| 模块 | 用途 |
|---|---|
| `llm/client.py` + `llm_repair.py` + `llm_batch.py` | LLM 调用 / JSON 修复 / 分批 |
| `prompts_loader.py` | Prompt 加载 |
| `engine.py` | 节点状态机（check_runnable / mark_running / mark_completed） |
| `artifacts.py` | 产物读写 + 写权校验 + Schema 校验 |
| `errors.py` | 错误码与 PipelineError |
| `case_validation.py` | 用例参数校验 |
| `security.py` | 敏感字段脱敏 / SecretStore |
| `config.py` | 环境变量配置 |

**方案 A（推荐）**：抽 `services/common/` 共享包，把上述模块迁入，orchestrator 与
test-executor 都依赖它（`pip install -e services/common` 或 workspace 依赖）。

**方案 B（过渡）**：test-executor 直接 `from orchestrator.xxx import ...`，把
orchestrator 当库依赖。耦合重、会拉入 FastAPI 等无关依赖，但迁移最快。

> 建议：先 B 快速打通链路（阶段 1），跑稳后择机重构成 A（与 #4 大文件拆分可合并做）。

### 3.2 节点执行 API 契约（对齐 java-parser 的异步 job 模式）

复用 java-parser 已验证的「创建任务 → 轮询终态」模式：

```
POST /api/v1/node-jobs          body: {job_id, node, ...节点参数}
GET  /api/v1/node-jobs/{id}     → {status: queued|running|completed|failed, ...}
```

- `node` 仅允许 4 个丙节点名，表外即拒。
- 节点参数含 `llm` 配置（model/base_url/api_key）、`auth`、`rest_base_url`、`fixtures` 等。
- **凭据安全**：`api_key` / `auth.password` 等敏感字段经 SecretStore 或一次性传输，
  不落日志、不入产物（沿用 `security.redact_secrets` 口径）。

### 3.3 状态同步（产物判据，机制已存在）

- orchestrator 的 `state.json` 仍是节点状态的唯一事实来源。
- test-executor 只负责「执行节点 + 写产物 + 返回结果」，**不维护 state.json**。
- orchestrator 收到 test-executor 的 completed 后，`engine.mark_completed` 更新状态；
  若 test-executor 返回 failed，`engine.mark_failed` 走既有的重试/降级治理。
- 产物判据兜底：即使状态不同步，`open_job` 会按产物重新对账（已实现）。

### 3.4 产物卷共享与写权

- 两个服务挂载**同一个 `artifacts_root`**（本地同目录 / 生产共享卷）。
- 写权二分（`_WRITE_OWNERS`）已防冲突：丙产物只有 test-executor 能写，
  甲/乙产物只有 orchestrator 能写，跨服务写直接报 `X-ORCH-01`。
- test-executor 的 `ArtifactStore(root, schemas_dir, "test-executor")` 与现网
  orchestrator 内 `te_store` 完全一致，Schema 校验规则不变。

---

## 4. 分阶段实施

### 阶段 1：test-executor 服务化 + 节点迁移

1. test-executor `pyproject` 加 FastAPI + uvicorn 依赖；
2. 搭 FastAPI 骨架（`app.py` + `/health` + `/api/v1/node-jobs`）；
3. 迁 4 个节点模块到 test-executor（先方案 B 依赖 orchestrator 包跑通）；
4. 单测：test-executor 侧 4 个节点的单元测试迁移/新增。

### 阶段 2：orchestrator 远程调用改造

1. 新增 `NodeExecutor` 抽象（本地实现 / 远程实现），或直接加 `test_executor_client.py`
   （对齐 `javaparser_client.py` 的异步 job 轮询）；
2. `runner.py::_invoke_node` 对 4 个丙节点改为 HTTP 调用；
3. 保留本地实现作为回退开关（`TEST_EXECUTOR_URL` 为空 → 走本地，便于开发调试）。

### 阶段 3：部署 + 端到端验证

1. `docker-compose.yml` 加 test-executor 服务（挂载 artifacts 共享卷）；
2. `dev_orchestrator.sh` 旁补 `dev_test_executor.sh` 本地启动脚本；
3. 端到端：提交任务 → 全 9 节点跑通，确认丙产物由 test-executor 进程写出。

---

## 5. 风险与注意事项

| 风险 | 说明 | 缓解 |
|---|---|---|
| 共享代码抽离体量大 | 8+ 核心模块依赖重排 | 先方案 B 过渡，择机重构 A |
| 凭据跨服务传输 | LLM key / auth 密码经 HTTP 传递 | SecretStore 或一次性传输 + 脱敏留痕 |
| 产物卷一致性问题 | 双写共享卷需保证原子性 | 沿用 `write` 的 tmp+replace 原子写 |
| 状态不同步 | test-executor 不维护 state | 产物判据 + orchestrator 侧 mark_* 兜底 |
| 本地开发双进程 | 起服务成本 +1 | `dev_test_executor.sh` 脚本固化 |

---

## 6. 待评审确认项

1. 共享代码抽离采用方案 A（抽 common 包）还是方案 B（依赖 orchestrator 包）？
2. 节点执行 API 采用异步 job 轮询（推荐，对齐 java-parser）还是同步阻塞调用？
3. 拆分是否与「#4 大文件拆分」合并排期（共享模块抽离天然是重构窗口）？
