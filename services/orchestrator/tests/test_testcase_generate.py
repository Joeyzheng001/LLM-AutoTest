"""testcase_generate 节点测试：用例生成全链路 + 参数一致性两轮校验 + 脚本渲染。

覆盖：成功渲染（TC 编号/py_compile/low_confidence）、R-GEN-02 违规重生成、
二次仍违规放弃（X-GEN-02）、零用例接口告警（X-GEN-01）、虚构接口（X-LLM-02）、跳序。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from helpers import requirement_review_payload
from orchestrator.artifacts import ArtifactStore
from orchestrator.engine import Engine
from orchestrator.errors import X_ORCH_02, PipelineError
from orchestrator.llm import LlmResult
from orchestrator.nodes import run_testcase_generate
from orchestrator.nodes.testcase_generate import _filter_param_violations, _parse_e2e_cases
from orchestrator.pipeline import NodeStatus
from orchestrator.prompts_loader import PromptSpec

_SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"
_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
_JOB = "job-3f2a1b8c-0000-4000-8000-000000000001"

_INTERFACES: list[dict[str, Any]] = [
    {
        "id": "IF-REST-001",
        "protocol": "rest",
        "signature": {"method": "GET", "path": "/api/v1/users", "summary": "查用户"},
        "params": [{"name": "id", "type": "long", "required": True, "in": "query"}],
        "code_location": {
            "repo": "user-service",
            "file": "UserController.java",
            "start_line": 30,
            "end_line": 45,
        },
        "is_fallback": False,
    },
    {
        "id": "IF-GRPC-001",
        "protocol": "grpc",
        "signature": {"service_method": "GetOrder", "summary": "查询订单"},
        "params": [
            {"name": "order_id", "type": "unknown", "required": True, "default": "ORDER-000"}
        ],
        "code_location": {
            "repo": "order-center",
            "file": "OrderGrpc.java",
            "start_line": 10,
            "end_line": 20,
        },
        "is_fallback": False,
    },
]

_LLM_GOOD = """{
  "cases": [
    {"interface_id": "IF-REST-001", "requirement_id": "REQ-001", "category": "normal",
     "title": "正常按 ID 查询用户", "params": {"id": 1},
     "expected": "返回 UserDTO 且 id=1"},
    {"interface_id": "IF-REST-001", "category": "boundary",
     "title": "id=0 边界查询", "params": {"id": 0},
     "expected": "返回空列表或 404"},
    {"interface_id": "IF-REST-001", "category": "exception",
     "title": "id 为负数", "params": {"id": -1},
     "expected": "错误码 INVALID_PARAM"},
    {"interface_id": "IF-GRPC-001", "requirement_id": "REQ-002", "category": "normal",
     "title": "正常查询订单", "params": {"order_id": "ORDER-001"},
     "expected": "返回订单详情含 status 字段"},
    {"interface_id": "IF-GRPC-001", "category": "boundary",
     "title": "order_id 为空串", "params": {"order_id": ""},
     "expected": "返回错误 NOT_FOUND"},
    {"interface_id": "IF-GRPC-001", "category": "exception",
     "title": "order_id 非法字符", "params": {"order_id": "@@@@"},
     "expected": "错误码 INVALID"}
  ]
}"""


class _StubLlm:
    """多轮可控 LLM 桩：按调用序返回预设文本。"""

    def __init__(self, outputs: list[str]) -> None:
        """预设各轮输出文本。"""
        self._outputs = outputs
        self.received: list[str] = []

    async def complete(self, node: str, prompt: PromptSpec, user_content: str) -> LlmResult:
        """记录调用并返回当轮预设文本。"""
        self.received.append(user_content)
        text = self._outputs[min(len(self.received), len(self._outputs)) - 1]
        return LlmResult(text=text, tokens=128, call_id=f"LLM-stub-{len(self.received):03d}")


@pytest.fixture()
def env(
    tmp_path: Path,
) -> tuple[Engine, ArtifactStore, ArtifactStore]:
    """引擎 + orchestrator 产物卷（前置）+ test-executor 产物卷（节点写权）。"""
    store = ArtifactStore(tmp_path, _SCHEMAS, "orchestrator")
    te_store = ArtifactStore(tmp_path, _SCHEMAS, "test-executor")
    engine = Engine(store)
    state = engine.init_job(_JOB)
    store.write(_JOB, "requirement_review.json", requirement_review_payload(_JOB))
    payloads: list[tuple[str, str, dict[str, Any] | None]] = [
        (
            "req_parse",
            "requirement_items.json",
            {
                "schema_version": "1.0",
                "job_id": _JOB,
                "items": [
                    {"id": "REQ-001", "title": "用户查询", "description": "按 ID 查询用户"},
                    {"id": "REQ-002", "title": "订单查询", "description": "按单号查询订单"},
                ],
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
                        "name": "order-center",
                        "path": "/data/repos/order-center",
                        "branch": "master",
                        "is_fallback": False,
                        "status": "ok",
                        "file_count": 1,
                    },
                ],
            },
        ),
        (
            "interface_reconstruct",
            "interface_candidates.json",
            {
                "schema_version": "1.0",
                "job_id": _JOB,
                "candidates": [
                    {
                        "candidate_id": "CAND-001",
                        "protocol": "rest",
                        "signature": "GET /api/v1/users",
                        "code_location": _INTERFACES[0]["code_location"],
                        "is_fallback": False,
                    },
                    {
                        "candidate_id": "CAND-002",
                        "protocol": "grpc",
                        "signature": "OrderService/GetOrder",
                        "code_location": _INTERFACES[1]["code_location"],
                        "is_fallback": False,
                    },
                ],
            },
        ),
        (
            "interface_reconstruct",
            "interfaces.json",
            {"schema_version": "1.0", "job_id": _JOB, "interfaces": _INTERFACES},
        ),
        (
            "code_review",
            "review_mapping.json",
            {
                "schema_version": "1.0",
                "job_id": _JOB,
                "mappings": [
                    {
                        "requirement_id": "REQ-001",
                        "interface_id": "IF-REST-001",
                        "status": "implemented",
                        "confidence": 0.9,
                    },
                    {
                        "requirement_id": "REQ-002",
                        "interface_id": "IF-GRPC-001",
                        "status": "partial",
                        "confidence": 0.6,
                    },
                ],
                "risks": [
                    {
                        "requirement_id": "REQ-002",
                        "interface_id": "IF-GRPC-001",
                        "description": "部分实现",
                    }
                ],
                "missing_features": [],
            },
        ),
    ]
    # 双产物节点（interface_reconstruct）：两产物都写完才能置完成
    for node_name, filename, payload in payloads:
        engine.mark_running(state, node_name)
        store.write(_JOB, filename, payload)
    engine.mark_completed(state, "req_parse")
    engine.mark_completed(state, "code_scan")
    engine.mark_completed(state, "interface_reconstruct")
    engine.mark_completed(state, "code_review")
    return engine, store, te_store


async def test_testcase_generate_success(env: tuple[Engine, ArtifactStore, ArtifactStore]) -> None:
    """成功：TC 编号、脚本渲染 py_compile 全过、unknown 参数标低置信度、completed。"""
    engine, store, te_store = env
    state = engine.open_job(_JOB)

    path = await run_testcase_generate(engine, state, te_store, _StubLlm([_LLM_GOOD]), _PROMPTS)

    assert path.is_file()
    payload = store.read_json(_JOB, "testcases.json")
    cases = payload["cases"]
    assert [c["id"] for c in cases] == [f"TC-{i:03d}" for i in range(1, 7)]
    # X-GEN-03：unknown 参数接口（IF-GRPC-001）用例标低置信度（代码判定）
    for case in cases:
        if case["interface_id"] == "IF-GRPC-001":
            assert case["low_confidence"] is True
        else:
            assert "low_confidence" not in case
    # 脚本渲染 + 可加载（R-GEN-03）
    for case in cases:
        script = store.job_dir(_JOB) / case["script_path"]
        assert script.is_file()
        assert case["loadable"] is True
        source = script.read_text(encoding="utf-8")
        assert f"def test_{case['id'].replace('-', '_').lower()}(" in source
    # 无告警（两接口均有用例、参数全合规）
    assert "warnings" not in payload
    assert state.node("testcase_generate").status is NodeStatus.COMPLETED


async def test_testcase_generate_param_retry(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """R-GEN-02：一轮违规丢弃，二轮修复通过——无放弃告警。"""
    engine, store, te_store = env
    state = engine.open_job(_JOB)
    # 第一轮：REST 有清单外参数 pageSize；二轮修复
    bad_round = _LLM_GOOD.replace('"params": {"id": 1}', '"params": {"id": 1, "pageSize": 20}')
    llm = _StubLlm([bad_round, _LLM_GOOD])

    await run_testcase_generate(engine, state, te_store, llm, _PROMPTS)

    assert len(llm.received) == 2  # 重生成确实发生
    assert "违规" in llm.received[1] or "参数名" in llm.received[1]
    payload = store.read_json(_JOB, "testcases.json")
    # 无 X-GEN-02 放弃告警
    assert not any("X-GEN-02" in w for w in payload.get("warnings", []))


def test_filter_rejects_semantic_placeholder_for_unknown_body() -> None:
    """unknown REST body 的文字标签不能伪装成可执行请求。"""
    interfaces = [
        {
            "id": "IF-REST-001",
            "params": [{"name": "body", "type": "unknown", "in": "body"}],
        }
    ]
    bad = [{"interface_id": "IF-REST-001", "params": {"body": "valid_payload"}}]
    valid, violations = _filter_param_violations(bad, interfaces)
    assert valid == []
    assert "禁止语义占位符" in violations[0]["message"]

    concrete = [{"interface_id": "IF-REST-001", "params": {"body": {"id": 1}}}]
    valid, violations = _filter_param_violations(concrete, interfaces)
    assert valid == concrete
    assert violations == []

    # gRPC/非 REST 容器的 unknown 标量字段仍是合法参数，不能被误拦截。
    grpc = [{"id": "IF-GRPC-001", "params": [{"name": "order_id", "type": "unknown"}]}]
    valid, violations = _filter_param_violations(
        [{"interface_id": "IF-GRPC-001", "params": {"order_id": "ORDER-001"}}], grpc
    )
    assert len(valid) == 1
    assert violations == []


async def test_testcase_generate_abandon_interface(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """X-GEN-02：二轮仍违规 → 该接口放弃记告警；另一接口用例保留。"""
    engine, store, te_store = env
    state = engine.open_job(_JOB)
    # 两轮 REST 用例都带清单外参数
    bad = _LLM_GOOD.replace('"params": {"id": 1}', '"params": {"id": 1, "pageSize": 20}')
    llm = _StubLlm([bad, bad])

    await run_testcase_generate(engine, state, te_store, llm, _PROMPTS)

    payload = store.read_json(_JOB, "testcases.json")
    assert any("X-GEN-02" in w and "IF-REST-001" in w for w in payload["warnings"])
    # X-GEN-02：整接口放弃（非部分保留）——REST 无用例，仅 GRPC 保留
    assert all(c["interface_id"] == "IF-GRPC-001" for c in payload["cases"])
    assert len(payload["cases"]) == 3


async def test_testcase_generate_skip_interface_warning(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """X-GEN-01：某接口零用例 → 告警跳过，节点仍完成。"""
    engine, store, te_store = env
    state = engine.open_job(_JOB)
    # 只产 GRPC 用例（REST 漏产）
    grpc_only = """{
      "cases": [
        {"interface_id": "IF-GRPC-001", "requirement_id": "REQ-002", "category": "normal",
         "title": "正常查询订单", "params": {"order_id": "ORDER-001"},
         "expected": "返回订单详情含 status 字段"},
        {"interface_id": "IF-GRPC-001", "category": "boundary",
         "title": "order_id 为空串", "params": {"order_id": ""},
         "expected": "返回错误 NOT_FOUND"},
        {"interface_id": "IF-GRPC-001", "category": "exception",
         "title": "order_id 非法字符", "params": {"order_id": "@@@@"},
         "expected": "错误码 INVALID"}
      ]
    }"""

    await run_testcase_generate(engine, state, te_store, _StubLlm([grpc_only]), _PROMPTS)

    payload = store.read_json(_JOB, "testcases.json")
    assert any("X-GEN-01" in w and "IF-REST-001" in w for w in payload["warnings"])
    assert state.node("testcase_generate").status is NodeStatus.COMPLETED


async def test_testcase_generate_all_abandoned(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """全部接口用例失败：X-GEN-02 节点失败。"""
    engine, _store, te_store = env
    state = engine.open_job(_JOB)
    # 两轮所有用例都带清单外参数（两接口全类目违规）
    bad = (
        _LLM_GOOD.replace('"params": {"id": 1}', '"params": {"id": 1, "pageSize": 20}')
        .replace('"params": {"id": 0}', '"params": {"id": 0, "pageSize": 20}')
        .replace('"params": {"id": -1}', '"params": {"id": -1, "pageSize": 20}')
        .replace('"params": {"order_id": "ORDER-001"}', '"params": {"order_id": "X", "extra": 1}')
        .replace('"params": {"order_id": ""}', '"params": {"order_id": "", "extra": 1}')
        .replace('"params": {"order_id": "@@@@"}', '"params": {"order_id": "@", "extra": 1}')
    )
    llm = _StubLlm([bad, bad])

    with pytest.raises(PipelineError) as err:
        await run_testcase_generate(engine, state, te_store, llm, _PROMPTS)

    assert err.value.code == "X-GEN-02"
    assert state.node("testcase_generate").status is NodeStatus.PENDING


async def test_testcase_generate_fabricated_interface(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """虚构 interface_id：X-LLM-02。"""
    engine, _store, te_store = env
    state = engine.open_job(_JOB)
    bad = _LLM_GOOD.replace('"IF-REST-001"', '"IF-REST-999"')

    with pytest.raises(PipelineError) as err:
        await run_testcase_generate(engine, state, te_store, _StubLlm([bad]), _PROMPTS)

    assert err.value.code == "X-LLM-02"


async def test_testcase_generate_auth_context(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """C-17：auth 凭据进生成上下文（登录类正向用例可真实通过）。"""
    engine, _store, te_store = env
    state = engine.open_job(_JOB)
    llm = _StubLlm([_LLM_GOOD])

    await run_testcase_generate(
        engine,
        state,
        te_store,
        llm,
        _PROMPTS,
        auth={
            "login_url": "https://svc/api/auth/login/",
            "username": "admin",
            "password": "admin123",
            "insecure_tls": True,
        },
    )

    import json as _json

    context = _json.loads(llm.received[0])
    assert context["auth"] == {
        "login_url": "https://svc/api/auth/login/",
        "username": "admin",
        "password": "admin123",
    }  # insecure_tls 不进 LLM 上下文（与用例设计无关）


async def test_testcase_generate_no_auth_context(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """无 auth：上下文不含该键（旧行为不变）。"""
    engine, _store, te_store = env
    state = engine.open_job(_JOB)
    llm = _StubLlm([_LLM_GOOD])

    await run_testcase_generate(engine, state, te_store, llm, _PROMPTS)

    import json as _json

    context = _json.loads(llm.received[0])
    assert "auth" not in context


def test_e2e_expect_url_does_not_require_selector() -> None:
    """expect_url 是页面级断言，Prompt 约定 selector 为空，不能被元素定位校验拒绝。"""
    cases = _parse_e2e_cases(
        '{"cases":[{"requirement_id":"REQ-001","title":"登录",'
        '"expected":"跳转首页","steps":['
        '{"action":"goto","value":"/login"},'
        '{"action":"expect_url","selector":"","value":"/home"}]}]}'
    )
    assert cases[0]["steps"][-1] == {"action": "expect_url", "value": "/home"}


async def test_testcase_generate_skip_order_guard(
    env: tuple[Engine, ArtifactStore, ArtifactStore],
) -> None:
    """code_review 未完成：X-ORCH-02。"""
    engine, _store, te_store = env
    state = engine.open_job(_JOB)
    state.node("code_review").status = NodeStatus.PENDING

    with pytest.raises(PipelineError) as err:
        await run_testcase_generate(engine, state, te_store, _StubLlm([_LLM_GOOD]), _PROMPTS)

    assert err.value.code == X_ORCH_02
