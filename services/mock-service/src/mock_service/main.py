"""Deterministic HTTP/JSON stubs and the fixture control API (F-EXEC-06)."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

_SCENARIO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}


class StrictModel(BaseModel):
    """Reject misspelled fixture fields instead of silently ignoring them."""

    model_config = ConfigDict(extra="forbid")


class RequestMatch(StrictModel):
    """Optional subset matchers; every configured key must match the request."""

    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any | None = None


class StubResponse(StrictModel):
    """JSON response sent when a scenario matches."""

    status_code: int = Field(default=200, ge=200, le=599)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = Field(default_factory=dict)

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        for name, value in headers.items():
            has_control_character = any(
                (ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value
            )
            if not _HEADER_NAME.fullmatch(name) or has_control_character:
                raise ValueError("响应头名称格式非法，且名称和值均不得包含换行符")
            if name.lower() in {"content-length", "transfer-encoding", "connection"}:
                raise ValueError(f"不允许手动设置传输控制响应头：{name}")
        return headers

    @field_validator("body")
    @classmethod
    def validate_json_body(cls, body: Any) -> Any:
        try:
            json.dumps(body, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("响应 body 必须是可 JSON 序列化的值") from exc
        return body


class ScenarioSpec(StrictModel):
    """One exact method/path stub with optional request selectors."""

    method: str = Field(min_length=1, max_length=16)
    path: str = Field(min_length=1, max_length=2048)
    match: RequestMatch = Field(default_factory=RequestMatch)
    priority: int = Field(default=0, ge=-1000, le=1000)
    response: StubResponse

    @field_validator("method")
    @classmethod
    def normalize_method(cls, method: str) -> str:
        normalized = method.upper()
        if normalized not in _HTTP_METHODS:
            raise ValueError(f"不支持的 HTTP 方法：{normalized}")
        return normalized

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        if (
            not path.startswith("/")
            or path.startswith("//")
            or ".." in path.split("/")
            or "?" in path
            or "#" in path
        ):
            raise ValueError("path 必须是以 / 开头且不含上跳路径的绝对路径")
        if path.startswith("/__admin") or path == "/health":
            raise ValueError("path 不能覆盖 mock 服务的管理或健康检查路径")
        return path


@dataclass
class StoredScenario:
    name: str
    spec: ScenarioSpec
    hits: int = 0


app = FastAPI(
    title="LLM-AutoTest Mock Service",
    version="0.1.0",
    description=(
        "用于确定性模拟下游 HTTP/JSON 依赖。通过 fixture 管理 API 安装和删除场景，"
        "被测服务需将其下游地址指向本服务。"
    ),
)
_scenarios: dict[str, StoredScenario] = {}
_scenarios_lock = asyncio.Lock()


def _is_subset(expected: Any, actual: Any) -> bool:
    """Match configured JSON objects recursively; arrays and scalars match exactly."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _is_subset(value, actual[key]) for key, value in expected.items()
        )
    return bool(expected == actual)


def _specificity(spec: ScenarioSpec) -> int:
    """Count configured selector leaves so more constrained stubs win ties."""

    def count(value: Any) -> int:
        if isinstance(value, dict):
            return sum(count(item) for item in value.values()) or 1
        return 1

    return (
        len(spec.match.query)
        + len(spec.match.headers)
        + (count(spec.match.body) if "body" in spec.match.model_fields_set else 0)
    )


def _query_values(request: Request) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in request.query_params.keys():
        found = request.query_params.getlist(key)
        values[key] = found[0] if len(found) == 1 else found
    return values


def _matches(
    scenario: StoredScenario,
    method: str,
    path: str,
    query: dict[str, Any],
    headers: dict[str, str],
    body: Any,
) -> bool:
    spec = scenario.spec
    if spec.method != method or spec.path != path:
        return False
    if not all(
        key in query and _is_subset(value, query[key]) for key, value in spec.match.query.items()
    ):
        return False
    if not all(
        key.lower() in headers and headers[key.lower()] == value
        for key, value in spec.match.headers.items()
    ):
        return False
    return "body" not in spec.match.model_fields_set or _is_subset(spec.match.body, body)


@app.get("/health")
async def health() -> dict[str, str]:
    """Container health probe."""
    return {"status": "ok", "service": "mock-services"}


@app.get("/__admin/scenarios")
async def list_scenarios() -> dict[str, Any]:
    """List installed stubs without returning configured response bodies."""
    async with _scenarios_lock:
        scenarios = [
            {
                "name": item.name,
                "method": item.spec.method,
                "path": item.spec.path,
                "match": {
                    "query_keys": sorted(item.spec.match.query),
                    "header_names": sorted(item.spec.match.headers),
                    "has_body": "body" in item.spec.match.model_fields_set,
                },
                "priority": item.spec.priority,
                "hits": item.hits,
            }
            for item in sorted(_scenarios.values(), key=lambda scenario: scenario.name)
        ]
    return {"scenarios": scenarios, "count": len(scenarios)}


@app.put("/__admin/scenarios/{name}", response_model=None)
async def put_scenario(name: str, spec: ScenarioSpec) -> dict[str, Any] | JSONResponse:
    """Create or replace a named scenario; idempotent setup is safe to retry."""
    if not _SCENARIO_NAME.fullmatch(name):
        return JSONResponse(
            status_code=422,
            content={"code": "invalid_scenario_name", "message": "场景名称格式不合法"},
        )
    async with _scenarios_lock:
        _scenarios[name] = StoredScenario(name=name, spec=spec)
    return {"name": name, "installed": True}


@app.delete("/__admin/scenarios/{name}")
async def delete_scenario(name: str) -> dict[str, Any]:
    """Delete one scenario; repeated teardown is idempotent."""
    async with _scenarios_lock:
        removed = _scenarios.pop(name, None) is not None
    return {"name": name, "deleted": removed}


async def _read_json_body(request: Request) -> tuple[Any, Response | None]:
    raw_body = await request.body()
    if not raw_body:
        return None, None
    try:
        return json.loads(raw_body), None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, JSONResponse(
            status_code=400,
            content={"code": "invalid_json", "message": "mock 服务只接受合法 JSON 请求体"},
        )


@app.api_route(
    "/{path:path}",
    methods=sorted(_HTTP_METHODS),
    include_in_schema=False,
)
async def invoke_stub(request: Request, path: str) -> Response:
    """Resolve one exact route and return its configured JSON response."""
    del path  # request.url.path preserves the path as received by the ASGI server.
    body, error = await _read_json_body(request)
    if error is not None:
        return error

    method = request.method.upper()
    request_path = request.url.path
    query = _query_values(request)
    headers = {key.lower(): value for key, value in request.headers.items()}
    async with _scenarios_lock:
        candidates = [
            item
            for item in _scenarios.values()
            if _matches(item, method, request_path, query, headers, body)
        ]
        if not candidates:
            return JSONResponse(
                status_code=404,
                content={
                    "code": "mock_scenario_not_found",
                    "message": "没有已安装的 mock 场景匹配该请求",
                    "method": method,
                    "path": request_path,
                },
            )

        candidates.sort(
            key=lambda item: (
                item.spec.priority,
                _specificity(item.spec),
                item.name,
            ),
            reverse=True,
        )
        selected = candidates[0]
        best_rank = (selected.spec.priority, _specificity(selected.spec))
        equally_ranked = [
            item
            for item in candidates
            if (item.spec.priority, _specificity(item.spec)) == best_rank
        ]
        if len(equally_ranked) > 1:
            return JSONResponse(
                status_code=409,
                content={
                    "code": "mock_scenario_ambiguous",
                    "message": "多个 mock 场景同优先级且同等具体，无法唯一选择",
                    "scenarios": sorted(item.name for item in equally_ranked),
                },
            )
        selected.hits += 1
        response = selected.spec.response

    if response.status_code in {204, 205, 304}:
        return Response(status_code=response.status_code, headers=response.headers)
    return JSONResponse(
        status_code=response.status_code,
        headers=response.headers,
        content=response.body,
    )
