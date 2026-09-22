# Prometheus 监控基线

编排服务提供：

- `/health`：进程存活检查，适合容器 liveness；
- `/ready`：数据库就绪检查，失败返回 HTTP 503；
- `/metrics`：固定路由 HTTP 计数、队列 active/queued 和数据库可用性。

`prometheus.yml` 和 `alerts.yml` 是最小可用样例，生产环境应按部门告警渠道调整阈值和通知路由。

压测前先确认 `/ready` 正常，再运行：

```bash
uv run python deploy/load_smoke.py \
  --url http://127.0.0.1:8000 \
  --path /ready \
  --requests 100 \
  --concurrency 10
```

该脚本只调用只读端点，不提交任务、不调用 LLM。业务吞吐压测应在专用测试环境中使用脱敏任务，并单独记录 LLM、数据库和被测服务容量。
