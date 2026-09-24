"""interface_reconstruct 节点测试：R-REV-02 两步法全链路。

覆盖：静态候选落盘 + LLM 合并成功、溯源字段代码回填（LLM 谎报被忽略）、
候选 ID 规范化、零候选（X-REV-04）、LLM 虚构候选/坏 JSON（X-LLM-02）、跳序防护。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from common.artifacts import ArtifactStore
from common.engine import Engine
from common.errors import X_ORCH_02, PipelineError
from common.llm import LlmResult
from common.pipeline import NodeStatus
from common.prompts_loader import PromptSpec

from helpers import requirement_review_payload
from orchestrator.javaparser_client import JavaParserClient, RepositoryInput
from orchestrator.nodes import run_interface_reconstruct
from orchestrator.nodes.interface_reconstruct import (
    _collect_changed_files,
    _filter_candidates_by_scope,
)

_SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"
_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
_JOB = "job-3f2a1b8c-0000-4000-8000-000000000001"

_REPOS = [
    RepositoryInput(name="user-service", path="/data/repos/user-service", branch="release/v2.3"),
    RepositoryInput(
        name="fallback-demo", path="/data/repos/fallback-demo", branch="master", is_fallback=True
    ),
]

# 契约响应：第二条缺 candidate_id（触发规范化），且为兜底仓库候选（D5 透传）
_EXTRACT_RESPONSE: dict[str, Any] = {
    "job_id": "extract-0001",
    "status": "completed",
    "candidates": [
        {
            "candidate_id": "CAND-001",
            "protocol": "rest",
            "signature": "GET /api/v1/users",
            "code_location": {
                "repo": "user-service",
                "file": "UserController.java",
                "start_line": 30,
                "end_line": 45,
            },
            "snippet": "@GetMapping(\"/users\") public List<UserDTO> list(@RequestParam long id)",
            "is_fallback": False,
            "confidence": 0.9,
        },
        {
            "protocol": "grpc",
            "signature": "OrderService/GetOrder",
            "code_location": {
                "repo": "fallback-demo",
                "file": "OrderGrpc.java",
                "start_line": 10,
                "end_line": 20,
            },
            "snippet": "rpc GetOrder (GetOrderRequest) returns (GetOrderReply)",
            "is_fallback": True,
            "confidence": 0.7,
        },
    ],
    "warnings": ["X-REV-01: /bad/Syntax.java 解析失败已跳过"],
}

_LLM_GOOD = """```json
{
  "interfaces": [
    {
      "candidate_id": "CAND-001",
      "signature": {"method": "GET", "path": "/api/v1/users", "summary": "按 ID 查询用户"},
      "params": [{"name": "id", "type": "long", "required": true, "in": "query"}],
      "returns": {"type": "UserDTO", "description": "用户视图"},
      "is_fallback": false,
      "code_location": {"repo": "LLM-谎报", "file": "fake.java", "start_line": 1, "end_line": 2}
    },
    {
      "candidate_id": "CAND-002",
      "signature": {"service_method": "GetOrder", "summary": "查询订单"},
      "params": [{"name": "order_id", "type": "unknown", "required": true, "default": "ORDER-000"}]
    }
  ]
}
```"""


class _StubLlm:
    """可控 LLM 桩。"""

    def __init__(self, text: str) -> None:
        """预设输出文本。"""
        self._text = text
        self.received: list[tuple[str, str]] = []

    async def complete(self, node: str, prompt: PromptSpec, user_content: str) -> LlmResult:
        """记录调用并返回预设文本。"""
        self.received.append((prompt.ref, user_content))
        return LlmResult(text=self._text, tokens=64, call_id="LLM-stub-002")


def _extract_handler(candidates: list[Any] | None = None) -> Any:
    """模拟 java-parser extract-jobs：POST 受理 → GET 直接 completed。"""
    body = {"job_id": "extract-0001", "status": "queued"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/extract-jobs":
            return httpx.Response(202, json=body)
        if request.method == "GET" and request.url.path == "/api/v1/extract-jobs/extract-0001":
            payload = dict(_EXTRACT_RESPONSE)
            if candidates is not None:
                payload["candidates"] = candidates
            return httpx.Response(200, json=payload)
        raise AssertionError(f"非预期请求：{request.method} {request.url.path}")

    return handler


def _client(handler: Any) -> JavaParserClient:
    """构造注入 MockTransport 的客户端（测试专用）。"""
    return JavaParserClient(
        "http://java-parser:8000", poll_interval_s=0.01, transport=httpx.MockTransport(handler)
    )


def _ready_env(tmp_path: Path) -> tuple[Engine, ArtifactStore]:
    """引擎 + 产物卷 + req_parse/code_scan 已完成的前置状态。"""
    store = ArtifactStore(tmp_path, _SCHEMAS, "orchestrator")
    engine = Engine(store)
    state = engine.init_job(_JOB)
    store.write(_JOB, "requirement_review.json", requirement_review_payload(_JOB))
    for node_name, filename, payload in (
        (
            "req_parse",
            "requirement_items.json",
            {
                "schema_version": "1.0",
                "job_id": _JOB,
                "source_file": "/data/docs/需求说明.md",
                "items": [{"id": "REQ-001", "title": "用户查询", "description": "按 ID 查询用户"}],
            },
        ),
        (
            "code_scan",
            "code_inventory.json",
            {
                "schema_version": "1.0",
                "job_id": _JOB,
                "repositories": [
                    {
                        "name": "user-service",
                        "path": "/data/repos/user-service",
                        "branch": "release/v2.3",
                        "is_fallback": False,
                        "status": "ok",
                        "file_count": 1,
                    },
                    {
                        "name": "fallback-demo",
                        "path": "/data/repos/fallback-demo",
                        "branch": "master",
                        "is_fallback": True,
                        "status": "ok",
                        "file_count": 1,
                    },
                ],
            },
        ),
    ):
        engine.mark_running(state, node_name)
        store.write(_JOB, filename, payload)
        engine.mark_completed(state, node_name)
    return engine, store


async def test_interface_reconstruct_success(tmp_path: Path) -> None:
    """两步法成功：候选入库 + LLM 合并；溯源字段代码回填（LLM 谎报被忽略）。"""
    engine, store = _ready_env(tmp_path)
    state = engine.open_job(_JOB)
    llm = _StubLlm(_LLM_GOOD)

    path = await run_interface_reconstruct(
        engine, state, store, _client(_extract_handler()), llm, _PROMPTS, _REPOS
    )

    assert path.is_file()
    # 第①步产物：候选规范化（缺 ID 的第二条 → CAND-002）+ 告警透传 + X-REV-04 弱口径
    cands = store.read_json(_JOB, "interface_candidates.json")
    assert [c["candidate_id"] for c in cands["candidates"]] == ["CAND-001", "CAND-002"]
    assert "X-REV-01: /bad/Syntax.java 解析失败已跳过" in cands["warnings"]
    assert any("X-REV-04" in w for w in cands["warnings"])

    # 第②步产物：ID 分配、溯源回填
    interfaces = store.read_json(_JOB, "interfaces.json")
    assert [i["id"] for i in interfaces["interfaces"]] == ["IF-REST-001", "IF-GRPC-001"]
    # LLM 谎报的 code_location 被忽略，回填候选真实位置
    assert interfaces["interfaces"][0]["code_location"]["repo"] == "user-service"
    # is_fallback 从候选回填（LLM 输出 false 也不生效）
    assert interfaces["interfaces"][1]["is_fallback"] is True
    assert interfaces["interfaces"][1]["confidence"] == 0.7
    # LLM 语义字段保留（含 X-REV-02 unknown 口径）
    assert interfaces["interfaces"][0]["params"][0]["in"] == "query"
    assert interfaces["interfaces"][1]["params"][0]["type"] == "unknown"

    assert state.node("interface_reconstruct").status is NodeStatus.COMPLETED
    # LLM 入参是候选 JSON 全文（含 snippet 与关联上下文，供语义解析）
    _, user_content = llm.received[0]
    assert "CAND-001" in user_content
    assert "@GetMapping" in user_content


async def test_interface_reconstruct_zero_candidates(tmp_path: Path) -> None:
    """零候选：X-REV-04 节点失败。"""
    engine, store = _ready_env(tmp_path)
    state = engine.open_job(_JOB)

    with pytest.raises(PipelineError) as err:
        await run_interface_reconstruct(
            engine,
            state,
            store,
            _client(_extract_handler(candidates=[])),
            _StubLlm(_LLM_GOOD),
            _PROMPTS,
            _REPOS,
        )

    assert err.value.code == "X-REV-04"
    assert state.node("interface_reconstruct").status is NodeStatus.PENDING


async def test_interface_reconstruct_llm_fabricated_candidate(tmp_path: Path) -> None:
    """LLM 虚构候选 ID：X-LLM-02，不落 interfaces.json。"""
    engine, store = _ready_env(tmp_path)
    state = engine.open_job(_JOB)
    bad_llm = _StubLlm(
        '{"interfaces": [{"candidate_id": "CAND-999", "signature": {"summary": "虚构"}}]}'
    )

    with pytest.raises(PipelineError) as err:
        await run_interface_reconstruct(
            engine, state, store, _client(_extract_handler()), bad_llm, _PROMPTS, _REPOS
        )

    assert err.value.code == "X-LLM-02"
    assert not (store.job_dir(_JOB) / "interfaces.json").exists()


async def test_interface_reconstruct_llm_duplicate_candidate(tmp_path: Path) -> None:
    """LLM 对同一候选输出两条：X-LLM-02。"""
    engine, store = _ready_env(tmp_path)
    state = engine.open_job(_JOB)
    bad_llm = _StubLlm(
        '{"interfaces": ['
        '{"candidate_id": "CAND-001", "signature": {"summary": "a"}},'
        '{"candidate_id": "CAND-001", "signature": {"summary": "b"}}]}'
    )

    with pytest.raises(PipelineError) as err:
        await run_interface_reconstruct(
            engine, state, store, _client(_extract_handler()), bad_llm, _PROMPTS, _REPOS
        )

    assert err.value.code == "X-LLM-02"


async def test_interface_reconstruct_llm_bad_json(tmp_path: Path) -> None:
    """LLM 输出非 JSON：X-LLM-02。"""
    engine, store = _ready_env(tmp_path)
    state = engine.open_job(_JOB)

    with pytest.raises(PipelineError) as err:
        await run_interface_reconstruct(
            engine,
            state,
            store,
            _client(_extract_handler()),
            _StubLlm("这不是 JSON"),
            _PROMPTS,
            _REPOS,
        )

    assert err.value.code == "X-LLM-02"


async def test_interface_reconstruct_skip_order_guard(tmp_path: Path) -> None:
    """code_scan 未完成：X-ORCH-02 跳序拒绝。"""
    engine, store = _ready_env(tmp_path)
    state = engine.open_job(_JOB)
    state.node("code_scan").status = NodeStatus.PENDING

    with pytest.raises(PipelineError) as err:
        await run_interface_reconstruct(
            engine, state, store, _client(_extract_handler()), _StubLlm(_LLM_GOOD), _PROMPTS, _REPOS
        )

    assert err.value.code == X_ORCH_02
    assert state.node("interface_reconstruct").status is NodeStatus.PENDING


def test_collect_changed_files_groups_added_and_modified_by_repo() -> None:
    """变更文件按 repo 分组，added + modified 都纳入；removed 排除。"""
    inventory: dict[str, Any] = {
        "repositories": [
            {
                "name": "repo-a",
                "changed_files": {
                    "added": ["A.java"],
                    "modified": ["B.java"],
                    "removed": ["C.java"],
                },
            },
            {"name": "repo-b", "changed_files": {"modified": ["D.java"]}},
        ]
    }
    changed = _collect_changed_files(inventory)
    assert changed == {"repo-a": {"A.java", "B.java"}, "repo-b": {"D.java"}}


def test_collect_changed_files_returns_empty_without_scope() -> None:
    """无变更范围（缺 changed_files 或整个 inventory 空）→ 空 dict（全量）。"""
    assert _collect_changed_files({}) == {}
    assert _collect_changed_files({"repositories": [{"name": "r", "files": []}]}) == {}


def test_filter_candidates_by_scope_keeps_only_changed_files() -> None:
    """只保留 code_location.file 落在变更文件里的候选。"""
    candidates: list[dict[str, Any]] = [
        {"candidate_id": "CAND-001", "code_location": {"repo": "repo-a", "file": "A.java"}},
        {"candidate_id": "CAND-002", "code_location": {"repo": "repo-a", "file": "B.java"}},
        {"candidate_id": "CAND-003", "code_location": {"repo": "repo-a", "file": "Other.java"}},
        {"candidate_id": "CAND-004", "code_location": {"repo": "repo-b", "file": "D.java"}},
    ]
    changed = {"repo-a": {"A.java", "B.java"}, "repo-b": {"D.java"}}
    kept = _filter_candidates_by_scope(candidates, changed)
    assert [c["candidate_id"] for c in kept] == ["CAND-001", "CAND-002", "CAND-004"]


def test_filter_candidates_keeps_repos_without_scope() -> None:
    """未配变更范围的仓库全保留；配了变更范围的仓库才按文件过滤。"""
    candidates: list[dict[str, Any]] = [
        {"candidate_id": "CAND-001", "code_location": {"repo": "scoped", "file": "A.java"}},
        {"candidate_id": "CAND-002", "code_location": {"repo": "scoped", "file": "Other.java"}},
        {"candidate_id": "CAND-003", "code_location": {"repo": "unscoped", "file": "X.java"}},
        {"candidate_id": "CAND-004", "code_location": {"repo": "unscoped", "file": "Y.java"}},
    ]
    kept = _filter_candidates_by_scope(candidates, {"scoped": {"A.java"}})
    assert [c["candidate_id"] for c in kept] == ["CAND-001", "CAND-003", "CAND-004"]
