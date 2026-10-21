# Mock 服务（HTTP/JSON）

当前实现提供一个确定性 REST/JSON 下游 mock，优先支持 Code Review 识别出的外部 Service/O32 响应分支。服务按配置的 HTTP 方法、路径和请求选择条件返回指定状态码、响应头及 JSON 正文；未匹配时返回 404，多条同优先级且同具体度的配置同时匹配时返回 409，避免静默选择错误响应。

## 启动

`mock-services` 已纳入默认 Compose 编排，编排会等待 `/health` 就绪后再启动 orchestrator。端口只绑定宿主机回环地址：

```sh
docker compose -f deploy/docker-compose.yml up -d --build mock-services
```

宿主机可访问 `http://127.0.0.1:8082`；同一 Compose 网络中的被测服务和 orchestrator 使用 `http://mock-services:8082`。管理 API 仅供本地/内部测试网络使用，不能直接暴露到公网。

本机单独启动时，先在 `services/mock-service/` 执行 `uv sync --frozen`，再从仓库根目录执行 `uv run --project services/mock-service python -m mock_service`。本机进程默认只绑定 `127.0.0.1`。

## 安装一个下游场景

`PUT /__admin/scenarios/{name}` 安装或替换场景。请求字段如下：

```json
{
  "method": "POST",
  "path": "/risk/o32/trial",
  "match": {
    "body": { "serialNo": "mock-case-001" }
  },
  "priority": 10,
  "response": {
    "status_code": 200,
    "headers": { "Content-Type": "application/json" },
    "body": { "body": { "errorCode": "0", "riskOutList": [] } }
  }
}
```

`match.query`、`match.headers` 和 `match.body` 都是可选的。对象按递归子集匹配，配置的字段都必须相等；数组和标量按完整值匹配。优先级较高的场景先匹配；优先级相同则选择条件更多的场景。仍有并列时返回 409，并列出冲突名称。`GET /__admin/scenarios` 可查看场景命中次数，但不会返回配置的响应正文。

删除场景使用 `DELETE /__admin/scenarios/{name}`。删除不存在的场景也返回 2xx，适合 setup/teardown 清理重试。

## 执行器 fixture 配置

分支控制 fixture 的 `base_url` 指向 mock 服务，setup 使用 `PUT` 安装场景，teardown 使用 `DELETE` 删除同名场景。例如：

```json
[
  {
    "name": "o32-business-failure",
    "phase": "setup",
    "method": "PUT",
    "base_url": "http://mock-services:8082",
    "path": "/__admin/scenarios/o32-business-failure",
    "body": {
      "method": "POST",
      "path": "/risk/o32/trial",
      "match": { "body": { "serialNo": "mock-case-001" } },
      "response": { "status_code": 200, "body": { "body": { "errorCode": "MOCK_ERROR", "errorMsg": "mock business failure", "riskOutList": [] } } }
    },
    "expected": "status_class=2xx",
    "required": true,
    "scope": "case",
    "control_kind": "downstream_response",
    "interface_id": "IF-REST-002",
    "scenario_contains": "O32试算失败"
  },
  {
    "name": "o32-business-failure",
    "phase": "teardown",
    "method": "DELETE",
    "base_url": "http://mock-services:8082",
    "path": "/__admin/scenarios/o32-business-failure",
    "expected": "status_class=2xx",
    "required": true,
    "scope": "case",
    "control_kind": "downstream_response",
    "interface_id": "IF-REST-002",
    "scenario_contains": "O32试算失败"
  }
]
```

上面的路径、字段及 fixture 场景文字是示例，需按被测系统实际调用的 O32 URL 和生成的 oracle 场景调整。还必须将被测系统的 O32 下游地址配置为该 mock 的可达地址；只安装响应场景不会自动改写被测服务配置。`body: null` 只表示 HTTP 响应正文为 JSON `null`，不能据此声称 Java 内部 Service 方法返回 null。当前网关的 O32 转换器会把空下游响应转换为错误对象；`StandardRiskTrialServiceImpl` 正常返回路径也会组装新响应。因此内部 Service 的 null 防御分支需使用隔离组件测试，不能靠 HTTP 响应桩冒充触发。响应目前统一使用 JSON 编码，不能模拟零字节响应或连接重置。

## 当前边界

- 场景仅保存在 mock 进程内，重启后清空；每条用例的 fixture setup/teardown 会重新安装并删除场景。
- 当前实现覆盖 HTTP/JSON 下游响应；gRPC、GraphQL、SOAP 和 MQ 适配器尚未实现。
- `application_config` 和 `service_registry` 夹具需对接被测应用自己的配置/注册表测试接口。此 mock 不会修改另一个进程的 Apollo 配置或 JVM 内存缓存。
- 该服务不代理或转发流量。被测服务需显式把对应下游 URL 指向 mock；本机直接访问时使用 `127.0.0.1:8082`，容器内访问时使用 Compose 服务名 `mock-services:8082`。

完整请求/响应和管理接口见 [mock-service.openapi.yaml](../schemas/contracts/mock-service.openapi.yaml)。

任务当前被测地址为远端服务时，远端机器不能通过 `127.0.0.1:8082` 访问开发机上的 mock。需在远端测试网络部署或提供可达地址，并配置 `icm.gateway.o32-executor.url` 指向实际 mock 路径；尚未完成此接线时，相关分支必须保留为待准备环境。并行任务应使用独立场景名称与可区分的请求匹配条件，当前内存场景名称在进程内全局共享。
