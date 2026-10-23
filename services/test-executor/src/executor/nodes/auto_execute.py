"""auto_execute 节点：加载 scripts/ 用例 → 按协议执行 → execution.json（F-EXEC-02~05）。

归属（docs/12 产物布局）：EXEC 节点属丙（test-executor），`execution.json` 丙写——
本函数的 `store` 参数须传入 **test-executor 身份** 的 ArtifactStore。

核心口径：
- R-EXEC-01：按协议路由 Runner，结果统一收敛 execution.json；
- R-EXEC-02：环境探测优先于执行（REST 探活；探测失败该协议用例记环境失败，X-EXEC-01）；
- R-EXEC-03：失败二态——assertion（断言失败，进缺陷清单）与 environment
  （环境/连接失败，不判缺陷，X-EXEC-01/02 同类）；
- X-EXEC-03：协议 Runner 未就绪 → 降级「脚本生成成功 + Mock 执行演示」，
  degraded_protocols 如实记录（报告分列的数据基础）；
- X-EXEC-02：超时记失败（归 environment 类）+ timeout 标记；
- R-GEN-03 特例（docs/12）：loadable=false 用例跳过执行并计数（summary.skipped），
  不静默、不阻塞；
- F-GEN-06：oracle_status=unverified 的用例不发起请求，结果标成待源码确认并跳过，
  避免把没有代码依据的预期计入缺陷；
- 完成判据（D4）：允许有失败用例——失败二态明确即 completed。

断言口径：状态码断言 + 受限 JSON 路径业务断言；缺少字段级断言的用例会明确标成
`status_only`，不宣称已经验证业务结果。Mock 演示 Runner 返回固定桩响应。
"""

from __future__ import annotations

from common.llm_repair import SupportsComplete
from uuid import uuid4

import asyncio
import base64
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol, cast
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlsplit, urlunsplit

import httpx
from common.artifacts import ArtifactStore
from common.case_validation import (
    blocking_parameter_messages,
    is_runtime_injected_param,
    materialize_declared_defaults,
)
from common.engine import Engine, JobState
from common.errors import X_ORCH_01, PipelineError
from common.java_response_paths import response_path_observation
from common.response_assertions import (
    evaluate_response_assertions,
    legacy_response_assertions,
    normalize_response_assertions,
    response_assertion_scope,
)
from common.oracle_conditions import execution_blockers, required_control_kinds as required_control_kinds_for_oracle
from common.security import redact_secrets
from executor.unit_coverage import run_controlled_unit_coverage
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

NODE_NAME = "auto_execute"

_DEFAULT_TIMEOUT_S = 10.0  # X-EXEC-02：单用例执行超时上限
# C-15：不同项目的登录契约并不统一。以下是“自动识别”模式允许的、
# 明确列举的常见凭据位置；不做无边界递归搜索，避免把普通业务字段误当作凭据。
_DEFAULT_TOKEN_PATHS = (
    "token",
    "access_token",
    "accessToken",
    "key",
    "jwt",
    "id_token",
    "idToken",
    "data.token",
    "data.access_token",
    "data.accessToken",
    "data.key",
    "data.jwt",
    "data.id_token",
    "data.idToken",
    "body.token",
    "body.access_token",
    "body.accessToken",
    "body.key",
    "body.jwt",
    "body.id_token",
    "body.idToken",
    "result.token",
    "result.access_token",
    "result.accessToken",
    "result.key",
)


def _value_at_path(payload: dict[str, Any], path: str) -> str | None:
    """从受控 dotted path 读取非空标量，兼容包装层内嵌 JSON；不记录响应值。

    数值型字段（如登录响应的 userId=1）需能作为请求头值注入自定义鉴权头
    （token/userId 等）；故字符串以外的 int/float 也转为字符串返回。bool 虽
    是 int 子类，但 "True"/"False" 不是合法头值，显式排除。
    """
    current: Any = payload
    for segment in path.split("."):
        # 某些网关以字符串承载 body JSON，例如 {"body":"{...}"}。
        # 仅在还要继续向下取字段时解码，纯字符串 body 可由用户显式填 token_path=body。
        if isinstance(current, str):
            try:
                current = json.loads(current)
            except json.JSONDecodeError:
                return None
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
    if isinstance(current, str):
        return current.strip() or None
    if isinstance(current, (int, float)) and not isinstance(current, bool):
        return str(current)
    return None


def _authorization_value(token: str, scheme: str) -> str:
    """按配置构造注入值；raw 适配已有网关/自定义鉴权头。"""
    return token if scheme == "raw" else f"{scheme} {token}"


def _transform_login_password(password: str, transform: str) -> str:
    """按显式登录契约转换密码，不记录明文或转换结果。

    base64_shift_2 用于部分历史 Web 登录页的轻量传输混淆：
    UTF-8 Base64 后每个 ASCII 字符码加 2。它不是加密，仅为兼容被测系统协议。
    """
    if transform == "plain":
        return password
    if transform == "base64_shift_2":
        encoded = base64.b64encode(password.encode("utf-8")).decode("ascii")
        return "".join(chr(ord(char) + 2) for char in encoded)
    raise PipelineError(
        code="X-EXEC-01",
        message=f"鉴权配置无效（C-15）：不支持的密码转换方式 {transform}",
        node_id=NODE_NAME,
    )


def _safe_response_shape(payload: Any) -> str:
    """仅返回顶层字段名，辅助配置 Token 路径且不泄漏业务正文/令牌。"""
    if not isinstance(payload, dict):
        return "非 JSON 对象"
    keys = sorted(str(key) for key in payload)[:12]
    if not keys:
        return "空对象"
    body = payload.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = None
    if isinstance(body, dict):
        nested = "、".join(sorted(str(key) for key in body)[:8]) or "空对象"
        return f"{'、'.join(keys)}（body：{nested}）"
    return "、".join(keys)


def _mapped_response_headers(
    payload: dict[str, Any] | None, mappings: Any
) -> tuple[dict[str, str] | None, list[str]]:
    """将响应 dotted path 显式映射为后续请求头，缺失项只返回路径而不泄漏正文。"""
    if not isinstance(mappings, dict) or not mappings:
        return None, []
    if not isinstance(payload, dict):
        return None, [str(path) for path in mappings.values()]
    headers: dict[str, str] = {}
    missing: list[str] = []
    for header, path in mappings.items():
        if not isinstance(header, str) or not isinstance(path, str):
            missing.append(str(path))
            continue
        value = _value_at_path(payload, path)
        if value is None:
            missing.append(path)
        else:
            headers[header] = value
    return (headers if not missing else None), missing


def _decode_unknown_body(value: Any) -> Any:
    """将 LLM 输出的 unknown body JSON 字符串还原为对象；非 JSON 原样保留。"""
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    return parsed if isinstance(parsed, dict) else value


def _find_auth_user_id(value: Any) -> str | None:
    """从用例参数中取服务端鉴权过滤器要求的 userId 请求头值。"""
    if isinstance(value, dict):
        user_id = value.get("userId")
        if user_id is not None and str(user_id).strip():
            return str(user_id)
        for child in value.values():
            found = _find_auth_user_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_auth_user_id(child)
            if found:
                return found
    return None


@dataclass(frozen=True)
class InvokeRequest:
    """单用例执行请求：接口定义（签名/参数位置）+ 用例参数。"""

    interface: dict[str, Any]
    params: dict[str, Any]
    # inherit：使用任务登录会话；missing：不带认证；invalid：明确伪造认证。
    auth_mode: str = "inherit"
    body_encoding: str | None = None
    timeout_s: float = _DEFAULT_TIMEOUT_S


@dataclass
class InvokeResponse:
    """Runner 统一响应：调用层结果（断言判定在节点侧）。"""

    ok: bool  # 调用层成功（拿到完整响应）
    status: int | None
    body: Any
    error: str | None  # 环境类错误描述（连接失败/超时）
    timed_out: bool
    duration_ms: int
    request_info: dict[str, Any] | None = None


class Runner(Protocol):
    """协议执行器（REST 为真实 HTTP 调用；Mock 演示为本地桩）。"""

    async def invoke(self, request: InvokeRequest) -> InvokeResponse:
        """执行一次调用。"""
        ...


class RestRunner:
    """REST 执行器：按 signature.method/path 与参数位置（query/body/path/header）调用。

    C-15：auth_headers 非空时注入登录凭据头（Authorization/Cookie）；登录接口
    自身位于 auth_exempt_paths 时不注入预登录凭据；
    verify=False 用于 https 自签场景（表单「跳过证书校验」）；
    P-12：Django APPEND_SLASH 补偿——无尾斜杠路径被 301，或被 Nginx 前置层
    直接拒为 405 时，均向同路径加 "/" 同 method 重发一次。
    """

    def __init__(
        self,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        auth_headers: dict[str, str] | None = None,
        verify: bool = True,
        auth_exempt_paths: set[str] | None = None,
        prefer_trailing_slash: bool = False,
    ) -> None:
        """transport 仅测试注入，生产留 None。"""
        self._base_url = base_url
        self._transport = transport
        self._auth_headers = auth_headers or {}
        self._verify = verify
        self._auth_exempt_paths = {path.rstrip("/") or "/" for path in auth_exempt_paths or set()}
        self._prefer_trailing_slash = prefer_trailing_slash

    def replace_auth_headers(self, auth_headers: dict[str, str]) -> None:
        """更换后续请求使用的会话凭据（例如登出用例撤销了旧 token）。"""
        self._auth_headers = auth_headers

    async def invoke(self, request: InvokeRequest) -> InvokeResponse:
        """按接口签名拼请求并调用；连接/超时归环境类错误。"""
        method, path, query, body, body_supplied, headers = self._request_parts(request)
        request_info: dict[str, Any] | None = None
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=request.timeout_s,
                transport=self._transport,
                verify=self._verify,
            ) as client:
                path_key = path.rstrip("/") or "/"
                inherited_headers = (
                    {} if path_key in self._auth_exempt_paths else self._auth_headers
                )
                if request.auth_mode == "missing":
                    inherited_headers = {}
                elif request.auth_mode == "invalid":
                    # 当前服务的 TokenFilter 读取 token/userId，而不是 Authorization。
                    inherited_headers = {
                        "token": "invalid-autotest-token",
                        "userId": _find_auth_user_id(request.params)
                        or "autotest-invalid-user",
                    }
                merged = {**inherited_headers, **headers}
                wire_body: dict[str, Any]
                if request.body_encoding == "json_null":
                    merged["content-type"] = "application/json"
                    wire_body = {"content": b"null"}
                else:
                    wire_body = {"json": body if body_supplied else None}
                outgoing = client.build_request(
                    method,
                    path,
                    params=query or None,
                    **wire_body,
                    headers=merged or None,
                )
                request_info = _request_info(
                    outgoing,
                    query=query,
                    body=body if body_supplied else None,
                    attempted=True,
                    sensitive_path_values=self._sensitive_path_values(request),
                )
                if request.body_encoding:
                    request_info["body_encoding"] = request.body_encoding
                response = await client.send(outgoing)
                # P-12：Django APPEND_SLASH 301 补偿（同 method 重发一次）
                location = response.headers.get("location", "")
                if location and response.status_code == 301:
                    # Location 可能是绝对 URL（Django 默认）——只取路径部分比对
                    redirect_path = httpx.URL(location).path
                    if redirect_path.rstrip("/") == path.rstrip("/"):
                        outgoing = client.build_request(
                            method,
                            redirect_path,
                            params=query or None,
                            **wire_body,
                            headers=merged or None,
                        )
                        request_info = _request_info(
                            outgoing,
                            query=query,
                            body=body if body_supplied else None,
                            attempted=True,
                            sensitive_path_values=self._sensitive_path_values(request),
                        )
                        if request.body_encoding:
                            request_info["body_encoding"] = request.body_encoding
                        response = await client.send(outgoing)
                # 部分 Django 部署由 Nginx 代理，POST 无尾斜杠不会收到 APPEND_SLASH
                # 的 301，而是被前置层直接 405。只对非根路径做一次同路径斜杠补偿。
                if response.status_code == 405 and path != "/" and not path.endswith("/"):
                    outgoing = client.build_request(
                        method,
                        f"{path}/",
                        params=query or None,
                        **wire_body,
                        headers=merged or None,
                    )
                    request_info = _request_info(
                        outgoing,
                        query=query,
                        body=body if body_supplied else None,
                        attempted=True,
                        sensitive_path_values=self._sensitive_path_values(request),
                    )
                    if request.body_encoding:
                        request_info["body_encoding"] = request.body_encoding
                    response = await client.send(outgoing)
        except httpx.TimeoutException as exc:
            return InvokeResponse(
                ok=False,
                status=None,
                body=None,
                error=f"执行超时（X-EXEC-02）：{exc}",
                timed_out=True,
                duration_ms=_elapsed_ms(started),
                request_info=request_info,
            )
        except httpx.TransportError as exc:
            return InvokeResponse(
                ok=False,
                status=None,
                body=None,
                error=f"连接失败（X-EXEC-01）：{exc}",
                timed_out=False,
                duration_ms=_elapsed_ms(started),
                request_info=request_info,
            )
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text
        return InvokeResponse(
            ok=True,
            status=response.status_code,
            body=payload,
            error=None,
            timed_out=False,
            duration_ms=_elapsed_ms(started),
            request_info=request_info,
        )

    def preview(self, request: InvokeRequest) -> dict[str, Any]:
        """生成不发送请求的脱敏预览，用于探测失败等未进入调用阶段的用例。"""
        method, path, query, body, body_supplied, headers = self._request_parts(request)
        path_key = path.rstrip("/") or "/"
        inherited_headers = {} if path_key in self._auth_exempt_paths else self._auth_headers
        if request.auth_mode == "missing":
            inherited_headers = {}
        elif request.auth_mode == "invalid":
            inherited_headers = {
                "token": "invalid-autotest-token",
                "userId": _find_auth_user_id(request.params) or "autotest-invalid-user",
            }
        merged = {**inherited_headers, **headers}
        base_url = httpx.URL(self._base_url)
        if not base_url.path.endswith("/"):
            base_url = base_url.copy_with(path=f"{base_url.path}/")
        url = base_url.join(path)
        if query:
            url = url.copy_merge_params(query)
        preview_headers = dict(merged)
        if body_supplied:
            preview_headers.setdefault("content-type", "application/json")
        if request.body_encoding == "json_null":
            preview_headers["content-type"] = "application/json"
        info = _request_info_values(
            method=method,
            url=str(url),
            headers=preview_headers,
            query=query,
            body=body if body_supplied else None,
            attempted=False,
            sensitive_path_values=self._sensitive_path_values(request),
        )
        if request.body_encoding:
            info["body_encoding"] = request.body_encoding
        return info

    def _sensitive_path_values(self, request: InvokeRequest) -> list[str]:
        param_defs = {
            str(item.get("name")): item for item in request.interface.get("params") or []
        }
        return [
            str(value)
            for name, value in request.params.items()
            if value is not None
            and _is_sensitive_key(name)
            and str((param_defs.get(name) or {}).get("in", "query")) == "path"
        ]

    def _request_parts(
        self, request: InvokeRequest
    ) -> tuple[str, str, dict[str, Any], Any, bool, dict[str, str]]:
        """按接口参数位置构造真实请求各部分，供预览和发送共用。"""
        signature = request.interface.get("signature") or {}
        method = str(signature.get("method", "GET")).upper()
        path = str(signature.get("path", "/"))
        if self._prefer_trailing_slash and path != "/" and not path.endswith("/"):
            path = f"{path}/"
        param_defs = {str(p.get("name")): p for p in request.interface.get("params") or []}
        query: dict[str, Any] = {}
        body: Any = {}
        body_supplied = False
        headers: dict[str, str] = {}
        location = request.interface.get("code_location") or {}
        single_java_body = (
            str(location.get("file", "")).endswith(".java")
            and sum(1 for item in param_defs.values() if item.get("in") == "body") == 1
        )
        for name, value in request.params.items():
            param_def = param_defs.get(name) or {}
            # 框架运行时注入的容器参数不是请求参数：`in=unknown` 会落到下面的 else 分支
            # 变成 query，实测会把 `response=unknown` 发给被测服务。直接跳过。
            if is_runtime_injected_param(param_def):
                continue
            position = str(param_def.get("in", "query"))
            # 扫描器无法展开 serializer 时，会留下 account_data/filter_params 等
            # unknown 包装参数。LLM 可能给对象或 JSON 字符串，均应展开真实请求体。
            decoded_value = _decode_unknown_body(value)
            unwrap_container = (
                str(param_def.get("type")) == "unknown" and isinstance(decoded_value, dict)
            )
            unwrap_body_dto = (
                isinstance(decoded_value, dict)
                and isinstance(param_def.get("fields"), list)
                and bool(param_def["fields"])
            )
            if position == "body":
                # 区分「没有 body 参数」和显式传入空对象 {}；后者必须发出 JSON 请求体。
                body_supplied = True
                # Java @RequestBody 的形参名不是 JSON 根字段；即使 JSONObject
                # 没有可枚举的 DTO fields，也必须发送这个形参的原始 JSON 值。
                if single_java_body:
                    body = decoded_value
                    continue
                # Java @RequestBody DTO 的参数名只是方法形参名，JSON 根节点应是 DTO 字段。
                if unwrap_container or unwrap_body_dto:
                    body.update(decoded_value)
                else:
                    body[name] = value
            elif position == "header":
                headers[name] = str(value)
            elif position == "path":
                # 联调发现：path 参数须替换路径模板，否则字面 {id} 进 URL 真实服务必 404
                path = path.replace("{" + name + "}", str(value))
            elif position == "query" and unwrap_container:
                # 与 unknown body 同理：query 容器里的真实键值必须展开，不能把
                # query={...} 作为单个参数发送，否则服务端看不到 fund_code 等字段。
                query.update(decoded_value)
            else:
                query[name] = value
        return method, path, query, body, body_supplied, headers


def _request_info(
    request: httpx.Request,
    *,
    query: dict[str, Any],
    body: Any,
    attempted: bool,
    sensitive_path_values: list[str],
) -> dict[str, Any]:
    """Capture a sanitized copy of the wire request for failure diagnosis."""
    return _request_info_values(
        method=request.method,
        url=str(request.url),
        headers=dict(request.headers),
        query=query,
        body=body,
        attempted=attempted,
        sensitive_path_values=sensitive_path_values,
    )


def _request_info_values(
    *,
    method: str,
    url: str,
    headers: dict[str, Any],
    query: Any,
    body: Any,
    attempted: bool,
    sensitive_path_values: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "method": method,
        "url": _safe_request_url(url, sensitive_path_values or []),
        "headers": _safe_request_value(headers),
        "query_params": _safe_request_value(query),
        "body": _safe_request_value(body),
        "attempted": attempted,
    }


def _safe_request_url(value: str, sensitive_path_values: list[str]) -> str:
    """Mask credentials and sensitive query values while preserving useful URL detail."""
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        if parsed.port:
            host = f"{host}:{parsed.port}"
    except ValueError:
        pass
    path = parsed.path
    for item in sensitive_path_values:
        if item:
            segments = path.split("/")
            encoded = quote(item, safe="")
            for index, segment in enumerate(segments):
                if unquote(segment) == item:
                    segments[index] = "<redacted>"
                elif len(item) >= 6:
                    segments[index] = re.sub(
                        re.escape(encoded), "<redacted>", segment, flags=re.IGNORECASE
                    )
            path = "/".join(segments)
    query = urlencode(
        [
            (key, "<redacted>" if _is_sensitive_key(key) else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, host, path, query, ""))


def _is_sensitive_key(value: Any) -> bool:
    key = re.sub(r"[^a-z0-9]", "", str(value).lower())
    return any(
        marker in key
        for marker in (
            "token",
            "password",
            "passwd",
            "secret",
            "authorization",
            "cookie",
            "apikey",
            "accesskey",
            "privatekey",
            "sessionkey",
        )
    ) or key in {"auth", "credential", "credentials"} or key.endswith("auth")


def _safe_request_value(value: Any, depth: int = 0) -> Any:
    """Bound request detail size and mask secret-like keys before it enters artifacts."""
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if _is_sensitive_key(key)
                else _safe_request_value(item, depth + 1)
            )
            for key, item in list(value.items())[:40]
        }
    if isinstance(value, (list, tuple)):
        return [_safe_request_value(item, depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return value[:2000] + ("…" if len(value) > 2000 else "")
    return value


def _response_assertion_results(
    expected: str, body: Any, assertions: Any = None, request_params: Any = None
) -> list[dict[str, Any]]:
    """Evaluate explicit response assertions, falling back to legacy field=value syntax."""
    try:
        normalized = normalize_response_assertions(assertions) if assertions else []
    except ValueError as exc:
        _ = exc
        return [{"path": "", "op": "invalid", "found": False, "passed": False}]
    if not normalized:
        normalized = legacy_response_assertions(expected)
    results = evaluate_response_assertions(body, normalized, request_params)
    for result in results:
        for key in ("actual", "expected"):
            if key in result:
                result[key] = _safe_actual_body(result[key])
        path = str(result.get("path", "")).lower()
        if any(marker in path for marker in ("token", "password", "secret", "authorization", "cookie")):
            if "actual" in result:
                result["actual"] = "<redacted>"
            if "expected" in result:
                result["expected"] = "<redacted>"
    return results


def _semantic_mismatches(
    expected: str, body: Any, assertions: Any = None, request_params: Any = None
) -> list[str]:
    """Return failed response-path assertions; old field=value expectations remain supported."""
    return [
        (
            f"{item.get('path')}: {item.get('op')} 断言失败"
            + (f"（期望 {item.get('expected')}）" if "expected" in item else "")
            + (f"，实际 {item.get('actual')}" if item.get("found") else "，字段不存在")
        )
        for item in _response_assertion_results(expected, body, assertions, request_params)
        if item.get("passed") is not True
    ]


def _expected_statuses(expected: str) -> tuple[set[int], set[int]]:
    """解析预期中的精确状态码和状态码段，如 ``status=401``、``HTTP 4xx``。"""
    explicit: set[int] = set()
    classes: set[int] = set()
    for token in expected.replace("；", ";").replace("，", ",").split(";"):
        token = token.strip()
        if "=" not in token:
            continue
        key, _, raw_value = token.partition("=")
        if key.strip() == "status":
            try:
                explicit.add(int(raw_value.strip()))
            except ValueError:
                pass
        elif key.strip() == "status_class":
            match = re.fullmatch(r"([1-5])xx", raw_value.strip(), re.I)
            if match:
                classes.add(int(match.group(1)))
    for match in re.finditer(r"\bHTTP\s*((?:[1-5]\d{2})(?:\s*/\s*[1-5]\d{2})*)\b", expected, re.I):
        explicit.update(int(code) for code in re.findall(r"[1-5]\d{2}", match.group(1)))
    classes.update(int(group) for group in re.findall(r"\bHTTP\s*([1-5])xx\b", expected, re.I))
    return explicit, classes


def _judge_assertion(
    expected: str, status: int, body: Any, assertions: Any = None, request_params: Any = None
) -> tuple[bool, list[str]]:
    """断言判定：先校验精确/区间 HTTP 状态码，再校验结构化/兼容字段断言。

    Returns:
        (是否通过, 语义失配清单)。
    """
    allowed_statuses, allowed_classes = _expected_statuses(expected)
    if allowed_statuses or allowed_classes:
        is_pass = status in allowed_statuses or status // 100 in allowed_classes
    else:
        is_pass = 200 <= status < 300
    if is_pass:
        semantic_failures = _semantic_mismatches(expected, body, assertions, request_params)
        is_pass = not semantic_failures
    else:
        semantic_failures = []
    return is_pass, semantic_failures


def _expects_success(expected: str) -> bool:
    """判断用例是否把 2xx 作为成功预期；无机器状态 token 时兼容旧口径。"""
    statuses, classes = _expected_statuses(expected)
    if not statuses and not classes:
        return True
    return any(200 <= value < 300 for value in statuses) or 2 in classes


async def _run_fixture_phase(
    fixtures: list[dict[str, Any]],
    phase: str,
    base_url: str,
    transport: httpx.AsyncBaseTransport | None,
    verify: bool,
    auth_headers: dict[str, str],
    *,
    case_id: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """按顺序执行 setup/teardown 夹具，并返回阻断原因。

    夹具只接受显式的相对路径、query/body 和机器状态断言；不把响应正文写入
    execution.json，避免造数接口返回的 token/敏感数据扩散到报告。
    """
    records: list[dict[str, Any]] = []
    blocking_reason: str | None = None
    for index, fixture in enumerate(fixtures, start=1):
        if str(fixture.get("phase", "setup")) != phase:
            continue
        name = str(fixture.get("name") or fixture.get("id") or f"fixture-{index:03d}")
        path = str(fixture.get("path", ""))
        method = str(fixture.get("method", "POST")).upper()
        required = bool(fixture.get("required", True))
        expected = str(fixture.get("expected", "status_class=2xx"))
        started = time.monotonic()
        record: dict[str, Any] = {
            "name": name,
            "phase": phase,
            "required": required,
        }
        if case_id:
            record["case_id"] = case_id
        if fixture.get("control_kind"):
            record["control_kind"] = str(fixture["control_kind"])
            record["interface_id"] = str(fixture.get("interface_id", ""))
        if not path.startswith("/") or path.startswith("//") or ".." in path.split("/"):
            record.update(
                {
                    "passed": False,
                    "failure_kind": "environment",
                    "failure_reason": "X-EXEC-05：夹具 path 必须是安全的相对路径",
                    "duration_ms": 0,
                }
            )
        else:
            query = fixture.get("query") if isinstance(fixture.get("query"), dict) else {}
            body = fixture.get("body") if isinstance(fixture.get("body"), dict) else {}
            fixture_base_url = str(fixture.get("base_url") or base_url).rstrip("/")
            # Credentials for the tested application must never be forwarded to a
            # separately configured fixture/control service.
            fixture_headers = auth_headers if not fixture.get("base_url") else {}
            try:
                async with httpx.AsyncClient(
                    base_url=fixture_base_url,
                    timeout=_DEFAULT_TIMEOUT_S,
                    transport=transport,
                    verify=verify,
                ) as client:
                    response = await client.request(
                        method,
                        path,
                        params=query or None,
                        json=body or None,
                        headers=fixture_headers or None,
                    )
                payload: Any
                try:
                    payload = response.json()
                except ValueError:
                    payload = response.text
                passed, semantic_failures = _judge_assertion(
                    expected, response.status_code, payload, fixture.get("assertions")
                )
                failure_reason = ""
                if not passed:
                    failure_reason = (
                        f"HTTP {response.status_code}（夹具预期：{expected}）"
                        + (f"；{'；'.join(semantic_failures)}" if semantic_failures else "")
                    )
                record.update(
                    {
                        "passed": passed,
                        "actual_status": response.status_code,
                        "failure_kind": None if passed else "environment",
                        "failure_reason": failure_reason,
                        "duration_ms": _elapsed_ms(started),
                    }
                )
            except httpx.TimeoutException as exc:
                record.update(
                    {
                        "passed": False,
                        "failure_kind": "environment",
                        "failure_reason": f"X-EXEC-05：夹具执行超时（{type(exc).__name__}）",
                        "duration_ms": _elapsed_ms(started),
                    }
                )
            except httpx.TransportError as exc:
                record.update(
                    {
                        "passed": False,
                        "failure_kind": "environment",
                        "failure_reason": f"X-EXEC-05：夹具不可达（{type(exc).__name__}）",
                        "duration_ms": _elapsed_ms(started),
                    }
                )
        records.append(record)
        if not record.get("passed") and required and blocking_reason is None:
            blocking_reason = f"X-EXEC-05：必需夹具 {name} 执行失败，前置条件未满足"
    return records, blocking_reason


def _job_scope_fixtures(fixtures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        fixture for fixture in fixtures
        if isinstance(fixture, dict)
        and str(fixture.get("scope", "job")) == "job"
        and not fixture.get("control_kind")
    ]


def _matching_case_control_fixtures(
    case: dict[str, Any], fixtures: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    interface_id = str(case.get("interface_id", ""))
    scenario = str(case.get("oracle_scenario", "")).casefold()
    if not interface_id or not scenario or case.get("oracle_status") == "unverified":
        return []
    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for fixture in fixtures:
        if (
            not isinstance(fixture, dict)
            or fixture.get("scope") != "case"
            or not fixture.get("control_kind")
            or str(fixture.get("interface_id", "")) != interface_id
            or str(fixture.get("scenario_contains", "")).casefold() not in scenario
        ):
            continue
        key = (
            str(fixture.get("name", "")),
            str(fixture.get("control_kind", "")),
            interface_id,
            str(fixture.get("scenario_contains", "")),
            str(fixture.get("base_url", "")),
        )
        groups.setdefault(key, []).append(fixture)
    return [
        fixture
        for group in groups.values()
        if {str(item.get("phase", "setup")) for item in group} == {"setup", "teardown"}
        and all(sum(str(item.get("phase")) == phase for item in group) == 1
                for phase in ("setup", "teardown"))
        for fixture in group
    ]


def _skipped_case_result(case: dict[str, Any], reason: str) -> dict[str, Any]:
    """夹具失败时生成不发请求的前置缺失结果。"""
    result = {
        "case_id": case.get("id"),
        "interface_id": case.get("interface_id"),
        "case_type": str(case.get("case_type", "api")),
        "expected": str(case.get("expected", "")),
        **_case_trace_fields(case),
        "timestamp": _now_iso(),
        "passed": False,
        "failure_kind": None,
        "failure_reason": reason,
        "timeout": False,
        "duration_ms": 0,
        "skipped": True,
        "skipped_reason": "前置缺失",
        "execution_state": "not_executed",
    }
    if case.get("oracle_status") == "unverified":
        result.update(
            {
                "assertion_scope": "unverified",
                "failure_reason": "Code Review 未找到足够源码证据确认该用例的执行预期，未发起请求",
                "skipped_reason": "源码预期待确认",
            }
        )
    return result


def _can_observe_unknown_contract(oracle: dict[str, Any], blockers: list[str]) -> bool:
    """A request-controlled scenario may collect evidence without judging an unknown mapping."""
    mapping_gap = "本接口处理行为已有源码依据，HTTP 响应映射尚未确认"
    return bool(
        oracle.get("expected_status") == "unresolved"
        and blockers and set(blockers) == {mapping_gap}
        and not oracle.get("fixture_plan")
        and not oracle.get("control_kinds")
        and not any(c.get("mode") == "controlled" for c in oracle.get("state_conditions", []))
        and any(c.get("op") not in {"exists", "type"} for c in oracle.get("preconditions", []))
    )


def _observe_source_paths(
    results: list[dict[str, Any]], cases: list[dict[str, Any]],
    oracles: dict[str, dict[str, Any]], interfaces: dict[str, dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> None:
    """Observe existing responses; never synthesize a response or an expected value.

    Reuse is limited to naturally observed downstream conditions. Configuration,
    registries, fixtures, unknown response contracts and unknown path prerequisites
    cannot be satisfied by another request's response.
    """
    cases_by_id = {str(case.get("id")): case for case in cases}
    sent = [r for r in results if (r.get("request") or {}).get("attempted") is True
            and "actual_body" in r and r.get("failure_kind") != "environment"]
    for result in sent:
        oracle = oracles.get(str(result.get("oracle_id")), {})
        interface_id = str(result.get("interface_id"))
        observation = response_path_observation(
            oracle, interfaces.get(interface_id, {}),
            [e for e in evidence if interface_id in e.get("interface_ids", [])], result["actual_body"],
        )
        result["branch_observation"] = _safe_actual_body(observation)
        if observation["status"] == "not_reached":
            result.update({
                "response_contract_passed": result.get("passed") is True,
                "passed": False, "failure_kind": None, "skipped": False,
                "execution_state": "observed_unjudged", "skipped_reason": "目标分支未触达",
                "failure_reason": "请求已发送，但返回状态不满足目标路径的前序条件；响应断言通过不能作为该业务分支通过",
            })
    for result in results:
        case = cases_by_id.get(str(result.get("case_id")), {})
        oracle = oracles.get(str(case.get("oracle_id")), {})
        controls = set(oracle.get("control_kinds") or []) | required_control_kinds_for_oracle(oracle)
        blockers = execution_blockers(oracle)
        if (result.get("execution_state") != "not_executed"
                or case.get("oracle_status") != "verified" or case.get("loadable") is False
                or controls != {"downstream_response"} or oracle.get("fixture_plan")
                or case.get("fixture_plan")
                or not result.get("execution_blockers")
                or any(not reason.startswith(("目标源码分支的条件来源尚未能绑定", "尚未准备 downstream_response"))
                       for reason in result.get("execution_blockers", []))
                or oracle.get("expected_status") == "unresolved"
                or not oracle.get("assertions")
                or any(not reason.startswith("目标源码分支的条件来源尚未能绑定") for reason in blockers)
                or any(c.get("kind") != "downstream_response" and c.get("mode") == "controlled"
                       for c in oracle.get("state_conditions", []))):
            continue
        if_id = str(case.get("interface_id"))
        iface = interfaces.get(if_id, {})
        iface_evidence = [e for e in evidence if if_id in e.get("interface_ids", [])]
        for source in sent:
            if source.get("interface_id") != if_id or source.get("fixture_context"):
                continue
            source_case = cases_by_id.get(str(source.get("case_id")), {})
            if resolved_auth_mode(case) != resolved_auth_mode(source_case):
                continue
            params = materialize_declared_defaults(source_case, iface).get("params", {})
            checks = evaluate_response_assertions(params, oracle.get("preconditions", []))
            if not all(check["passed"] for check in checks):
                continue
            observation = response_path_observation(oracle, iface, iface_evidence, source["actual_body"])
            if observation["status"] != "matched_observable_path":
                continue
            passed, failures = _judge_assertion(
                str(oracle.get("expected_status")), int(source["actual_status"]),
                source["actual_body"], oracle["assertions"], params,
            )
            result.update({
                "execution_state": "reused_response", "source_case_id": source["case_id"],
                "source_params": _safe_actual_body(params),
                "actual_status": source["actual_status"], "actual_body": source["actual_body"],
                "branch_observation": _safe_actual_body(observation),
                "assertion_scope": response_assertion_scope(oracle["assertions"]),
                "assertion_results": _response_assertion_results(
                    str(oracle.get("expected_status")), source["actual_body"], oracle["assertions"], params,
                ),
                "passed": passed, "skipped": False,
                "failure_kind": None if passed else "assertion",
                "failure_reason": "" if passed else "复用实际响应的源码断言失败：" + ";".join(failures),
            })
            result.pop("skipped_reason", None)
            break


def _record_fixture_payload_ok(response: InvokeResponse, message: str) -> bool:
    return bool(response.ok and response.status == 200 and
                isinstance(response.body, dict) and
                response.body.get("returnMsg") == message)


def _record_fixture_body(template: dict[str, Any], marker: str) -> dict[str, Any]:
    return {
        key: (marker if value == "__AUTOTEST_MARKER__" else value)
        for key, value in template.items()
    }


async def _record_fixture_query(
    plan: dict[str, Any], interfaces: dict[str, dict[str, Any]],
    runner: RestRunner, marker: str | None = None,
    *, record_id: int | None = None,
    query_body: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    query = interfaces.get(str(plan["query_interface_id"]))
    if query is None:
        return None, "查询接口不存在"
    response = await runner.invoke(InvokeRequest(
        interface=query,
        params={"request": _record_fixture_body(
            query_body if isinstance(query_body, dict) else plan["query_body"],
            marker or "",
        )},
    ))
    if not _record_fixture_payload_ok(response, str(plan["query_success_message"])):
        return None, f"查询接口未返回源码约定的成功响应（HTTP {response.status or 0}）"
    body = response.body.get("body")
    if not isinstance(body, list):
        return None, "查询响应 body 不是记录列表"
    def row_id(item: dict[str, Any]) -> int | None:
        value = item.get("id")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    matched = [
        item for item in body if isinstance(item, dict)
        and item.get("deleted") in (0, None)
        and (
            item.get(plan["marker_field"]) == marker
            if marker is not None else row_id(item) == record_id
        )
    ]
    return matched, None


async def _prepare_record_fixture(
    case: dict[str, Any], plan: dict[str, Any],
    interfaces: dict[str, dict[str, Any]], runner: RestRunner,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    marker = "AT" + uuid.uuid4().hex[:20].upper()
    record: dict[str, Any] = {
        "name": f"{case.get('id')} 独立记录",
        "phase": "case",
        "case_id": case.get("id"),
        "oracle_id": case.get("oracle_id"),
        "cleanup_mode": "soft_delete",
        "marker": marker,
        "creation_attempted": False,
        "passed": False,
    }
    case_params = cast(dict[str, Any], case.get("params")) if isinstance(case.get("params"), dict) else {}
    case_body = cast(dict[str, Any], case_params.get("request")) if isinstance(case_params.get("request"), dict) else {}
    add_body = dict(plan["add_body"])
    for field_name in add_body:
        if field_name != plan["marker_field"] and field_name in case_body:
            add_body[field_name] = case_body[field_name]
    add_body[plan["marker_field"]] = marker
    query_body = dict(plan["query_body"])
    if "businessType" in query_body and "businessType" in case_body:
        query_body["businessType"] = case_body["businessType"]
    if plan["marker_field"] in query_body:
        query_body[plan["marker_field"]] = marker
    cleanup_query_body = {
        key: value for key, value in query_body.items()
        if key != plan["marker_field"]
    }
    if "businessType" in cleanup_query_body and "businessType" in case_body:
        cleanup_query_body["businessType"] = case_body["businessType"]
    record["lookup_query_body"] = query_body
    record["cleanup_query_body"] = cleanup_query_body
    original_cleanup_query_body = {
        key: value for key, value in plan["query_body"].items()
        if key != plan["marker_field"]
    }
    if "businessType" in original_cleanup_query_body:
        original_cleanup_query_body["businessType"] = add_body["businessType"]
    record["cleanup_query_bodies"] = list({
        json.dumps(body, ensure_ascii=False, sort_keys=True): body
        for body in (cleanup_query_body, original_cleanup_query_body)
    }.values())
    add = interfaces.get(str(plan["add_interface_id"]))
    if add is None:
        record["failure_reason"] = "新增接口不存在"
        return None, record
    record["creation_attempted"] = True
    response = await runner.invoke(InvokeRequest(
        interface=add,
        params={"request": add_body},
    ))
    if not _record_fixture_payload_ok(response, str(plan["add_success_message"])):
        record["failure_reason"] = f"新增接口未返回源码约定的成功响应（HTTP {response.status or 0}）"
        # An error response can happen after a write. Always try to recover the
        # uniquely marked row and clean it up instead of assuming it was not made.
        record["cleanup_status"] = "unknown"
        return None, record
    for attempt in range(3):
        matches, error = await _record_fixture_query(
            plan, interfaces, runner, marker, query_body=query_body
        )
        if error is None and matches is not None and len(matches) == 1:
            row_id = matches[0].get("id")
            if isinstance(row_id, str) and row_id.isdigit():
                row_id = int(row_id)
            if isinstance(row_id, int) and not isinstance(row_id, bool) and row_id > 0:
                body = dict(plan["target_body"])
                body.pop(plan["id_field"], None)
                body.pop(plan["marker_field"], None)
                body.update(case_body)
                body[plan["id_field"]] = row_id if plan["id_field"] == "id" else [row_id]
                record.update({"passed": True, "record_id": row_id})
                return {**case, "params": {"request": body}}, record
        if error is not None:
            record["failure_reason"] = error
        if attempt < 2:
            await asyncio.sleep(0.2)
    record.setdefault("failure_reason", "新增已返回成功，但查询未唯一定位测试记录；需按标记清理")
    record["cleanup_status"] = "unknown"
    return None, record


def _prepare_add_case_fixture(
    case: dict[str, Any], plan: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Mark the record created by this add case so it can be found and soft-deleted."""
    marker = "AT" + uuid.uuid4().hex[:20].upper()
    params = cast(dict[str, Any], case.get("params")) if isinstance(case.get("params"), dict) else {}
    request = cast(dict[str, Any], params.get("request")) if isinstance(params.get("request"), dict) else None
    record: dict[str, Any] = {
        "name": f"{case.get('id')} 新增记录",
        "phase": "case",
        "case_id": case.get("id"),
        "oracle_id": case.get("oracle_id"),
        "cleanup_mode": "soft_delete",
        "marker": marker,
        "creation_attempted": True,
        "passed": False,
    }
    if request is None:
        record["failure_reason"] = "新增用例缺少 request 对象，无法注入隔离标记"
        return None, record
    body = dict(request)
    body[str(plan["marker_field"])] = marker
    query_body = dict(plan["query_body"])
    if "businessType" in query_body and "businessType" in body:
        query_body["businessType"] = body["businessType"]
    if str(plan["marker_field"]) in query_body:
        query_body[str(plan["marker_field"])] = marker
    record["lookup_query_body"] = query_body
    record["cleanup_query_body"] = {
        key: value for key, value in query_body.items()
        if key != str(plan["marker_field"])
    }
    record["cleanup_query_bodies"] = [record["cleanup_query_body"]]
    return {**case, "params": {**params, "request": body}}, record


async def _cleanup_record_fixture(
    plan: dict[str, Any], interfaces: dict[str, dict[str, Any]],
    runner: RestRunner, record: dict[str, Any],
) -> tuple[bool, str, bool]:
    """按真实 ID 调用源码软删除接口，并用查询结果检查活记录是否残留。"""
    marker = str(record["marker"])
    known_id = record.get("record_id")
    has_id = isinstance(known_id, int) and not isinstance(known_id, bool)
    if not has_id:
        lookup_body = record.get("lookup_query_body")
        lookup_body = lookup_body if isinstance(lookup_body, dict) else None
        matches: list[dict[str, Any]] | None = None
        error: str | None = None
        for attempt in range(3):
            matches, error = await _record_fixture_query(
                plan, interfaces, runner, marker, query_body=lookup_body
            )
            if error or matches:
                break
            if attempt < 2:
                await asyncio.sleep(0.2)
        if error:
            return False, error, False
        if not matches:
            if not record.get("creation_attempted"):
                return True, "新增请求未发起，无测试记录需要清理", False
            return False, "新增请求可能已写入记录，但未能按唯一标记找回；不能确认已清理", False
        if len(matches) != 1:
            return False, "标记查询结果不唯一，未执行清理", False
        row_id = matches[0].get("id")
        if isinstance(row_id, str) and row_id.isdigit():
            row_id = int(row_id)
        if not isinstance(row_id, int) or isinstance(row_id, bool) or row_id <= 0:
            return False, "标记查询未返回有效记录 ID，未执行清理", False
        known_id = row_id
        record["record_id"] = row_id
        has_id = True

    if not has_id:
        return False, "无法确认测试记录 ID，未执行清理", False

    target_action = str(record.get("target_action", ""))
    still_active_after_target = False
    delete = interfaces.get(str(plan["delete_interface_id"]))
    if delete is None:
        return False, "删除接口不存在", False
    query_bodies = record.get("cleanup_query_bodies")
    if not isinstance(query_bodies, list) or not query_bodies:
        query_bodies = [record.get("cleanup_query_body")]

    # For delete cases, capture whether the target operation left the record
    # active before running cleanup. This lets the report preserve a real delete
    # bug even if the cleanup request subsequently removes the fixture.
    if target_action == "delete":
        target_query_error: str | None = None
        for query_body in query_bodies:
            remaining, error = await _record_fixture_query(
                plan, interfaces, runner, marker=None,
                record_id=known_id,
                query_body=query_body if isinstance(query_body, dict) else None,
            )
            if error:
                target_query_error = error
            elif remaining:
                still_active_after_target = True
        if not still_active_after_target and target_query_error is None:
            return True, "目标删除后查询确认活记录已消失", False

    delete_succeeded = False
    delete_failure = ""
    response = await runner.invoke(InvokeRequest(
        interface=delete,
        params={"request": {"ids": [known_id], "deleteReason": "自动测试记录清理"}},
    ))
    delete_succeeded = _record_fixture_payload_ok(
        response, str(plan["delete_success_message"])
    )
    if not delete_succeeded:
        delete_failure = (
            f"软删除接口未返回源码约定的成功响应（HTTP {response.status or 0}）"
        )

    query_error: str | None = None
    still_active = False
    for query_body in query_bodies:
        remaining, error = await _record_fixture_query(
            plan, interfaces, runner, marker=None,
            record_id=known_id,
            query_body=query_body if isinstance(query_body, dict) else None,
        )
        if error:
            query_error = error
        elif remaining:
            still_active = True
    if still_active:
        return False, "软删除后仍能查询到活记录", still_active_after_target
    if query_error:
        if delete_succeeded:
            return False, "软删除接口返回成功，但状态查询未能确认：" + query_error, still_active_after_target
        return False, delete_failure + "；状态查询失败：" + query_error, still_active_after_target
    if delete_succeeded:
        return True, "已通过接口软删除并查询确认", still_active_after_target
    if target_action == "delete" and not still_active:
        return True, "目标删除后查询确认活记录已消失", still_active_after_target
    return False, delete_failure or "未能确认记录已软删除", still_active_after_target


def _expects_404(expected: str) -> bool:
    """负向资源用例允许 404 时，不把业务性不存在误报为路由未部署。"""
    statuses, classes = _expected_statuses(expected)
    return 404 in statuses or 4 in classes


class MockDemoRunner:
    """Mock 演示执行器（X-EXEC-03 降级路径）：返回固定桩响应。

    演示口径：脚本生成成功 + Mock 执行演示——passed 仅代表演示链路跑通，
    真实性由 degraded_protocols + 报告分列保证，不冒充真实执行结果。
    """

    async def invoke(self, request: InvokeRequest) -> InvokeResponse:
        """返回 Mock 桩响应（200 + 演示标记体）。"""
        return InvokeResponse(
            ok=True,
            status=200,
            body={
                "mock_demo": True,
                "interface": request.interface.get("id"),
                "params": request.params,
            },
            error=None,
            timed_out=False,
            duration_ms=0,
        )


async def probe_rest(
    base_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
    verify: bool = True,
    issues: dict[str, str] | None = None,
) -> bool:
    """Probe an API target and reject an obvious SPA fallback at /health."""
    try:
        async with httpx.AsyncClient(
            base_url=base_url, timeout=3.0, transport=transport, verify=verify
        ) as client:
            response = await client.get("/health")
    except (httpx.TimeoutException, httpx.TransportError):
        if issues is not None:
            issues[base_url] = "被测服务不可达（X-EXEC-01，探测失败）"
        return False
    content_type = response.headers.get("content-type", "").lower()
    sample = response.text[:8_000].lower()
    if (
        response.status_code == 200 and "text/html" in content_type
        and "<html" in sample and "<script" in sample
    ):
        if issues is not None:
            issues[base_url] = (
                "被测地址返回前端网页，而非 API 健康检查响应（X-EXEC-01）；"
                "请检查 REST 服务地址和端口"
            )
        return False
    return True


async def login_for_headers(
    auth: dict[str, Any],
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, str]:
    """C-15：执行前登录被测服务，提取凭据头（数值/凭据不经 LLM）。

    实测口径（docs/13 C-15）：POST login_url JSON 登录字段（默认 {username, password}，
    可配置为 {userName, password} 等）→
    - 响应 JSON 按 token_path（默认自动识别常见根层/ data.* / result.* 路径）取 Token，
      再按 authorization_scheme 注入 Authorization；
    - 否则 Set-Cookie → Cookie 回带（session 会话口径）；
    - 登录失败（非 2xx/3xx 或连接异常）→ PipelineError X-EXEC-01（环境类，节点重试）。
    verify=insecure_tls（https 自签场景表单默认跳过校验）。
    """
    login_url = str(auth.get("login_url", ""))
    username = str(auth.get("username", ""))
    password = str(auth.get("password", ""))
    username_field = str(auth.get("username_field") or "username")
    password_field = str(auth.get("password_field") or "password")
    password_transform = str(auth.get("password_transform") or "plain")
    token_path = str(auth.get("token_path") or "auto")
    authorization_scheme = str(auth.get("authorization_scheme") or "Token")
    response_header_paths = auth.get("response_header_paths")
    if username_field == password_field:
        raise PipelineError(
            code="X-EXEC-01",
            message="鉴权配置无效（C-15）：用户名字段与密码字段不能相同",
            node_id=NODE_NAME,
        )
    verify = not bool(auth.get("insecure_tls", True))
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=transport, verify=verify) as client:
            response = await client.post(
                login_url,
                json={
                    username_field: username,
                    password_field: _transform_login_password(password, password_transform),
                },
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise PipelineError(
            code="X-EXEC-01",
            message=f"登录接口不可达（C-15）：{login_url}：{exc}",
            node_id=NODE_NAME,
        ) from exc
    if response.status_code >= 400:
        raise PipelineError(
            code="X-EXEC-01",
            message=(
                f"登录失败（C-15）：{login_url} 返回 {response.status_code}"
                f"（检查用户名/密码/登录 URL 是否带尾斜杠）"
            ),
            node_id=NODE_NAME,
        )
    # 凭据提取：自定义路径优先（或有限的常见路径自动识别），回退 Set-Cookie 回带。
    try:
        payload = response.json()
    except ValueError:
        payload = None
    mapped_headers, missing_paths = _mapped_response_headers(payload, response_header_paths)
    if mapped_headers is not None:
        return mapped_headers
    if isinstance(payload, dict):
        paths = _DEFAULT_TOKEN_PATHS if token_path == "auto" else (token_path,)
        for path in paths:
            token = _value_at_path(payload, path)
            if token:
                return {"Authorization": _authorization_value(token, authorization_scheme)}
    cookies = [
        "=".join(part.strip() for part in cookie.split("=", 1))
        for cookie in response.headers.get_list("set-cookie")
        if "=" in cookie
    ]
    if cookies:
        # 去属性（HttpOnly/SameSite 等）：只取第一个会话键值对口径
        pairs = [c.split(";", 1)[0] for c in cookies]
        return {"Cookie": "; ".join(pairs)}
    raise PipelineError(
        code="X-EXEC-01",
        message=(
            f"登录响应无凭据可提取（C-15）：{login_url}"
            f"（Token 路径={token_path}；响应顶层字段：{_safe_response_shape(payload)}；"
            + (
                f"响应头映射缺失：{'、'.join(missing_paths)}；"
                if missing_paths
                else ""
            )
            + "无 Set-Cookie）"
        ),
        node_id=NODE_NAME,
    )


def auth_exempt_paths(auth: dict[str, Any] | None) -> set[str]:
    """返回登录接口路径，供执行器跳过预登录凭据注入。"""
    if not auth:
        return set()
    path = urlparse(str(auth.get("login_url", ""))).path
    return {path} if path else set()


def interface_service_base_url(
    interface: dict[str, Any],
    default_base_url: str,
    service_base_urls: dict[str, str] | None = None,
) -> str:
    """按源码扫描得到的仓库归属选择接口服务地址（C-07）。

    接口产物中的 ``code_location.repo`` 是唯一稳定的路由键；未配置仓库级
    地址时回退到任务级 ``rest_url``，兼容单服务任务与旧产物。
    """
    repo = str((interface.get("code_location") or {}).get("repo") or "").strip()
    candidate = (service_base_urls or {}).get(repo)
    return str(candidate or default_base_url).strip().rstrip("/")


def prefers_django_trailing_slash(
    auth: dict[str, Any] | None, service_base_url: str
) -> bool:
    """Only carry a login route's slash convention to the same HTTP service."""
    login_url = str((auth or {}).get("login_url", ""))
    if not login_url:
        return False
    try:
        login = httpx.URL(login_url)
        service = httpx.URL(service_base_url)
    except (ValueError, TypeError):
        return False
    return (
        login.scheme == service.scheme
        and login.host == service.host
        and login.port == service.port
        and login.path.endswith("/")
    )


def mutates_auth_session(interface: dict[str, Any]) -> bool:
    """识别会使当前会话失效的接口，避免用例之间错误共享已登出的 token。"""
    signature = interface.get("signature") or {}
    path = str(signature.get("path", "")).lower().rstrip("/")
    summary = str(signature.get("summary", "")).lower()
    return path.endswith("/logout") or "退出登录" in summary or "logout" in summary


def resolved_auth_mode(case: dict[str, Any]) -> str:
    """读取显式认证模式，并为旧产物识别典型的未认证/伪 token 反例。"""
    explicit = str(case.get("auth_mode", "")).lower()
    if explicit in {"inherit", "missing", "invalid"}:
        return explicit
    expected = str(case.get("expected", "")).lower()
    category = str(case.get("category", ""))
    auth_words = ("token", "认证", "未认证", "401", "403")
    expects_auth_error = any(word in expected for word in auth_words) and any(
        mark in expected for mark in ("4xx", "401", "403")
    )
    return "missing" if category in {"boundary", "exception"} and expects_auth_error else "inherit"


async def run_auto_execute(
    engine: Engine,
    state: JobState,
    store: ArtifactStore,
    rest_base_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
    auth: dict[str, Any] | None = None,
    e2e: dict[str, Any] | None = None,
    insecure_tls: bool = True,
    preauthenticated_headers: dict[str, str] | None = None,
    fixtures: list[dict[str, Any]] | None = None,
    service_base_urls: dict[str, str] | None = None,
    llm: SupportsComplete | None = None,
    prompts_dir: Path | None = None,
) -> Path:
    """执行 auto_execute 全流程：读用例 → 探测 → 分协议执行 → 落盘。

    Args:
        store: test-executor 身份的产物卷（execution.json 丙写）。
        rest_base_url: REST 被测/Mock 服务地址（R-EXEC-02 探测与执行共用）。
        transport: 测试注入（MockTransport），生产留 None。
        auth: C-15 鉴权配置（login_url/username/password/insecure_tls）；
            非空时先登录拿凭据头注入全部 REST 请求（登录失败 X-EXEC-01 节点重试）。
        insecure_tls: REST HTTPS 是否跳过证书校验；与登录、E2E 配置独立，确保
            探活与每一条 REST 请求使用完全一致的策略。
        fixtures: 可选 setup/teardown 夹具；必需 setup 失败时所有用例标记前置缺失并跳过。
        service_base_urls: 仓库名到接口服务地址的可选映射（C-07）；未命中回退 rest_base_url。

    Returns:
        execution.json 产物路径。

    Raises:
        PipelineError: 前置产物缺失（X-ORCH-01）；跳序（X-ORCH-02）；登录失败（X-EXEC-01）。
    """
    engine.check_runnable(state, NODE_NAME)
    engine.mark_running(state, NODE_NAME)
    try:
        testcases = _load_json_array(store, state.job_id, "testcases.json", "cases")
        testcase_payload = store.read_json(state.job_id, "testcases.json")
        e2e_cases = testcase_payload.get("e2e_cases", [])
        if not isinstance(e2e_cases, list):
            e2e_cases = []
        interfaces = _load_json_array(store, state.job_id, "interfaces.json", "interfaces")
        interface_by_id = {str(i.get("id", "")): i for i in interfaces if i.get("id")}
        review = store.read_json(state.job_id, "review_mapping.json")
        try:
            job_input = store.read_json(state.job_id, "job_input.json")
        except FileNotFoundError:
            job_input = {}
        auto_record_fixtures = bool(job_input.get("auto_record_fixtures", False))
        required_body_contract = job_input.get("required_body_contract")
        oracles_by_id = {
            str(item.get("oracle_id", "")): item for item in review.get("test_oracles") or []
            if isinstance(item, dict)
        }
        fixture_plans_by_oracle = {
            str(item.get("oracle_id")): item["fixture_plan"]
            for item in review.get("test_oracles") or []
            if isinstance(item, dict) and isinstance(item.get("fixture_plan"), dict)
        }
        success_messages = {"add": "新增成功", "update": "更新成功", "delete": "删除成功"}
        unisolated_success_oracle_ids: set[str] = set()
        unisolated_success_interface_ids: set[str] = set()
        for oracle in review.get("test_oracles") or []:
            if not isinstance(oracle, dict) or oracle.get("category") != "normal" or oracle.get("fixture_plan"):
                continue
            oracle_interface_id = str(oracle.get("interface_id", ""))
            oracle_interface = next(
                (item for item in interfaces if str(item.get("id", "")) == oracle_interface_id), {}
            )
            action = str((oracle_interface.get("signature") or {}).get("path", "")).rsplit("/", 1)[-1]
            message = success_messages.get(action)
            if message and any(
                item.get("path") == "returnMsg" and item.get("op") == "equals"
                and item.get("value") == message
                for item in oracle.get("assertions", []) if isinstance(item, dict)
            ):
                unisolated_success_oracle_ids.add(str(oracle.get("oracle_id", "")))
                unisolated_success_interface_ids.add(oracle_interface_id)
        contracts_by_interface: dict[str, list[dict[str, Any]]] = {}
        for contract in review.get("field_value_contracts") or []:
            if isinstance(contract, dict) and contract.get("interface_id"):
                contracts_by_interface.setdefault(
                    str(contract["interface_id"]), []
                ).append(contract)

        # REST 证书策略独立于登录配置，避免「未配置登录 → 自签环境全量不可达」。
        auth_headers: dict[str, str] = dict(preauthenticated_headers or {})
        verify = not insecure_tls
        has_verified_api_case = any(
            case.get("oracle_status") != "unverified" for case in testcases
        )
        has_executable_e2e_case = bool(
            e2e and e2e.get("execute_cases") and e2e_cases
        )
        if auth and not auth_headers and has_verified_api_case:
            auth_headers = await login_for_headers(auth, transport)

        # R-EXEC-02：环境探测优先于执行。C-07 允许不同仓库的接口指向不同
        # 服务；未配置仓库地址时仍只探测/使用任务级 rest_url。
        default_service_url = rest_base_url.rstrip("/")
        route_urls = {default_service_url}
        route_urls.update(
            str(url).strip().rstrip("/")
            for url in (service_base_urls or {}).values()
            if str(url).strip()
        )
        probe_issues: dict[str, str] = {}
        rest_ready_by_url = {
            url: await probe_rest(url, transport, verify, probe_issues) for url in route_urls
        }
        rest_runners = {
            url: RestRunner(
                url,
                transport,
                auth_headers,
                verify,
                auth_exempt_paths(auth),
                prefers_django_trailing_slash(auth, url),
            )
            for url in route_urls
        }
        demo_runner = MockDemoRunner()

        results: list[dict[str, Any]] = []
        degraded_protocols: dict[str, dict[str, Any]] = {}
        fixture_records: list[dict[str, Any]] = []
        setup_block_reason: str | None = None
        all_fixture_configs = fixtures or []
        fixture_configs = _job_scope_fixtures(all_fixture_configs)
        control_fixture_configs = [
            fixture for fixture in all_fixture_configs
            if isinstance(fixture, dict) and fixture.get("scope") == "case"
        ]
        if fixture_configs and (has_verified_api_case or has_executable_e2e_case):
            fixture_records, setup_block_reason = await _run_fixture_phase(
                fixture_configs,
                "setup",
                rest_base_url,
                transport,
                verify,
                auth_headers,
            )
        if setup_block_reason:
            results.extend(_skipped_case_result(case, setup_block_reason) for case in testcases)
        else:
            for case in testcases:
                if_id = str(case.get("interface_id", ""))
                interface = interface_by_id.get(if_id, {})
                protocol = str(interface.get("protocol", "rest"))
                route_url = interface_service_base_url(
                    interface, default_service_url, service_base_urls
                )
                canonical_oracle = oracles_by_id.get(str(case.get("oracle_id", "")), {})
                active_body_contract = (
                    required_body_contract if isinstance(required_body_contract, dict)
                    and case.get("oracle_basis") == "spring_required_body"
                    and if_id in (required_body_contract.get("interface_ids") or [])
                    and required_body_contract.get("source") == "user_confirmed_environment"
                    else None
                )
                effective_case = case
                if active_body_contract:
                    contract_assertions = [{"path": "returnCode", "op": "equals",
                                            "value": active_body_contract["return_code"]}]
                    if active_body_contract.get("body_is_null"):
                        contract_assertions.append({"path": "body", "op": "equals", "value": None})
                    effective_case = {**case, "expected": f"status={active_body_contract['http_status']}",
                                      "assertions": contract_assertions,
                                      "environment_contract_confirmed": True,
                                      "expectation_source": "user_confirmed_environment"}
                blockers = list(dict.fromkeys(
                    execution_blockers(canonical_oracle) + list(case.get("execution_blockers") or [])
                ))
                if active_body_contract:
                    blockers = [reason for reason in blockers if reason !=
                                "本接口处理行为已有源码依据，HTTP 响应映射尚未确认"]
                case_control_fixtures = _matching_case_control_fixtures(case, control_fixture_configs)
                required_kinds = set(case.get("oracle_control_kinds") or [])
                required_kinds.update(canonical_oracle.get("control_kinds") or [])
                required_kinds.update(required_control_kinds_for_oracle(canonical_oracle))
                available_kinds = {str(f.get("control_kind")) for f in case_control_fixtures}
                missing_kinds = required_kinds - available_kinds
                observe_contract = (
                    protocol == "rest" and _can_observe_unknown_contract(canonical_oracle, blockers)
                    and all(check["passed"] for check in evaluate_response_assertions(
                        materialize_declared_defaults(case, interface).get("params", {}),
                        canonical_oracle.get("preconditions", []),
                    ))
                )
                if (blockers and not observe_contract) or missing_kinds:
                    reasons = blockers + [f"尚未准备 {kind} 控制条件" for kind in sorted(missing_kinds)]
                    skipped = _skipped_case_result(
                        case, "X-EXEC-05：用例设计已保留，执行条件尚未满足：" + "；".join(reasons)
                    )
                    skipped["execution_blockers"] = reasons
                    skipped["missing_control_kinds"] = sorted(missing_kinds)
                    results.append(skipped)
                    continue
                plan = case.get("fixture_plan")
                canonical_plan = fixture_plans_by_oracle.get(str(case.get("oracle_id", "")))
                needs_fixture = (
                    auto_record_fixtures
                    and case.get("category") == "normal"
                    and _expects_success(str(case.get("expected", "")))
                    and (
                        str(case.get("oracle_id", "")) in unisolated_success_oracle_ids
                        or (
                            not case.get("oracle_id")
                            and if_id in unisolated_success_interface_ids
                        )
                    )
                )
                if needs_fixture and not isinstance(canonical_plan, dict):
                    results.append(_skipped_case_result(
                        case,
                        "X-EXEC-05：数据库成功分支没有 Code Review 认可的自动造数与软删除计划；"
                        "为避免使用历史或臆造 ID，已隔离该用例",
                    ))
                    continue
                if (plan is not None or canonical_plan is not None) and plan != canonical_plan:
                    results.append(_skipped_case_result(
                        case, "X-EXEC-05：用例造数计划与 Code Review 源码契约不一致"
                    ))
                    continue
                if plan is not None and protocol != "rest":
                    results.append(_skipped_case_result(
                        case, "X-EXEC-05：源码造数计划只支持 REST 接口"
                    ))
                    continue
                prepared_case = ({**effective_case, "observe_unknown_contract": True}
                                 if observe_contract else effective_case)
                fixture_record: dict[str, Any] | None = None
                fixture_ready = (
                    isinstance(plan, dict) and protocol == "rest"
                    and rest_ready_by_url.get(route_url, False)
                    and case.get("oracle_status") == "verified"
                    and case.get("loadable") is not False
                )
                if isinstance(plan, dict) and not fixture_ready:
                    results.append(_skipped_case_result(
                        case, "X-EXEC-05：造数计划未能执行；为避免使用不确定的旧记录 ID，已隔离该用例"
                    ))
                    continue
                case_id = str(case.get("id", ""))
                case_control_fixtures = _matching_case_control_fixtures(
                    case, control_fixture_configs
                )
                case_control_kinds = case.get("oracle_control_kinds")
                required_control_kinds = {
                    str(kind) for kind in (case_control_kinds if isinstance(case_control_kinds, list) else [])
                    if isinstance(kind, str)
                }
                required_control_kinds.update(canonical_oracle.get("control_kinds") or [])
                required_control_kinds.update(required_control_kinds_for_oracle(canonical_oracle))
                available_control_kinds = {
                    str(fixture.get("control_kind")) for fixture in case_control_fixtures
                }
                missing_control_kinds = required_control_kinds - available_control_kinds
                if missing_control_kinds:
                    results.append(_skipped_case_result(
                        case,
                        "X-EXEC-05：用例设计已保留，待准备指定返回或环境状态的控制条件："
                        + ", ".join(sorted(missing_control_kinds)),
                    ))
                    continue
                if case_control_fixtures:
                    case_fixture_records, case_setup_block_reason = await _run_fixture_phase(
                        case_control_fixtures,
                        "setup",
                        rest_base_url,
                        transport,
                        verify,
                        auth_headers,
                        case_id=case_id,
                    )
                    fixture_records.extend(case_fixture_records)
                    if case_setup_block_reason:
                        teardown_records, teardown_block_reason = await _run_fixture_phase(
                            case_control_fixtures,
                            "teardown",
                            rest_base_url,
                            transport,
                            verify,
                            auth_headers,
                            case_id=case_id,
                        )
                        fixture_records.extend(teardown_records)
                        skipped_result = _skipped_case_result(case, case_setup_block_reason)
                        if teardown_block_reason:
                            skipped_result["failure_reason"] += "；清理步骤也未完成"
                        results.append(skipped_result)
                        continue
                if fixture_ready:
                    target_path = str((interface.get("signature") or {}).get("path", ""))
                    if target_path.endswith("/add"):
                        prepared, fixture_record = _prepare_add_case_fixture(case, cast(dict[str, Any], plan))
                    else:
                        prepared, fixture_record = await _prepare_record_fixture(
                            case, cast(dict[str, Any], plan), interface_by_id, rest_runners[route_url]
                        )
                    if fixture_record is not None:
                        fixture_record["target_action"] = target_path.rsplit("/", 1)[-1]
                    if prepared is None:
                        if case_control_fixtures:
                            teardown_records, _ = await _run_fixture_phase(
                                case_control_fixtures,
                                "teardown",
                                rest_base_url,
                                transport,
                                verify,
                                auth_headers,
                                case_id=case_id,
                            )
                            fixture_records.extend(teardown_records)
                        results.append(_skipped_case_result(
                            case, "X-EXEC-05：用例造数失败；" +
                            str(fixture_record.get("failure_reason", "无法取得测试记录"))
                        ))
                        fixture_records.append(fixture_record)
                        continue
                    prepared_case = prepared
                case_rest_ready = rest_ready_by_url.get(route_url, False)
                if case_control_fixtures and protocol == "rest":
                    case_rest_ready = await probe_rest(
                        route_url, transport, verify, probe_issues
                    )
                result: dict[str, Any] | None = None
                try:
                    result = await _execute_case(
                        prepared_case,
                        interface,
                        protocol,
                        case_rest_ready,
                        rest_runners[route_url],
                        demo_runner,
                        degraded_protocols,
                        auth_configured=bool(auth or preauthenticated_headers),
                        field_contracts=contracts_by_interface.get(if_id, []),
                        rest_probe_issue=probe_issues.get(route_url),
                    )
                    if fixture_configs or case_control_fixtures or fixture_record:
                        result["fixture_context"] = True
                finally:
                    if fixture_record is not None and fixture_ready:
                        try:
                            cleaned, detail, still_active = await _cleanup_record_fixture(
                                cast(dict[str, Any], plan), interface_by_id, rest_runners[route_url], fixture_record
                            )
                        except Exception as exc:  # noqa: BLE001 — cleanup errors must be reported with the case
                            cleaned = False
                            still_active = fixture_record.get("target_action") == "delete"
                            detail = f"软删除清理发生内部异常（{type(exc).__name__}）"
                        fixture_record["cleanup_status"] = (
                            "soft_deleted" if cleaned and fixture_record.get("record_id")
                            else "not_created" if cleaned else "failed"
                        )
                        fixture_record["cleanup_detail"] = detail
                        fixture_records.append(fixture_record)
                    if case_control_fixtures:
                        teardown_records, teardown_block_reason = await _run_fixture_phase(
                            case_control_fixtures,
                            "teardown",
                            rest_base_url,
                            transport,
                            verify,
                            auth_headers,
                            case_id=case_id,
                        )
                        fixture_records.extend(teardown_records)
                        if teardown_block_reason and result is not None:
                            original_failure = result.get("failure_reason")
                            result.update({
                                "passed": False,
                                "failure_kind": "environment",
                                "failure_reason": (
                                    teardown_block_reason
                                    + (f"；原用例结果：{original_failure}" if original_failure else "")
                                ),
                            })
                if fixture_record is not None:
                    result["fixture_cleanup"] = fixture_record["cleanup_status"]
                    if still_active and result.get("passed"):
                        result.update({
                            "passed": False,
                            "failure_kind": "assertion",
                            "failure_reason": "删除接口返回成功，但测试记录仍处于未删除状态",
                        })
                    if fixture_record["cleanup_status"] == "failed":
                        original_failure = result.get("failure_reason")
                        cleanup_failure = (
                            "测试记录软删除清理未确认："
                            + str(fixture_record.get("cleanup_detail", "未知原因"))
                        )
                        if result.get("failure_kind") == "assertion":
                            result["cleanup_failure_reason"] = cleanup_failure
                        else:
                            result["original_failure_reason"] = original_failure
                            result.update({
                                "passed": False,
                                "failure_kind": "environment",
                                "failure_reason": (
                                    cleanup_failure
                                    + (f"；原用例失败：{original_failure}" if original_failure else "")
                                ),
                            })
                results.append(result)
                # 登出等接口可能撤销当前 token。测试用例必须相互隔离，不能让它污染
                # 同一批中随后“已认证用户”场景；重新登录只作用于下一条 REST 用例。
                if auth and protocol == "rest" and mutates_auth_session(interface):
                    auth_headers = await login_for_headers(auth, transport)
                    for runner in rest_runners.values():
                        runner.replace_auth_headers(auth_headers)

        if e2e and bool(e2e.get("execute_cases")):
            base_url = str(e2e.get("base_url", "")).rstrip("/")
            ignore_https_errors = bool(e2e.get("insecure_tls", True))
            total = len(e2e_cases)
            _append_runtime_log(
                store,
                state.job_id,
                event="e2e_started",
                total=total,
                message=f"开始执行 {total} 条前端 E2E 用例",
            )
            if (e2e_cases and all(case.get("contract_version") == "2.0" for case in e2e_cases)
                    and not setup_block_reason and prompts_dir is not None):
                from executor.e2e.service import execute_job
                e2e_execution = await execute_job(
                    store, state.job_id, llm, prompts_dir, e2e, auth, auth_headers,
                    progress=lambda value: _append_runtime_log(
                        store, state.job_id, event="e2e_" + value["event"],
                        case_id=value["case_id"], message=value.get("intent") or value.get("status", ""),
                    ),
                )
                results.extend(e2e_execution["results"])
                e2e_cases = []
            for index, case in enumerate(e2e_cases, start=1):
                if setup_block_reason:
                    results.append(
                        _skipped_case_result(
                            {
                                **dict(case),
                                "case_type": "e2e",
                                "interface_id": "UI-E2E-001",
                            },
                            setup_block_reason,
                        )
                    )
                    continue
                case_id = str(case.get("id", f"E2E-{index:03d}"))
                _append_runtime_log(
                    store,
                    state.job_id,
                    event="e2e_case_started",
                    index=index,
                    total=total,
                    case_id=case_id,
                    message=f"正在执行 {case_id}",
                )
                if case.get("contract_version") == "2.0":
                    from executor.e2e.runtime import execute_case
                    from executor.e2e.planner import recover_target
                    async def recover(step: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any] | None:
                        if llm is None or prompts_dir is None:
                            return None
                        return await recover_target(llm, prompts_dir, step, observation)
                    result = await execute_case(
                        dict(case), {**e2e, "run_id": str(uuid4().hex)},
                        lambda path, content: store.write_bytes(state.job_id, path, content).relative_to(store.job_dir(state.job_id)).as_posix(),
                        auth=auth, auth_headers=auth_headers, recover=recover,
                    )
                else:
                    result = await _execute_e2e_case(
                        dict(case),
                        base_url,
                        ignore_https_errors,
                        _e2e_runtime_values(auth),
                        auth_headers=(
                            auth_headers
                            if str(e2e.get("auth_mode", "browser")) == "reuse"
                            else None
                        ),
                        evidence_writer=lambda relative_path, content: store.write_bytes(
                            state.job_id, relative_path, content
                        ).relative_to(store.job_dir(state.job_id)).as_posix(),
                    )
                results.append(result)
                _append_runtime_log(
                    store,
                    state.job_id,
                    event="e2e_case_completed",
                    index=index,
                    total=total,
                    case_id=case_id,
                    ok=bool(result.get("passed")),
                    duration_ms=result.get("duration_ms"),
                    message=(
                        "通过"
                        if result.get("passed")
                        else str(result.get("failure_reason", "执行失败"))
                    ),
                )

        # 新增成功后若短时查询不到 ID，结束前再按唯一标记补查一次，清理
        # 已经落库但未能进入目标接口执行的记录。
        case_by_id = {str(case.get("id", "")): case for case in testcases}
        for fixture_record in fixture_records:
            if fixture_record.get("phase") != "case" or fixture_record.get("cleanup_status") != "unknown":
                continue
            case = case_by_id.get(str(fixture_record.get("case_id", "")), {})
            plan = fixture_plans_by_oracle.get(str(fixture_record.get("oracle_id", "")))
            interface = interface_by_id.get(str(case.get("interface_id", "")), {})
            if not plan or not interface:
                fixture_record["cleanup_status"] = "failed"
                fixture_record["cleanup_detail"] = "无法读取源码造数计划以重试清理"
                continue
            route_url = interface_service_base_url(interface, default_service_url, service_base_urls)
            if not fixture_record.get("target_action"):
                fixture_record["target_action"] = str(
                    ((interface.get("signature") or {}).get("path", "")).rsplit("/", 1)[-1]
                )
            try:
                cleaned, detail, _ = await _cleanup_record_fixture(
                    plan, interface_by_id, rest_runners[route_url], fixture_record
                )
            except Exception as exc:  # noqa: BLE001 — retain cleanup outcome in execution.json
                cleaned = False
                detail = f"软删除清理重试发生内部异常（{type(exc).__name__}）"
            fixture_record["cleanup_status"] = (
                "soft_deleted" if cleaned and fixture_record.get("record_id")
                else "not_created" if cleaned else "failed"
            )
            fixture_record["cleanup_detail"] = detail

        if fixture_configs:
            teardown_records, _ = await _run_fixture_phase(
                fixture_configs,
                "teardown",
                rest_base_url,
                transport,
                verify,
                auth_headers,
            )
            fixture_records.extend(teardown_records)

        _observe_source_paths(results, testcases, oracles_by_id, interface_by_id,
                              review.get("evidence_catalog") or [])
        for result in results:
            if "execution_state" not in result:
                result["execution_state"] = (
                    "observed_unjudged" if result.get("skipped_reason") and (result.get("request") or {}).get("attempted") else
                    "not_executed" if result.get("skipped") else "executed"
                )
            if result["execution_state"] == "observed_unjudged":
                result["skipped"] = False
        unit_coverage: dict[str, Any] | None = None
        if job_input.get("generate_unit_tests"):
            try:
                inventory = store.read_json(state.job_id, "code_inventory.json")
            except FileNotFoundError:
                inventory = {}
            try:
                unit_coverage = await run_controlled_unit_coverage(
                    store, state.job_id, testcases, results, interfaces, review,
                    inventory, llm, prompts_dir,
                )
            except Exception as exc:  # 单测附加分析不能吞掉已完成的接口结果
                unit_coverage = {
                    "status": "unavailable", "reason": f"隔离单元测试未完成（{type(exc).__name__}）",
                    "cases_total": 0, "cases_passed": 0, "cases_failed": 0,
                    "repositories": [],
                }
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "job_id": state.job_id,
            "results": results,
            "summary": _summarize(results),
        }
        if unit_coverage is not None:
            payload["unit_test_coverage"] = unit_coverage
        if degraded_protocols:
            payload["degraded_protocols"] = list(degraded_protocols.values())
        if fixture_records:
            payload["fixtures"] = fixture_records
        path = store.write(state.job_id, "execution.json", payload)
        # D4：允许失败用例——失败二态明确即完成（不 mark_degraded）
        engine.mark_completed(state, NODE_NAME)
        return path
    except PipelineError as exc:
        engine.mark_failed(state, NODE_NAME, exc)
        raise


async def _execute_e2e_case(
    case: dict[str, Any], base_url: str, ignore_https_errors: bool = True,
    runtime_values: dict[str, str] | None = None,
    auth_headers: dict[str, str] | None = None,
    evidence_writer: Callable[[str, bytes], str] | None = None,
) -> dict[str, Any]:
    """执行受限的 Playwright 动作；浏览器缺失、页面不可达归环境失败。"""
    base: dict[str, Any] = {
        "case_id": case.get("id"),
        "case_type": "e2e",
        "interface_id": "UI-E2E-001",
        "expected": str(case.get("expected", "")),
        "timestamp": _now_iso(),
        "assertion_scope": "ui_steps",
    }
    for key in ("requirement_id", "claim_id", "testpoint_id", "title"):
        if case.get(key):
            base[key] = str(case[key])
    if not any(str(step.get("action", "")).startswith("expect_") for step in case.get("steps", [])):
        return {**base, "passed": False, "outcome": "blocked", "execution_state": "not_executed",
                "failure_reason": "历史 E2E 用例缺少可执行断言，不能将动作完成判为业务通过"}
    started = time.monotonic()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context(ignore_https_errors=ignore_https_errors)
            page = await context.new_page()
            console_errors: list[str] = []
            request_failures: list[str] = []
            page.on("pageerror", lambda error: console_errors.append(str(error)[:400]))
            page.on(
                "console",
                lambda message: console_errors.append(str(message.text)[:400])
                if message.type == "error"
                else None,
            )
            page.on(
                "requestfailed",
                lambda request: request_failures.append(
                    f"{request.method} {_safe_browser_url(request.url)}"
                ),
            )
            if auth_headers:
                # Token/Cookie 只附加到 E2E 页面同源请求，避免凭据泄露到第三方资源或跳转地址。
                target = urlparse(base_url)
                target_origin = (target.scheme.lower(), target.netloc.lower())

                async def attach_same_origin_auth(route: Any) -> None:
                    request_url = urlparse(route.request.url)
                    request_origin = (request_url.scheme.lower(), request_url.netloc.lower())
                    if request_origin != target_origin:
                        await route.continue_()
                        return
                    headers = dict(await route.request.all_headers())
                    for name, value in auth_headers.items():
                        existing_name = next(
                            (key for key in headers if key.lower() == name.lower()), name
                        )
                        if name.lower() == "cookie" and existing_name in headers:
                            headers[existing_name] = f"{headers[existing_name]}; {value}"
                        else:
                            headers[existing_name] = value
                    await route.continue_(headers=headers)

                await page.route("**/*", attach_same_origin_auth)
            # E2E 单步骤不能默认等待 30 秒，否则错误定位器会令整个批次长时间无进度。
            page.set_default_timeout(10_000)
            try:
                for step in case.get("steps", []):
                    await _run_e2e_step(page, dict(step), base_url, runtime_values or {})
            finally:
                evidence: dict[str, Any] = {
                    "kind": "browser",
                    "page_url": _safe_browser_url(page.url),
                    "console_errors": console_errors[:20],
                    "request_failures": request_failures[:20],
                }
                try:
                    evidence["page_title"] = (await page.title())[:300]
                except PlaywrightError:
                    pass
                if evidence_writer:
                    try:
                        safe_case_id = re.sub(r"[^A-Za-z0-9_-]", "", str(case.get("id", "E2E")))
                        screenshot = await page.screenshot(full_page=True, timeout=5_000)
                        evidence["screenshot_ref"] = evidence_writer(
                            f"evidence/{safe_case_id}.png", screenshot
                        )
                    except Exception as exc:
                        evidence["capture_error"] = f"{type(exc).__name__}: {str(exc)[:250]}"
                base["evidence"] = evidence
                try:
                    await context.close()
                finally:
                    await browser.close()
    except PlaywrightTimeoutError as exc:
        base.update(
            {
                "passed": False,
                "failure_kind": "environment",
                "failure_reason": f"前端自动化超时（X-E2E-02）：{exc}",
                "timeout": True,
                "duration_ms": _elapsed_ms(started),
            }
        )
        return base
    except PlaywrightError as exc:
        base.update(
            {
                "passed": False,
                "failure_kind": "environment",
                "failure_reason": f"前端自动化环境失败（X-E2E-01）：{exc}",
                "timeout": False,
                "duration_ms": _elapsed_ms(started),
            }
        )
        return base
    except AssertionError as exc:
        base.update(
            {
                "passed": False,
                "failure_kind": "assertion",
                "failure_reason": str(exc),
                "timeout": False,
                "duration_ms": _elapsed_ms(started),
            }
        )
        return base
    base.update({"passed": True, "timeout": False, "duration_ms": _elapsed_ms(started)})
    return base


async def _run_e2e_step(
    page: Any, step: dict[str, str], base_url: str, runtime_values: dict[str, str]
) -> None:
    """解释白名单动作；定位器格式由 prompt 约束为 role/label/text/testid。"""
    action = step["action"]
    value = _resolve_e2e_value(step.get("value", ""), runtime_values)
    if action == "goto":
        await page.goto(f"{base_url}{value}", wait_until="domcontentloaded")
        return
    if action == "expect_url":
        if value not in page.url:
            raise AssertionError(f"页面 URL 为 {page.url}，预期包含 {value}")
        return
    selector = step["selector"]
    locator = _e2e_locator(page, selector)
    if action == "click":
        await locator.click()
    elif action == "fill":
        await locator.fill(value)
    elif action == "check":
        await locator.check()
    elif action == "select":
        await locator.select_option(value)
    elif action == "expect_text":
        text = await locator.inner_text()
        if value not in text:
            raise AssertionError(f"元素文本为 {text!r}，预期包含 {value!r}")


def _e2e_locator(page: Any, selector: str) -> Any:
    """仅支持可访问语义定位，禁止将模型输出直接作为 CSS/XPath 执行。"""
    if selector.startswith("text="):
        return page.get_by_text(selector.removeprefix("text="), exact=False)
    if selector.startswith("label="):
        return page.get_by_label(selector.removeprefix("label="), exact=False)
    if selector.startswith("placeholder="):
        return page.get_by_placeholder(selector.removeprefix("placeholder="), exact=False)
    if selector.startswith("testid="):
        return page.get_by_test_id(selector.removeprefix("testid="))
    if selector.startswith("role="):
        role, _, name = selector.removeprefix("role=").partition("[name=")
        if name.endswith("]"):
            name = name[:-1]
        return page.get_by_role(role, name=name or None, exact=False)
    raise AssertionError(f"不支持的 E2E 语义定位器：{selector}")


def _e2e_runtime_values(auth: dict[str, Any] | None) -> dict[str, str]:
    """认证信息只在运行时替换，不进入 E2E 用例产物。"""
    if not auth:
        return {}
    return {
        "{{AUTH_USERNAME}}": str(auth.get("username", "")),
        "{{AUTH_PASSWORD}}": str(auth.get("password", "")),
    }


def _resolve_e2e_value(value: str, runtime_values: dict[str, str]) -> str:
    if value in {"{{AUTH_USERNAME}}", "{{AUTH_PASSWORD}}"} and not runtime_values.get(value):
        raise AssertionError(f"E2E 用例需要认证配置中的 {value}，当前任务未提供")
    return runtime_values.get(value, value)


def _undocumented_normal_codes(
    case: dict[str, Any], contracts: list[dict[str, Any]]
) -> list[str]:
    """正常场景只发评审阶段有源码依据的编码；未获证实的取值留待修正。"""
    if case.get("category") != "normal":
        return []
    params = case.get("params")
    if not isinstance(params, dict):
        return []
    issues: list[str] = []
    for contract in contracts:
        status = contract.get("value_rule_status")
        if status == "coded_values":
            codes = {
                str(item.get("code"))
                for item in contract.get("documented_values") or []
                if isinstance(item, dict) and item.get("code")
            }
        elif status == "examples_only":
            codes = {str(code) for code in contract.get("example_codes") or []}
        else:
            continue
        if not codes:
            continue
        body = params.get(str(contract.get("parameter", "")))
        if not isinstance(body, dict):
            continue
        value = body.get(str(contract.get("field", "")))
        values = value if isinstance(value, list) else [value]
        if any(
            item is not None and str(item).strip() and str(item) not in codes
            for item in values
        ):
            issues.append(
                f"body.{contract.get('field')} 的正常场景取值没有源码编码依据"
            )
    return issues


async def _execute_case(
    case: dict[str, Any],
    interface: dict[str, Any],
    protocol: str,
    rest_ready: bool,
    rest_runner: Runner,
    demo_runner: Runner,
    degraded_protocols: dict[str, dict[str, Any]],
    auth_configured: bool = False,
    field_contracts: list[dict[str, Any]] | None = None,
    rest_probe_issue: str | None = None,
) -> dict[str, Any]:
    """执行单用例并组装 result 条目（失败二态在此判定）。"""
    base: dict[str, Any] = {
        "case_id": case.get("id"),
        "interface_id": case.get("interface_id"),
        "case_type": "api",
        "expected": str(case.get("expected", "")),
        **_case_trace_fields(case),
        "timestamp": _now_iso(),
    }
    if case.get("expectation_source"):
        base["expectation_source"] = case["expectation_source"]
    if case.get("oracle_status") == "unverified":
        base.update(
            {
                "assertion_scope": "unverified",
                "passed": False,
                "failure_kind": None,
                "failure_reason": "Code Review 未找到足够源码证据确认该用例的执行预期，未发起请求",
                "timeout": False,
                "duration_ms": 0,
                "skipped": True,
                "skipped_reason": "源码预期待确认",
            }
        )
        return base
    explicit_assertions = case.get("assertions")
    base["assertion_scope"] = (
        "framework_boundary" if case.get("oracle_basis") == "spring_required_body" else
        response_assertion_scope(explicit_assertions or legacy_response_assertions(str(case.get("expected", ""))))
    )
    # P0-05：先验证请求能否按接口契约构造，再决定是否访问被测服务。这样“缺少
    # fund_code/share_type”等编排问题不会被服务端 400/401/500 伪装成产品缺陷。
    normalized_case = materialize_declared_defaults(case, interface)
    parameter_messages = blocking_parameter_messages(normalized_case, interface)
    value_messages = _undocumented_normal_codes(normalized_case, field_contracts or [])
    parameter_messages.extend(value_messages)
    if parameter_messages:
        base.update(
            {
                "passed": False,
                "failure_kind": None,
                "failure_reason": "X-EXEC-05：请求前置校验失败；" + "；".join(parameter_messages),
                "timeout": False,
                "duration_ms": 0,
                "skipped": True,
                "skipped_reason": "取值未确认" if value_messages else "前置缺失",
            }
        )
        return base
    case = normalized_case
    raw_params = case.get("params")
    params: dict[str, Any] = dict(raw_params) if isinstance(raw_params, dict) else {}
    invoke_request = InvokeRequest(
        interface=interface, params=params, auth_mode=resolved_auth_mode(case),
        body_encoding=case.get("body_encoding"),
    )

    # R-GEN-03 特例：不可加载脚本跳过执行并计数（不静默）
    if case.get("loadable") is False:
        base.update(
            {
                "passed": False,
                "failure_kind": None,
                "failure_reason": "脚本不可加载（loadable=false，R-GEN-03）：跳过执行",
                "timeout": False,
                "skipped": True,
            }
        )
        return base

    # 路由（R-EXEC-01）：REST 真实执行；其余协议 Runner 未就绪 → Mock 演示降级（X-EXEC-03）
    if protocol == "rest":
        preview = getattr(rest_runner, "preview", None)
        if callable(preview):
            base["request"] = preview(invoke_request)
        if not rest_ready:
            base.update(
                {
                    "passed": False,
                    "failure_kind": "environment",
                    "failure_reason": rest_probe_issue or "被测服务不可达（X-EXEC-01，探测失败）",
                    "timeout": False,
                    "duration_ms": 0,
                }
            )
            return base
        runner = rest_runner
    else:
        if protocol not in degraded_protocols:
            degraded_protocols[protocol] = {
                "protocol": protocol,
                "mode": "脚本生成成功+Mock执行演示",
                "reason": "Runner 未就绪（X-EXEC-03），降级为 Mock 执行演示",
            }
        runner = demo_runner

    response = await runner.invoke(invoke_request)
    if response.request_info is not None:
        base["request"] = response.request_info
    signature = cast(dict[str, Any], interface.get("signature")) if isinstance(interface.get("signature"), dict) else {}
    base["evidence"] = {
        "kind": "http_response",
        "method": str(signature.get("method", "")),
        "path": str(signature.get("path", "")),
    }

    if not response.ok:
        base.update(
            {
                "passed": False,
                "failure_kind": "environment",
                "failure_reason": response.error or "环境失败",
                "timeout": response.timed_out,
                "duration_ms": response.duration_ms,
            }
        )
        return base

    if (protocol == "rest" and isinstance(response.body, str)
            and re.match(r"(?is)^\s*(?:<!doctype\s+html|<html\b)", response.body)):
        base.update({
            "passed": False, "failure_kind": "environment",
            "failure_reason": "目标接口返回 HTML 页面；请核对仓库 service_base_url 与接口路径前缀",
            "actual_status": response.status or 0,
            "actual_body": _safe_actual_body(response.body),
            "timeout": response.timed_out, "duration_ms": response.duration_ms,
        })
        return base

    # 断言判定（与 retest 同源）：状态码 + 结构化业务字段断言
    status = response.status or 0
    base["actual_status"] = status
    base["actual_body"] = _safe_actual_body(response.body)
    assertion_results = _response_assertion_results(
        str(case.get("expected", "")), response.body, case.get("assertions"), normalized_case.get("params")
    )
    if assertion_results:
        base["assertion_results"] = assertion_results
    if status in {401, 403} and _expects_success(str(case.get("expected", ""))):
        auth_hint = (
            "鉴权配置未提供或凭据未生效"
            if not auth_configured
            else "已配置鉴权但服务未接受凭据"
        )
        base.update(
            {
                "passed": False,
                "failure_kind": "environment",
                "failure_reason": f"HTTP {status}（X-EXEC-05：{auth_hint}，不计入产品缺陷）",
                "timeout": response.timed_out,
                "duration_ms": response.duration_ms,
            }
        )
        return base
    # X-EXEC-01 同类：HTTP 404 = 路由不存在（接口未部署或指向了别的服务地址）——
    # 归环境失败，不判缺陷（R-EXEC-03/R-DEF-01；业务级「资源不存在」由服务以
    # 4xx 业务码表达，不与 404 混淆）
    if status == 404 and not _expects_404(str(case.get("expected", ""))):
        base.update(
            {
                "passed": False,
                "failure_kind": "environment",
                "failure_reason": (
                    "HTTP 404 路由不存在（接口未部署或服务地址不符，X-EXEC-01 同类）"
                ),
                "timeout": response.timed_out,
                "duration_ms": response.duration_ms,
            }
        )
        return base
    if case.get("observe_unknown_contract"):
        base.update({
            "passed": False, "failure_kind": None, "skipped": False,
            "skipped_reason": "响应契约待确认", "execution_state": "observed_unjudged",
            "failure_reason": "请求已发送并保留实际响应；HTTP 映射缺少源码依据，仅作为观测，不反写预期或判定产品缺陷",
            "timeout": response.timed_out, "duration_ms": response.duration_ms,
        })
        return base
    is_pass, semantic_failures = _judge_assertion(
        str(case.get("expected", "")), status, response.body, case.get("assertions"), normalized_case.get("params")
    )
    dependency_contract = case.get("dependency_contract")
    if isinstance(dependency_contract, dict) and not dependency_contract.get("deployment_verified"):
        base["expectation_source"] = "local_dependency_bytecode"
        if not is_pass:
            base.update({
                "passed": False, "failure_kind": None,
                "failure_reason": (
                    "实际响应与本地 Maven 依赖字节码契约不符；部署依赖版本摘要尚未核对，"
                    "需先确认运行环境，暂不判定产品缺陷"
                ),
                "timeout": response.timed_out, "duration_ms": response.duration_ms,
                "skipped": False, "skipped_reason": "部署依赖版本待确认",
                "execution_state": "observed_unjudged",
            })
            return base
    if (case.get("oracle_basis") == "spring_required_body" and not is_pass
            and not case.get("environment_contract_confirmed")
            and not isinstance(dependency_contract, dict)):
        # The required body binding does not prove the gateway's HTTP mapping.
        # Retain the observed response without inventing a project contract.
        base.update({
            "passed": False, "failure_kind": None,
            "failure_reason": f"必填请求体的部署响应契约待确认（实测 HTTP {status}）；尚未判定业务缺陷",
            "timeout": response.timed_out, "duration_ms": response.duration_ms,
            "skipped": False, "skipped_reason": "框架响应待确认", "execution_state": "observed_unjudged",
        })
        return base
    entry: dict[str, Any] = {
        "passed": is_pass,
        "timeout": response.timed_out,
        "duration_ms": response.duration_ms,
    }
    if not is_pass:
        entry["failure_kind"] = "assertion"
        if semantic_failures:
            detail = ";".join(semantic_failures)
            entry["failure_reason"] = f"HTTP {status} 语义断言失败：{detail}"
        else:
            entry["failure_reason"] = f"HTTP {status}（语义断言：{case.get('expected', '')}）"
    base.update(entry)
    return base


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """程序化汇总（数值不经 LLM，execution.json Schema 口径）。"""
    def counts(items: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "total": len(items),
            "passed": sum(1 for item in items if item.get("passed") is True),
            "failed_assertion": sum(
                1
                for item in items
                if item.get("passed") is False and item.get("failure_kind") == "assertion"
            ),
            "failed_environment": sum(
                1
                for item in items
                if item.get("passed") is False and item.get("failure_kind") == "environment"
            ),
        }
        skipped_count = sum(1 for item in items if item.get("skipped") is True)
        if skipped_count:
            result["skipped"] = skipped_count
        result["actual_requests"] = sum((item.get("request") or {}).get("attempted") is True for item in items)
        result["observed_unjudged"] = sum(item.get("execution_state") == "observed_unjudged" for item in items)
        result["not_executed"] = sum(item.get("execution_state") == "not_executed" for item in items)
        result["reused_response"] = sum(item.get("execution_state") == "reused_response" for item in items)
        return result

    summary = counts(results)
    # 只有混入 E2E 结果时增加按类型分解，保持历史纯接口产物的精确兼容。
    if any(result.get("case_type") == "e2e" for result in results):
        summary["by_type"] = {
            "api": counts([result for result in results if result.get("case_type") != "e2e"]),
            "e2e": counts([result for result in results if result.get("case_type") == "e2e"]),
        }
    return summary


def _append_runtime_log(
    store: ArtifactStore,
    job_id: str,
    *,
    event: str,
    message: str,
    **fields: Any,
) -> None:
    """追加脱敏运行事件；执行中的前端可据此显示 E2E 实时进度。"""
    entry = redact_secrets({
        "timestamp": _now_iso(),
        "node": NODE_NAME,
        "event": event,
        "message": message,
        **fields,
    })
    path = store.job_dir(job_id) / "runtime_log.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _load_json_array(
    store: ArtifactStore, job_id: str, filename: str, key: str
) -> list[dict[str, Any]]:
    """读前置产物数组（缺失/结构不符 → X-ORCH-01）。"""
    try:
        payload = store.read_json(job_id, filename)
        entries = payload.get(key)
    except FileNotFoundError as exc:
        raise PipelineError(
            code=X_ORCH_01,
            message=f"前置产物 {filename} 缺失",
            node_id=NODE_NAME,
        ) from exc
    if not isinstance(entries, list):
        raise PipelineError(
            code=X_ORCH_01,
            message=f"前置产物 {filename} 缺 {key} 数组",
            node_id=NODE_NAME,
        )
    return [dict(entry) for entry in entries]


def _elapsed_ms(started: float) -> int:
    """耗时（毫秒）。"""
    return int((time.monotonic() - started) * 1000)


def _safe_actual_body(value: Any, depth: int = 0) -> Any:
    """Retain complete response evidence, redacting sensitive fields only."""
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if _is_sensitive_key(key)
                else _safe_actual_body(item, depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_actual_body(item, depth + 1) for item in value]
    return value


def _case_trace_fields(case: dict[str, Any]) -> dict[str, Any]:
    """Copy stable requirement lineage into execution evidence."""
    fields: dict[str, Any] = {
        key: str(case[key])
        for key in (
            "requirement_id", "claim_id", "testpoint_id", "title", "category",
            "oracle_id", "oracle_status",
        )
        if case.get(key)
    }
    if isinstance(case.get("oracle_evidence_refs"), list):
        fields["oracle_evidence_refs"] = [str(item) for item in case["oracle_evidence_refs"]]
    return fields


def _safe_browser_url(value: str) -> str:
    """Keep browser evidence to origin + path; query strings and fragments may contain secrets."""
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return parsed.path or ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（横切约定）。"""
    return datetime.now(UTC).isoformat()
