#!/usr/bin/env python3
"""本地 OpenAI 兼容桩（C-04：无独立 LLM 网关时的全链路联调驱动）。

定位：POST /chat/completions 兼容响应（choices/usage 结构与 GLM 网关一致）；
按 system prompt 标记路由到 4 个语义节点之一，动态解析 user_content（各节点
均传 JSON）生成对齐响应——除 LLM 语义内容外全链路真实（真 CLI/真 java-parser/
真被测服务执行/真缺陷闭环）。

诚实性口径：桩产生的语义内容与真实 GLM 有别，联调产物中语义部分按「桩产生」
对待（docs/13 C-04）；真实网关到位后仅换 LLM_BASE_URL 重跑。
"""

from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8090

# 需求条目（与 deploy/golden/golden.expected.json 对齐：5 条）
_REQ_ITEMS = [
    {"id": "REQ-001", "title": "用户查询", "description": "按用户 ID 查询单个用户，返回完整信息（编号、姓名、邮箱、年龄）", "priority": "P1"},
    {"id": "REQ-002", "title": "用户列表过滤", "description": "查询用户列表，可选按最小年龄过滤", "priority": "P2"},
    {"id": "REQ-003", "title": "用户创建", "description": "创建新用户，返回系统分配的用户编号", "priority": "P1"},
    {"id": "REQ-004", "title": "用户信息更新与删除", "description": "按用户 ID 更新与删除用户", "priority": "P2"},
    {"id": "REQ-005", "title": "订单查询", "description": "订单按 ID 查询、按用户过滤、创建订单", "priority": "P1"},
]


def _interface_params(method: str, path: str) -> list[dict[str, object]]:
    """按真实端点语义推导接口参数（与 user-service/order-center 实现对齐）。"""
    params: list[dict[str, object]] = []
    for m in re.findall(r"\{(\w+)\}", path):
        params.append({"name": m, "type": "long", "required": True, "in": "path"})
    if method == "GET" and path == "/api/v1/users":
        params.append({"name": "minAge", "type": "integer", "required": False, "in": "query"})
    if method == "GET" and path == "/api/v1/orders":
        params.append({"name": "userId", "type": "long", "required": False, "in": "query"})
    if method in ("POST", "PUT"):
        body = [
            {"name": "name", "type": "string", "required": True, "in": "body"},
            {"name": "email", "type": "string", "required": True, "in": "body"},
            {"name": "age", "type": "integer", "required": True, "in": "body"},
        ] if path.startswith("/api/v1/users") else [
            {"name": "userId", "type": "long", "required": True, "in": "body"},
            {"name": "amount", "type": "number", "required": True, "in": "body"},
        ]
        params.extend(body)
    return params


def _summary(method: str, path: str) -> str:
    return {"GET": "查询", "POST": "创建", "PUT": "更新", "DELETE": "删除", "PATCH": "部分更新"}.get(method, "调用") + f" {path}"


def _resp_interface_reconstruct(user_content: str) -> str:
    """候选清单 → 接口语义定义（params 与真实服务端点对齐）。"""
    candidates = json.loads(user_content)
    interfaces = []
    for cand in candidates:
        sig = str(cand.get("signature", "")).strip()
        method, _, path = sig.partition(" ")
        interfaces.append({
            "candidate_id": cand.get("candidate_id"),
            "protocol": cand.get("protocol", "rest"),
            "signature": {"method": method, "path": path, "summary": _summary(method, path)},
            "params": _interface_params(method, path),
        })
    return json.dumps({"interfaces": interfaces}, ensure_ascii=False)


def _resp_code_review(user_content: str) -> str:
    """需求 × 接口 → 映射（按路径语义配对，全部真实实现）。"""
    data = json.loads(user_content)
    interfaces = data.get("interfaces", [])
    pairs = [
        ("REQ-001", "/api/v1/users/{id}"),
        ("REQ-002", "/api/v1/users"),
        ("REQ-003", "/api/v1/users"),
        ("REQ-004", "/api/v1/users/{id}"),
        ("REQ-005", "/api/v1/orders"),
    ]
    by_path = {}
    for itf in interfaces:
        path = (itf.get("signature") or {}).get("path", "")
        by_path.setdefault(path, itf.get("id"))
    mappings = []
    for req_id, path in pairs:
        if_id = by_path.get(path)
        if if_id:
            mappings.append({
                "requirement_id": req_id,
                "interface_id": if_id,
                "status": "implemented",
                "evidence": {"note": f"路径命中 {path}（静态候选 + 语义一致）"},
            })
    return json.dumps({"mappings": mappings}, ensure_ascii=False)


def _case_params(interface: dict[str, object], category: str) -> dict[str, object]:
    """按接口参数与类目生成用例参数（R-GEN-02 参数一致性由节点校验，须对齐）。"""
    params: dict[str, object] = {}
    for p in interface.get("params") or []:
        name = str(p.get("name"))
        ptype = str(p.get("type"))
        if category == "normal":
            value: object = {"long": 1, "integer": 30, "number": 100.0}.get(ptype, "stub-value")
        else:  # boundary
            value = {"long": 999999, "integer": 200, "number": 999999.0}.get(ptype, "x" * 200)
        params[name] = value
    return params


def _expected(interface: dict[str, object], category: str) -> str:
    """expected 约定（docs/13 C-05）：「字段=值」等值对 + 描述。"""
    method = (interface.get("signature") or {}).get("method", "GET")
    path = (interface.get("signature") or {}).get("path", "")
    if category == "normal" and method == "GET" and path == "/api/v1/users/{id}":
        return "id=1;name=alice;age=28；按 ID 查询返回完整用户"
    if category == "normal" and method == "GET" and path == "/api/v1/users":
        return "列表非空；minAge 过滤后年龄均不低于阈值"
    if category == "normal" and method == "POST":
        return "返回系统分配的编号（id>0）；2xx"
    if category == "boundary" and method in ("GET", "PUT") and path.endswith("{id}"):
        # C-05 扩展：status token——不存在资源预期 400（业务码非 404；
        # DELETE 幂等删除 204 亦合法，走默认 2xx 判定）
        return "status=400；不存在资源返回业务错误码"
    return "2xx 响应结构符合接口定义"


def _resp_testcase_generate(user_content: str) -> str:
    """接口 + 需求 + 映射 → 用例（normal/boundary 各一；exception 类目因演示态
    断言将 400 记 assertion 失败、会污染缺陷闭环演示而不生成——节点未强制三类
    全覆盖，如实口径见 docs/13 C-05）。"""
    data = json.loads(user_content)
    interfaces = data.get("interfaces", [])
    mappings = data.get("mappings", [])
    req_by_if: dict[str, str] = {}
    for m in mappings:
        req_by_if.setdefault(str(m.get("interface_id")), str(m.get("requirement_id")))
    cases = []
    for itf in interfaces:
        if_id = str(itf.get("id"))
        for category in ("normal", "boundary"):
            cases.append({
                "interface_id": if_id,
                "requirement_id": req_by_if.get(if_id),
                "category": category,
                "title": f"{_summary((itf.get('signature') or {}).get('method', 'GET'), (itf.get('signature') or {}).get('path', ''))}——{category} 用例",
                "expected": _expected(itf, category),
                "params": _case_params(itf, category),
            })
    return json.dumps({"cases": cases}, ensure_ascii=False)


def _route_and_respond(system: str, user_content: str) -> str:
    """按 system prompt 标记路由（各节点输出结构键唯一）。"""
    if '"mappings"' in system:
        return _resp_code_review(user_content)
    if '"cases"' in system:
        return _resp_testcase_generate(user_content)
    if '"interfaces"' in system:
        return _resp_interface_reconstruct(user_content)
    if '"items"' in system:
        return json.dumps({"items": _REQ_ITEMS}, ensure_ascii=False)
    raise ValueError(f"无法路由的 system prompt（前 80 字）：{system[:80]}")


class Handler(BaseHTTPRequestHandler):
    """OpenAI 兼容 /chat/completions。"""

    def do_POST(self) -> None:  # noqa: N802 —— http.server 约定
        # OpenAI 兼容：base_url 带 /v1 前缀（…/v1）或不带均受理
        if self.path not in ("/chat/completions", "/v1/chat/completions"):
            self._json(404, {"error": {"message": f"unknown path {self.path}"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages", [])
        system = str(messages[0].get("content", "")) if messages else ""
        user_content = str(messages[1].get("content", "")) if len(messages) > 1 else ""
        try:
            content = _route_and_respond(system, user_content)
        except Exception as exc:  # noqa: BLE001 —— 桩把失败如实返回给客户端
            self._json(500, {"error": {"message": f"stub routing failed: {exc}"}})
            return
        self._json(200, {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"total_tokens": 128},
        })

    def _json(self, status: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[llm-stub] {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"[llm-stub] listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
