---
module: M1-LLMAutoTest
version: v1.0
status: Approved
created: 2026-09-15
---

# 09-API接口规格

## 元数据

| 字段 | 值 |
|------|-----|
| 模块编号 | M1 |
| 模块名称 | LLMAutoTest |
| 版本 | v1.0 |
| 状态 | Approved |
| 创建日期 | 2026-09-15 |
| 最后更新 | 2026-09-15 |

## 变更记录

| 日期 | 版本 | 变更内容 | 操作人 |
|------|------|----------|--------|
| 2026-09-15 | v1.0 | 初始创建，整合 schemas/contracts/ 与 services/ 骨架 | - |

---

## 一、概述

本文档为对外字段契约真源。所有 REST API 路径、请求/响应结构、错误码在此冻结。

服务划分：
- **orchestrator**：Python FastAPI，流水线编排与产物管理
- **java-parser**：Java Spring Boot，Java 代码扫描与接口逆向还原
- **test-executor**：Python，测试脚本加载与执行
- **frontend**：Vue 3，Web 界面（消费 orchestrator API）

## 二、Orchestrator API

### API-001 健康检查

- **Path**: `GET /health`
- **描述**: 服务健康检查
- **请求**: 无
- **响应**:

```json
{
  "status": "ok",
  "service": "orchestrator",
  "version": "1.0.0"
}
```

- **错误**:
  - `500`: 服务内部错误

```json
{
  "error": {
    "code": "INTERNAL_ERROR",
    "message": "服务内部错误"
  }
}
```

---

### API-002 流水线状态查询

- **Path**: `GET /api/v1/pipelines/{pipeline_id}`
- **描述**: 查询指定流水线的执行状态
- **请求参数**:
  - `pipeline_id` (path, required): 流水线 ID
- **响应**:

```json
{
  "pipeline_id": "pl-001",
  "status": "running",
  "nodes": [
    {
      "node_id": "req_parse",
      "status": "completed",
      "started_at": "2026-09-15T10:00:00Z",
      "completed_at": "2026-09-15T10:01:00Z"
    },
    {
      "node_id": "code_scan",
      "status": "running",
      "started_at": "2026-09-15T10:01:00Z"
    }
  ],
  "created_at": "2026-09-15T10:00:00Z"
}
```

- **错误**:
  - `404`: 流水线不存在

```json
{
  "error": {
    "code": "PIPELINE_NOT_FOUND",
    "message": "流水线 pl-999 不存在"
  }
}
```

---

### API-003 触发流水线执行

- **Path**: `POST /api/v1/pipelines`
- **描述**: 创建并触发流水线执行
- **请求体**:

```json
{
  "config": {
    "repo_paths": ["/path/to/repo1", "/path/to/repo2"],
    "requirement_files": ["/path/to/req.md"],
    "protocols": ["REST", "gRPC"]
  }
}
```

- **响应**:

```json
{
  "pipeline_id": "pl-001",
  "status": "created",
  "created_at": "2026-09-15T10:00:00Z"
}
```

- **错误**:
  - `400`: 请求参数无效

```json
{
  "error": {
    "code": "INVALID_REQUEST",
    "message": "repo_paths 不能为空"
  }
}
```

---

### API-004 节点产物查询

- **Path**: `GET /api/v1/pipelines/{pipeline_id}/nodes/{node_id}/artifacts`
- **描述**: 查询指定节点的产物
- **请求参数**:
  - `pipeline_id` (path, required): 流水线 ID
  - `node_id` (path, required): 节点 ID
- **响应**:

```json
{
  "node_id": "req_parse",
  "artifacts": [
    {
      "name": "requirements.json",
      "path": "/data/llm-autotest/pl-001/req_parse/requirements.json",
      "size_bytes": 1234,
      "created_at": "2026-09-15T10:01:00Z"
    }
  ]
}
```

---

## 三、Java Parser API

### API-005 健康检查

- **Path**: `GET /actuator/health`
- **描述**: Java 解析服务健康检查
- **响应**:

```json
{
  "status": "UP"
}
```

---

### API-006 代码扫描

- **Path**: `POST /api/v1/scan`
- **描述**: 扫描 Java 仓库，输出源码清单
- **请求体**:

```json
{
  "repo_paths": ["/path/to/repo1", "/path/to/repo2"],
  "exclude_patterns": ["**/test/**", "**/generated/**"]
}
```

- **响应**:

```json
{
  "scan_id": "scan-001",
  "repositories": [
    {
      "path": "/path/to/repo1",
      "modules": ["module-a", "module-b"],
      "files": [
        {
          "path": "src/main/java/com/example/Controller.java",
          "size_bytes": 5678
        }
      ]
    }
  ]
}
```

---

### API-007 接口逆向还原

- **Path**: `POST /api/v1/parse-interfaces`
- **描述**: 从 Java 源码中逆向还原接口定义
- **请求体**:

```json
{
  "scan_id": "scan-001",
  "protocols": ["REST", "gRPC"]
}
```

- **响应**:

```json
{
  "parse_id": "parse-001",
  "interfaces": [
    {
      "interface_id": "intf-001",
      "protocol": "REST",
      "path": "/api/users",
      "method": "GET",
      "parameters": [
        {
          "name": "userId",
          "type": "string",
          "in": "path",
          "required": true
        }
      ],
      "response_schema": {
        "type": "object",
        "properties": {
          "id": {"type": "string"},
          "name": {"type": "string"}
        }
      },
      "code_location": {
        "repo": "/path/to/repo1",
        "file": "src/main/java/com/example/UserController.java",
        "line_range": [10, 25]
      }
    }
  ]
}
```

---

## 四、Test Executor API

### API-008 执行测试用例

- **Path**: `POST /api/v1/execute`
- **描述**: 加载并执行测试用例
- **请求体**:

```json
{
  "pipeline_id": "pl-001",
  "test_cases_path": "/data/llm-autotest/pl-001/gen/test_cases/"
}
```

- **响应**:

```json
{
  "execution_id": "exec-001",
  "status": "completed",
  "results": [
    {
      "case_id": "tc-001",
      "status": "passed",
      "duration_ms": 123
    },
    {
      "case_id": "tc-002",
      "status": "failed",
      "duration_ms": 456,
      "failure_reason": "Assertion failed: expected 200, got 404"
    }
  ],
  "summary": {
    "total": 10,
    "passed": 8,
    "failed": 2,
    "pass_rate": 0.8
  }
}
```

---

## 五、错误码总表

| 错误码 | HTTP 状态码 | 说明 |
|--------|------------|------|
| `INTERNAL_ERROR` | 500 | 服务内部错误 |
| `PIPELINE_NOT_FOUND` | 404 | 流水线不存在 |
| `INVALID_REQUEST` | 400 | 请求参数无效 |
| `NODE_NOT_FOUND` | 404 | 节点不存在 |
| `ARTIFACT_NOT_FOUND` | 404 | 产物不存在 |
| `SCAN_FAILED` | 500 | 代码扫描失败 |
| `PARSE_FAILED` | 500 | 接口解析失败 |
| `EXECUTION_FAILED` | 500 | 测试执行失败 |

## 六、示例请求/响应

### 完整流水线执行示例

**请求**:
```bash
curl -X POST http://localhost:8000/api/v1/pipelines \
  -H "Content-Type: application/json" \
  -d '{
    "config": {
      "repo_paths": ["/data/repos/repo1", "/data/repos/repo2"],
      "requirement_files": ["/data/reqs/requirements.md"],
      "protocols": ["REST"]
    }
  }'
```

**响应**:
```json
{
  "pipeline_id": "pl-001",
  "status": "created",
  "created_at": "2026-09-15T10:00:00Z"
}
```

**查询状态**:
```bash
curl http://localhost:8000/api/v1/pipelines/pl-001
```

**响应**:
```json
{
  "pipeline_id": "pl-001",
  "status": "completed",
  "nodes": [
    {"node_id": "req_parse", "status": "completed"},
    {"node_id": "code_scan", "status": "completed"},
    {"node_id": "interface_parse", "status": "completed"},
    {"node_id": "code_review", "status": "completed"},
    {"node_id": "test_gen", "status": "completed"},
    {"node_id": "test_exec", "status": "completed"},
    {"node_id": "report", "status": "completed"},
    {"node_id": "defect", "status": "completed"},
    {"node_id": "regression", "status": "completed"}
  ]
}
```
