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
- 完成判据（D4）：允许有失败用例——失败二态明确即 completed。

演示态简化（如实声明）：断言仅到「调用层成功 + HTTP 2xx」层级，
`expected` 的语义断言留给断言引擎完善；Mock 演示 Runner 返回固定桩响应。
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from orchestrator.artifacts import ArtifactStore
from orchestrator.case_validation import blocking_parameter_messages, materialize_declared_defaults
from orchestrator.engine import Engine, JobState
from orchestrator.errors import X_ORCH_01, PipelineError
from orchestrator.security import redact_secrets

NODE_NAME = "auto_execute"

_DEFAULT_TIMEOUT_S = 10.0  # X-EXEC-02：单用例执行超时上限
_SENSITIVE_RESPONSE_KEYS = {"token", "access_token", "refresh_token", "password", "secret"}

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
    """从受控 dotted path 读取非空字符串，兼容包装层内嵌 JSON；不记录响应值。"""
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
    return current.strip() if isinstance(current, str) and current.strip() else None


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


@dataclass(frozen=True)
class InvokeRequest:
    """单用例执行请求：接口定义（签名/参数位置）+ 用例参数。"""

    interface: dict[str, Any]
    params: dict[str, Any]
    # inherit：使用任务登录会话；missing：不带认证；invalid：明确伪造认证。
    auth_mode: str = "inherit"
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
        signature = request.interface.get("signature") or {}
        method = str(signature.get("method", "GET")).upper()
        path = str(signature.get("path", "/"))
        if self._prefer_trailing_slash and path != "/" and not path.endswith("/"):
            path = f"{path}/"
        param_defs = {str(p.get("name")): p for p in request.interface.get("params") or []}
        query: dict[str, Any] = {}
        body: dict[str, Any] = {}
        headers: dict[str, str] = {}
        for name, value in request.params.items():
            param_def = param_defs.get(name) or {}
            position = str(param_def.get("in", "query"))
            # 扫描器无法展开 serializer 时，会留下 account_data/filter_params 等
            # unknown 包装参数。LLM 可能给对象或 JSON 字符串，均应展开真实请求体。
            decoded_value = _decode_unknown_body(value)
            unwrap_container = (
                str(param_def.get("type")) == "unknown" and isinstance(decoded_value, dict)
            )
            if position == "body":
                body.update(decoded_value) if unwrap_container else body.__setitem__(name, value)
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
                    inherited_headers = {"Authorization": "Token invalid-autotest-token"}
                merged = {**inherited_headers, **headers}
                response = await client.request(
                    method,
                    path,
                    params=query or None,
                    json=body or None,
                    headers=merged or None,
                )
                # P-12：Django APPEND_SLASH 301 补偿（同 method 重发一次）
                location = response.headers.get("location", "")
                if location and response.status_code == 301:
                    # Location 可能是绝对 URL（Django 默认）——只取路径部分比对
                    redirect_path = httpx.URL(location).path
                    if redirect_path.rstrip("/") == path.rstrip("/"):
                        response = await client.request(
                            method,
                            redirect_path,
                            params=query or None,
                            json=body or None,
                            headers=merged or None,
                        )
                # 部分 Django 部署由 Nginx 代理，POST 无尾斜杠不会收到 APPEND_SLASH
                # 的 301，而是被前置层直接 405。只对非根路径做一次同路径斜杠补偿。
                if response.status_code == 405 and path != "/" and not path.endswith("/"):
                    response = await client.request(
                        method,
                        f"{path}/",
                        params=query or None,
                        json=body or None,
                        headers=merged or None,
                    )
        except httpx.TimeoutException as exc:
            return InvokeResponse(
                ok=False,
                status=None,
                body=None,
                error=f"执行超时（X-EXEC-02）：{exc}",
                timed_out=True,
                duration_ms=_elapsed_ms(started),
            )
        except httpx.TransportError as exc:
            return InvokeResponse(
                ok=False,
                status=None,
                body=None,
                error=f"连接失败（X-EXEC-01）：{exc}",
                timed_out=False,
                duration_ms=_elapsed_ms(started),
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
        )


def _semantic_mismatches(expected: str, body: Any) -> list[str]:
    """最小语义断言（演示态）：解析 expected 中的「字段=值」等值对并比对响应体。

    约定（F-GEN 断言语义的最小实现，文档 docs/13 C-05）：
    - expected 可含分号/逗号分隔的「字段=值」对（如 "id=1;age=28"）；
    - 仅当响应体为对象且含该字段时比对；无等值对则不做语义检查（纯描述型 expected 不受影响）；
    - 数值按数值比、其余按字符串比；不匹配返回原因清单；
    - status 键由 _expected_status 单独处理（映射 HTTP 状态码，不比对响应体）。
    """
    if not isinstance(body, dict):
        return []
    mismatches: list[str] = []
    for token in expected.replace("；", ";").replace("，", ",").split(";"):
        token = token.strip()
        if "=" not in token:
            continue
        key, _, raw_value = token.partition("=")
        key = key.strip()
        raw_value = raw_value.strip()
        # 仅解析显式机器断言（field=value）。旧版中文描述中如“响应含
        # fund_code='000330'”不是完整 token，不能被误判为字段断言。
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", key):
            continue
        # HTTP 状态断言由 _expected_statuses 单独判定；它们不是响应体字段。
        if key in ("status", "status_class"):
            continue
        if key not in body:
            mismatches.append(f"字段 {key} 不在响应体中（期望 {raw_value}）")
            continue
        actual = body[key]
        try:
            if float(actual) != float(raw_value):
                mismatches.append(f"{key}={actual}（期望 {raw_value}）")
        except (TypeError, ValueError):
            if str(actual) != raw_value:
                mismatches.append(f"{key}={actual}（期望 {raw_value}）")
    return mismatches


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


def _judge_assertion(expected: str, status: int, body: Any) -> tuple[bool, list[str]]:
    """断言判定：精确/区间 HTTP 状态码 > 默认 2xx + 语义等值对。

    Returns:
        (是否通过, 语义失配清单)。
    """
    allowed_statuses, allowed_classes = _expected_statuses(expected)
    if allowed_statuses or allowed_classes:
        is_pass = status in allowed_statuses or status // 100 in allowed_classes
    else:
        is_pass = 200 <= status < 300
    if is_pass:
        semantic_failures = _semantic_mismatches(expected, body)
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
            try:
                async with httpx.AsyncClient(
                    base_url=base_url,
                    timeout=_DEFAULT_TIMEOUT_S,
                    transport=transport,
                    verify=verify,
                ) as client:
                    response = await client.request(
                        method,
                        path,
                        params=query or None,
                        json=body or None,
                        headers=auth_headers or None,
                    )
                payload: Any
                try:
                    payload = response.json()
                except ValueError:
                    payload = response.text
                passed, semantic_failures = _judge_assertion(
                    expected, response.status_code, payload
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


def _skipped_case_result(case: dict[str, Any], reason: str) -> dict[str, Any]:
    """夹具失败时生成不发请求的前置缺失结果。"""
    return {
        "case_id": case.get("id"),
        "interface_id": case.get("interface_id"),
        "expected": str(case.get("expected", "")),
        "timestamp": _now_iso(),
        "passed": False,
        "failure_kind": None,
        "failure_reason": reason,
        "timeout": False,
        "duration_ms": 0,
        "skipped": True,
        "skipped_reason": "前置缺失",
    }


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
) -> bool:
    """R-EXEC-02：REST 环境探活（GET /health，任何响应均视为服务存活）。"""
    try:
        async with httpx.AsyncClient(
            base_url=base_url, timeout=3.0, transport=transport, verify=verify
        ) as client:
            await client.get("/health")
    except (httpx.TimeoutException, httpx.TransportError):
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


def prefers_django_trailing_slash(auth: dict[str, Any] | None) -> bool:
    """登录 API 本身以 / 结尾时，按同一 Django 服务的路由口径执行 REST 用例。"""
    return any(path.endswith("/") for path in auth_exempt_paths(auth))


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

        # REST 证书策略独立于登录配置，避免「未配置登录 → 自签环境全量不可达」。
        auth_headers: dict[str, str] = dict(preauthenticated_headers or {})
        verify = not insecure_tls
        if auth and not auth_headers:
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
        rest_ready_by_url = {
            url: await probe_rest(url, transport, verify) for url in route_urls
        }
        rest_runners = {
            url: RestRunner(
                url,
                transport,
                auth_headers,
                verify,
                auth_exempt_paths(auth),
                prefers_django_trailing_slash(auth),
            )
            for url in route_urls
        }
        demo_runner = MockDemoRunner()

        results: list[dict[str, Any]] = []
        degraded_protocols: dict[str, dict[str, Any]] = {}
        fixture_records: list[dict[str, Any]] = []
        setup_block_reason: str | None = None
        fixture_configs = fixtures or []
        if fixture_configs:
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
                results.append(
                    await _execute_case(
                        case,
                        interface,
                        protocol,
                        rest_ready_by_url.get(route_url, False),
                        rest_runners[route_url],
                        demo_runner,
                        degraded_protocols,
                        auth_configured=bool(auth or preauthenticated_headers),
                    )
                )
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

        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "job_id": state.job_id,
            "results": results,
            "summary": _summarize(results),
        }
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
) -> dict[str, Any]:
    """执行受限的 Playwright 动作；浏览器缺失、页面不可达归环境失败。"""
    base: dict[str, Any] = {
        "case_id": case.get("id"),
        "case_type": "e2e",
        "interface_id": "UI-E2E-001",
        "expected": str(case.get("expected", "")),
        "timestamp": _now_iso(),
    }
    started = time.monotonic()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context(ignore_https_errors=ignore_https_errors)
            page = await context.new_page()
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
                await context.close()
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


async def _execute_case(
    case: dict[str, Any],
    interface: dict[str, Any],
    protocol: str,
    rest_ready: bool,
    rest_runner: Runner,
    demo_runner: Runner,
    degraded_protocols: dict[str, dict[str, Any]],
    auth_configured: bool = False,
) -> dict[str, Any]:
    """执行单用例并组装 result 条目（失败二态在此判定）。"""
    base: dict[str, Any] = {
        "case_id": case.get("id"),
        "interface_id": case.get("interface_id"),
        "expected": str(case.get("expected", "")),
        "timestamp": _now_iso(),
    }
    # P0-05：先验证请求能否按接口契约构造，再决定是否访问被测服务。这样“缺少
    # fund_code/share_type”等编排问题不会被服务端 400/401/500 伪装成产品缺陷。
    normalized_case = materialize_declared_defaults(case, interface)
    parameter_messages = blocking_parameter_messages(normalized_case, interface)
    if parameter_messages:
        base.update(
            {
                "passed": False,
                "failure_kind": None,
                "failure_reason": "X-EXEC-05：请求前置校验失败；" + "；".join(parameter_messages),
                "timeout": False,
                "duration_ms": 0,
                "skipped": True,
                "skipped_reason": "前置缺失",
            }
        )
        return base
    case = normalized_case
    raw_params = case.get("params")
    params: dict[str, Any] = dict(raw_params) if isinstance(raw_params, dict) else {}

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
        if not rest_ready:
            base.update(
                {
                    "passed": False,
                    "failure_kind": "environment",
                    "failure_reason": "被测服务不可达（X-EXEC-01，探测失败）",
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

    response = await runner.invoke(
        InvokeRequest(interface=interface, params=params, auth_mode=resolved_auth_mode(case))
    )

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

    # 断言判定（与 retest 同源）：status token > 2xx + 语义等值对（docs/13 C-05）
    status = response.status or 0
    base["actual_status"] = status
    base["actual_body"] = _safe_actual_body(response.body)
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
    is_pass, semantic_failures = _judge_assertion(
        str(case.get("expected", "")), status, response.body
    )
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
    """供 UI 展示的脱敏、截断响应副本，避免 token 或超长数据进入产物。"""
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {
            str(key): (
                "***"
                if str(key).lower() in _SENSITIVE_RESPONSE_KEYS
                else _safe_actual_body(item, depth + 1)
            )
            for key, item in list(value.items())[:40]
        }
    if isinstance(value, list):
        return [_safe_actual_body(item, depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return value[:2000] + ("…" if len(value) > 2000 else "")
    return value


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（横切约定）。"""
    return datetime.now(UTC).isoformat()
