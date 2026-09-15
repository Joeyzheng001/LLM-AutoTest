# 演示仓库组（验收链路物料）

| 仓库 | 角色 | 验收口径 |
|---|---|---|
| `user-service` | 被测主仓库：可运行 Spring Boot REST（端口 9000），6 个 REST 接口 + `/health` 探针；埋入缺陷见 `defects/` | G1 端到端真实执行；G3 真实仓库之一 |
| `order-center` | G3 第二仓库（分母要求 ≥2）：3 个 REST 接口 + `src/main/proto/` gRPC 契约物料 | G3；D2（gRPC 静态识别未接入，如实告警不虚构） |
| `fallback-simple` | 兜底演示仓库（结构极简，1 个接口） | D5：`is_fallback=true` 单独标注、不计入 G3、与真实仓库分列 |

## 验收链路（G1 一条命令）

```bash
export JAVA_HOME=/opt/homebrew/opt/openjdk@21   # 本机 Java 21 路径，按环境调整

# 1) 起依赖：java-parser（:8081）
cd services/java-parser && mvn -q spring-boot:run &

# 2) 起被测服务（:9000）
cd deploy/demo-repos/user-service && mvn -q spring-boot:run &

# 2) 单命令跑全流程（F-ORCH-01）
cd services/orchestrator
export JAVA_PARSER_URL=http://127.0.0.1:8081
export LLM_BASE_URL=<GLM 网关> ARTIFACTS_ROOT=/tmp/llm-autotest SCHEMAS_DIR=../../schemas
uv run python -m orchestrator run \
  --job-id job-demo-0001 \
  --doc ../../deploy/golden/requirements.md \
  --repos ../../deploy/task-config/demo.repos.json \
  --rest-url http://127.0.0.1:9000

# 3) 断点/重跑诊断
uv run python -m orchestrator status --job-id job-demo-0001
```

产物：`/tmp/llm-autotest/job-demo-0001/`（11 个结构化产物 + `scripts/`），
`test_report.md` + `metrics.json` 即 G1/G5 证据物。

## 埋入缺陷（AC3 验收闭环）

见 [user-service/defects/README.md](user-service/defects/README.md)：
业务逻辑层 `age+1` 错误数据 → 断言失败 → 缺陷清单 → retained →
还原主干 → closed_suggested + 回归归档。

## 如实降级（不造假绿灯）

- gRPC（order-center 的 proto）：java-parser 静态识别未接入 → 契约告警；
  流水线侧按 X-EXEC-03 Mock 演示降级，`degraded_protocols` 如实记录；
- 兜底仓库结果分列呈现（D5 红线）。
