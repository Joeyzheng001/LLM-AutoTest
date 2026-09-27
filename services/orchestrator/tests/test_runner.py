"""PipelineRunner 端到端测试：9 节点串联 + 断点续跑 + 重试降级。

stub LLM 按节点返回预置 JSON；MockTransport 模拟 java-parser 与被测 REST 服务——
不经真实 LLM/网络，验证驱动纪律（顺序、降级、续跑）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from common.artifacts import ArtifactStore
from common.config import Settings
from common.engine import Engine
from common.llm.client import LlmResult
from common.pipeline import NodeStatus
from common.prompts_loader import PromptSpec

from orchestrator.javaparser_client import JavaParserClient, RepositoryInput
from orchestrator.nodes.code_review import SupportsComplete
from orchestrator.runner import PipelineRunner, repository_inputs_from_config

_SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"
_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
_JOB = "job-runner-0000-4000-8000-000000000001"

_REQ_ITEMS = {
    "items": [
        {
            "id": "REQ-001",
            "title": "用户查询",
            "description": "按 ID 查询用户，返回 UserDTO",
            "priority": "P1",
        }
    ]
}

_REQ_REVIEW = {
    "score": 80,
    "quality_grade": "B",
    "summary": "需求主流程清晰，可开始测试设计。",
    "dimension_scores": {
        "需求完整性": {"weight": 0.35, "score": 80, "reason": "范围明确"},
        "一致性与逻辑性": {"weight": 0.20, "score": 80, "reason": "流程一致"},
        "可测试性": {"weight": 0.25, "score": 80, "reason": "可验证"},
        "清晰度与规范性": {"weight": 0.10, "score": 80, "reason": "术语统一"},
        "风险与依赖描述": {"weight": 0.10, "score": 80, "reason": "依赖明确"},
    },
    "quality_assessment": {"grade": "B", "grade_name": "合格", "summary_reason": "可设计测试"},
    "issues": [],
    "readable_issue_points": [],
    "completeness_issues": [],
    "consistency_issues": [],
    "untestable_items": [],
    "risk_flags": [],
    "testable_features": ["用户查询"],
    "mindmap": {"主题": "测试思维导图", "子主题": []},
}

_INTERFACES = [
    {
        "candidate_id": "CAND-001",
        "protocol": "rest",
        "signature": {"method": "GET", "path": "/api/v1/users", "summary": "查用户"},
        "params": [{"name": "id", "type": "long", "required": True, "in": "query"}],
    }
]

_REVIEW = {
    "mappings": [
        {
            "requirement_id": "REQ-001",
            "interface_id": "IF-REST-001",
            "status": "implemented",
            "evidence": {"note": "Controller 命中"},
        }
    ]
}

_CASES = [
    {
        "interface_id": "IF-REST-001",
        "requirement_id": "REQ-001",
        "category": "normal",
        "title": "正常查询",
        "expected": "返回 UserDTO",
        "params": {"id": 1},
    },
    {
        "interface_id": "IF-REST-001",
        "requirement_id": "REQ-001",
        "category": "boundary",
        "title": "零值边界查询",
        "expected": "status=200；返回空结果或明确业务提示",
        "params": {"id": 0},
    },
    {
        "interface_id": "IF-REST-001",
        "requirement_id": "REQ-001",
        "category": "exception",
        "title": "非法 id",
        "expected": "400",
        "params": {"id": -1},
    },
]


class StubLlm:
    """按节点返回预置 JSON（覆盖 \\`json 围栏，验证解析容错）。"""

    def __init__(self, scripts: dict[str, str] | None = None) -> None:
        self.scripts = scripts
        self.calls: list[str] = []
        self.prompt_refs: list[str] = []

    async def complete(
        self,
        node: str,
        prompt: PromptSpec,
        user_content: str,
        max_attempts: int | None = None,
    ) -> LlmResult:
        """返回该节点的预置输出；max_attempts 由节点按调用场景下传，本桩忽略。"""
        _ = user_content
        _ = max_attempts
        self.calls.append(node)
        self.prompt_refs.append(prompt.ref)
        key = prompt.name if prompt.name == "requirement_review" else node
        text = (self.scripts or {}).get(key, "")
        text = "```json\n" + text + "\n```"
        return LlmResult(text=text, tokens=10, call_id=f"LLM-stub-{len(self.calls)}")


def _default_llm_scripts() -> dict[str, str]:
    return {
        "requirement_review": json.dumps(_REQ_REVIEW, ensure_ascii=False),
        "req_parse": json.dumps(_REQ_ITEMS, ensure_ascii=False),
        "interface_reconstruct": json.dumps({"interfaces": _INTERFACES}, ensure_ascii=False),
        "code_review": json.dumps(_REVIEW, ensure_ascii=False),
        "testcase_generate": json.dumps({"cases": _CASES}, ensure_ascii=False),
    }


def _java_parser_transport() -> httpx.MockTransport:
    """模拟 java-parser：scan/extract 均同步完成（completed 带完整契约产物）。"""

    code_inventory = {
        "schema_version": "1.0",
        "job_id": _JOB,
        "repositories": [
            {
                "name": "user-service",
                "path": "/var/repos/user-service",
                "branch": "main",
                "is_fallback": False,
                "status": "ok",
                "file_count": 1,
                # 变更范围必填：接口候选只取变更文件里的接口（C-09 扩展）
                "changed_files": {
                    "added": ["UserController.java"],
                    "modified": [],
                    "removed": [],
                },
            },
            {
                "name": "order-center",
                "path": "/var/repos/order-center",
                "branch": "master",
                "is_fallback": False,
                "status": "ok",
                "file_count": 1,
                # 该仓库本次无变更 → 空集；但「已配范围」这点必须体现，否则会被拦
                "changed_files": {"added": [], "modified": [], "removed": []},
            },
        ],
    }
    candidates = [
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
            "is_fallback": False,
            "confidence": 0.9,
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/scan-jobs":
            return httpx.Response(202, json={"job_id": "jp-scan", "status": "queued"})
        if request.url.path == "/api/v1/extract-jobs":
            return httpx.Response(202, json={"job_id": "jp-ext", "status": "queued"})
        if request.url.path == "/api/v1/scan-jobs/jp-scan":
            return httpx.Response(
                200,
                json={"job_id": "jp-scan", "status": "completed", "code_inventory": code_inventory},
            )
        if request.url.path == "/api/v1/extract-jobs/jp-ext":
            return httpx.Response(
                200,
                json={
                    "job_id": "jp-ext",
                    "status": "completed",
                    "candidates": candidates,
                    "warnings": [],
                },
            )
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "路径未模拟"})

    return httpx.MockTransport(handler)


def _rest_service_transport(status: int = 200) -> httpx.MockTransport:
    """模拟被测 REST 服务（/health 探活 + 业务端点固定状态码）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(status, json={"ok": status < 300})

    return httpx.MockTransport(handler)


class _ScriptedRunner(PipelineRunner):
    """注入 stub LLM 与 MockTransport 的 runner（测试构造便利）。"""

    def __init__(self, settings: Settings, tmp: Path, llm: StubLlm, rest_status: int = 200) -> None:
        orch = ArtifactStore(tmp, _SCHEMAS, "orchestrator")
        te = ArtifactStore(tmp, _SCHEMAS, "test-executor")
        engine = Engine(orch)
        java = JavaParserClient(
            base_url="http://java-parser:8081",
            transport=_java_parser_transport(),
            poll_interval_s=0.0,
            max_wait_s=1.0,
        )
        self.llm = llm
        self.rest_status = rest_status
        super().__init__(settings, engine, orch, te, java)
        # 测试注入：覆写传输层与 LLM（生产构造经 Settings）
        self._test_rest_transport = httpx.AsyncClient(
            transport=_rest_service_transport(rest_status)
        )._transport

    async def _invoke_node(  # type: ignore[no-untyped-def]
        self, node_name, state, llm, prompts_dir, doc_paths, repositories, rest_base_url,
        auth=None, auth_headers=None, e2e=None, document_pairs=None, rest_insecure_tls=True,
    ):
        """覆写：给 auto_execute/retest 注入 MockTransport（auth 透传 C-15）。"""
        from executor.nodes import (
            run_auto_execute,
            run_defect_list,
            run_retest_regression,
            run_testcase_generate,
        )

        from orchestrator.nodes import (
            run_code_review,
            run_code_scan,
            run_interface_reconstruct,
            run_req_parse,
            run_test_report,
        )

        if node_name == "req_parse":
            await run_req_parse(
                self._engine, state, self._orch_store, self.llm, _PROMPTS, doc_paths
            )
        elif node_name == "code_scan":
            await run_code_scan(
                self._engine, state, self._orch_store, self._java_client, repositories
            )
        elif node_name == "interface_reconstruct":
            await run_interface_reconstruct(
                self._engine, state, self._orch_store, self._java_client,
                self.llm, _PROMPTS, repositories,
            )
        elif node_name == "code_review":
            await run_code_review(self._engine, state, self._orch_store, self.llm, _PROMPTS)
        elif node_name == "testcase_generate":
            await run_testcase_generate(
                self._engine, state, self._te_store, self.llm, _PROMPTS, auth=auth
            )
        elif node_name == "auto_execute":
            await run_auto_execute(
                self._engine, state, self._te_store, rest_base_url,
                self._test_rest_transport, auth=auth, e2e=e2e, insecure_tls=rest_insecure_tls,
                preauthenticated_headers=auth_headers,
            )
        elif node_name == "test_report":
            await run_test_report(self._engine, state, self._orch_store)
        elif node_name == "defect_list":
            await run_defect_list(self._engine, state, self._te_store)
        elif node_name == "retest_regression":
            await run_retest_regression(
                self._engine, state, self._te_store, rest_base_url,
                self._test_rest_transport, auth=auth, insecure_tls=rest_insecure_tls,
                preauthenticated_headers=auth_headers,
            )


def _settings(tmp: Path) -> Settings:
    return Settings(
        port=8000,
        java_parser_url="http://java-parser:8081",
        artifacts_root=str(tmp),
        llm_base_url="http://llm:8000/v1",
        llm_model_name="glm-5.3",
        llm_api_key="",
        llm_temperature=0.2,
        llm_top_p=0.8,
        llm_seed=None,
        llm_timeout_s=10.0,
        schemas_dir=str(_SCHEMAS),
    )


def _doc(tmp: Path) -> Path:
    doc = tmp / "req.md"
    doc.write_text("# 需求\\n\\n- REQ-001 用户查询：按 ID 查询用户\\n", encoding="utf-8")
    return doc


def _repos() -> list[RepositoryInput]:
    # C-10：仓库数下限 ≥1（schema minItems 已放宽；mock 侧仍双仓库覆盖多仓场景）
    return [
        RepositoryInput(name="user-service", path="/var/repos/user-service", branch="main"),
        RepositoryInput(name="order-center", path="/var/repos/order-center", branch="master"),
    ]


async def test_runner_full_pipeline_e2e(tmp_path: Path) -> None:
    """全链 e2e：9 节点按序完成，全部产物落盘，CLI 汇总含完成数。"""
    llm = StubLlm(_default_llm_scripts())
    runner = _ScriptedRunner(_settings(tmp_path), tmp_path, llm, rest_status=200)

    outcome = await runner.run(_JOB, [_doc(tmp_path)], _repos(), "http://rest:9000")

    assert outcome.ok
    assert outcome.completed == [
        "req_parse", "code_scan", "interface_reconstruct", "code_review",
        "testcase_generate", "auto_execute", "test_report", "defect_list",
        "retest_regression",
    ]
    assert outcome.degraded == []
    # 产物齐备（9 产物 + 留痕 + 脚本）
    job_dir = tmp_path / _JOB
    for filename in (
        "requirement_items.json",
        "requirement_review.json",
        "code_inventory.json",
        "interface_candidates.json",
        "interfaces.json",
        "review_mapping.json",
        "testcases.json",
        "execution.json",
        "test_report.md",
        "metrics.json",
        "defects.json",
        "regression_index.json",
        # 注：stub LLM 不经 LlmClient，故无 llm_calls.jsonl 留痕（生产路径有）
    ):
        assert (job_dir / filename).is_file(), filename
    runtime_events = [
        json.loads(line)
        for line in (job_dir / "runtime_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert runtime_events[0]["event"] == "run_started"
    assert any(
        event["event"] == "node_started" and event["node"] == "req_parse"
        for event in runtime_events
    )
    assert any(
        event["event"] == "node_finished" and event["node"] == "retest_regression"
        for event in runtime_events
    )
    manifest = json.loads((job_dir / "artifact_manifest.json").read_text(encoding="utf-8"))
    manifest_paths = {entry["path"] for entry in manifest["files"]}
    assert "requirement_items.json" in manifest_paths
    assert any(path.endswith("/test_TC_001.py") for path in manifest_paths)
    assert ".job-credentials.enc" not in manifest_paths
    # 需求节点包含评审与条目提取两个子阶段；用例生成前另有一次测试点拆分维度补全。
    # LLM 共调用 6 次。
    assert llm.calls == [
        "req_parse",
        "requirement_review",
        "interface_reconstruct",
        "code_review",
        "testpoint_derive",
        "testcase_generate",
    ]
    # 只断言前两个 Prompt 的节点口径，不锁版本号：Prompt 内容迭代不应误报此测试。
    assert [ref.split("@", 1)[0] for ref in llm.prompt_refs[:2]] == [
        "req_parse",
        "requirement_review",
    ]
    # 收尾刷新（docs/13 P-10）：metrics.json 为九节点终态版，自动化率 9/9
    metrics = json.loads((job_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["automation_rate"] == {
        "value": 1.0,
        "numerator": 9,
        "denominator": 9,
        "data_complete": True,
    }
    assert all(n["status"] == "completed" for n in metrics["nodes"])


async def test_runner_metrics_snapshot_then_final(tmp_path: Path) -> None:
    """P-10：报告时点快照后下游两节点完成，收尾刷新为终态版不再停留在 7/9。"""
    llm = StubLlm(_default_llm_scripts())
    runner = _ScriptedRunner(_settings(tmp_path), tmp_path, llm)
    # 中断在 test_report 之后：先手动跑前 7 节点，再验证快照与终态差异
    await runner.run(_JOB, [_doc(tmp_path)], _repos(), "http://rest:9000")
    # 跑完后重放「报告时点」验证：把 defect_list/retest_regression 回退为 pending 会触发重跑，
    # 此处直接验证终态产物口径——快照版（7/9）已被终态版覆盖，见 test_test_report 快照测试
    metrics = json.loads((tmp_path / _JOB / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["automation_rate"]["numerator"] == 9
    assert metrics["automation_rate"]["data_complete"] is True


async def test_runner_metrics_not_refreshed_when_interrupted(tmp_path: Path) -> None:
    """P-10 反向：早期中断（仍有 pending 节点）不刷新 metrics——避免产物判据误判。"""
    llm = StubLlm(_default_llm_scripts())
    runner = _ScriptedRunner(_settings(tmp_path), tmp_path, llm)
    state = runner._engine.init_job(_JOB)
    state.node("req_parse").status = NodeStatus.COMPLETED
    state.node("code_scan").status = NodeStatus.PENDING
    runner._refresh_final_metrics(state)
    assert not (tmp_path / _JOB / "metrics.json").exists()


async def test_runner_resume_skips_completed(tmp_path: Path) -> None:
    """断点续跑（F-ORCH-02）：产物在的节点跳过，产物缺的节点续跑。"""
    llm = StubLlm(_default_llm_scripts())
    runner = _ScriptedRunner(_settings(tmp_path), tmp_path, llm)
    # 第一次跑完
    await runner.run(_JOB, [_doc(tmp_path)], _repos(), "http://rest:9000")

    # 模拟中断时点：删除后半产物（产物为唯一事实，open_job 判据自动回退状态）
    job_dir = tmp_path / _JOB
    for filename in (
        "testcases.json",
        "execution.json",
        "test_report.md",
        "metrics.json",
        "defects.json",
        "regression_index.json",
    ):
        (job_dir / filename).unlink()
    # 脚本目录回退到未归档状态（首次 retest 已把脚本移入回归目录）
    for script in sorted((job_dir / "scripts").rglob("test_TC_*.py")):
        script.unlink()

    llm2 = StubLlm(_default_llm_scripts())
    runner.llm = llm2
    outcome = await runner.run(_JOB, [_doc(tmp_path)], _repos(), "http://rest:9000")

    # 产物在的前 4 节点跳过（LLM 只被用例生成及其拆分维度补全子阶段调用）
    assert llm2.calls == ["testpoint_derive", "testcase_generate"]
    assert outcome.ok
    assert outcome.completed[:4] == [
        "req_parse", "code_scan", "interface_reconstruct", "code_review"
    ]
    assert len(outcome.completed) == 9


async def test_runner_degrade_after_retry(tmp_path: Path) -> None:
    """重试耗尽 → 降级不中断（D1）：下游继续，outcome.degraded 记录原因。"""
    # testcase_generate 的 LLM 输出非法（X-LLM-02 两次）→ 降级
    scripts = _default_llm_scripts()
    scripts["testcase_generate"] = "not-json"
    llm = StubLlm(scripts)
    runner = _ScriptedRunner(_settings(tmp_path), tmp_path, llm)

    outcome = await runner.run(_JOB, [_doc(tmp_path)], _repos(), "http://rest:9000")

    assert not outcome.degraded == []
    degraded_names = [name for name, _reason in outcome.degraded]
    assert "testcase_generate" in degraded_names
    # D1：降级节点不阻塞下游
    assert "auto_execute" in outcome.completed or "auto_execute" in degraded_names
    # 降级原因含错误码
    reason = dict(outcome.degraded)["testcase_generate"]
    assert reason.startswith("X-LLM-02")


async def test_runner_rest_down_environment_failures_flow(
    tmp_path: Path,
) -> None:
    """被测服务 500：断言失败用例走完整闭环（缺陷清单 + 回归沉淀）。"""
    llm = StubLlm(_default_llm_scripts())
    runner = _ScriptedRunner(_settings(tmp_path), tmp_path, llm, rest_status=500)

    outcome = await runner.run(_JOB, [_doc(tmp_path)], _repos(), "http://rest:9000")

    assert outcome.ok
    defects = json.loads((tmp_path / _JOB / "defects.json").read_text(encoding="utf-8"))
    # 三类用例均断言失败 → 缺陷清单
    assert len(defects["defects"]) == 3
    # 复测仍 500 → retained
    assert all(d["status"] == "retained" for d in defects["defects"])
    # 回归索引空（无复测通过可沉淀）
    index = json.loads((tmp_path / _JOB / "regression_index.json").read_text(encoding="utf-8"))
    assert index["suites"] == []
    # 报告存在且含分列章节
    report = (tmp_path / _JOB / "test_report.md").read_text(encoding="utf-8")
    assert "## 2. 真实执行明细" in report


async def test_runner_supports_complete_protocol() -> None:
    """StubLlm 满足节点 SupportsComplete 协议（静态契约核对）。"""
    llm: SupportsComplete = StubLlm()
    assert llm is not None


async def test_runner_force_node_archives_old_artifacts(tmp_path: Path) -> None:
    """force-node（设计 §1.2）：旧产物移入 archive/，节点重跑产出新产物。"""
    llm = StubLlm(_default_llm_scripts())
    runner = _ScriptedRunner(_settings(tmp_path), tmp_path, llm)
    await runner.run(_JOB, [_doc(tmp_path)], _repos(), "http://rest:9000")
    job_dir = tmp_path / _JOB
    old_items = json.loads((job_dir / "requirement_items.json").read_text(encoding="utf-8"))

    # 重跑 req_parse（LLM 输出多一条需求项，验证产物确实重产出）
    scripts = _default_llm_scripts()
    scripts["req_parse"] = json.dumps(
        {"items": old_items["items"] + [
            {"id": "REQ-002", "title": "新增需求", "description": "force-node 重跑",
             "priority": "P2"}
        ]},
        ensure_ascii=False,
    )
    # 级联失效后 code_review 重跑：映射须覆盖新增 REQ-002（R-REVW-01 口径）
    scripts["code_review"] = json.dumps(
        {"mappings": _REVIEW["mappings"] + [
            {
                "requirement_id": "REQ-002",
                "interface_id": "IF-REST-001",
                "status": "missing",
                "evidence": {"note": "force-node 重跑新增需求"},
            }
        ]},
        ensure_ascii=False,
    )
    llm2 = StubLlm(scripts)
    runner.llm = llm2
    outcome = await runner.run(
        _JOB, [_doc(tmp_path)], _repos(), "http://rest:9000", force_node="req_parse"
    )

    assert outcome.ok
    # 旧产物已归档（C-14：时间戳前缀防多次重跑覆盖，禁止原地覆盖）
    archived = sorted((job_dir / "archive" / "req_parse").glob("*_requirement_items.json"))
    assert len(archived) == 1, f"应恰有一份带时间戳前缀的归档：{archived}"
    assert sorted((job_dir / "archive" / "req_parse").glob("*_requirement_review.json"))
    # 新产物是重跑结果（2 条需求项）
    new_items = json.loads((job_dir / "requirement_items.json").read_text(encoding="utf-8"))
    assert len(new_items["items"]) == 2
    # force 节点被真实重跑（req_parse 最先），下游 LLM 节点因级联失效全部重跑
    assert llm2.calls[0] == "req_parse"
    # C-14 级联失效：下游节点旧产物同时归档、重跑而非跳过
    assert (job_dir / "archive" / "test_report").is_dir()
    assert sorted((job_dir / "archive" / "test_report").glob("*_test_report.md"))
    assert sorted((job_dir / "archive" / "test_report").glob("*_metrics.json"))
    assert llm2.calls.count("interface_reconstruct") == 1
    assert llm2.calls.count("code_review") == 1
    assert llm2.calls.count("testcase_generate") == 1


async def test_runner_force_node_rejects_unknown_node(tmp_path: Path) -> None:
    """force-node 未知节点：X-ORCH-01 拒绝，不产生任何副作用。"""
    from common.errors import PipelineError

    llm = StubLlm(_default_llm_scripts())
    runner = _ScriptedRunner(_settings(tmp_path), tmp_path, llm)
    with pytest.raises(PipelineError) as exc_info:
        await runner.run(
            _JOB, [_doc(tmp_path)], _repos(), "http://rest:9000", force_node="not-a-node"
        )
    assert exc_info.value.code == "X-ORCH-01"


async def test_runner_req_parse_multi_docs(tmp_path: Path) -> None:
    """C-10：多文档提交——分节拼接单次送 LLM，产物 source_files 记全部。"""
    llm = StubLlm(_default_llm_scripts())
    runner = _ScriptedRunner(_settings(tmp_path), tmp_path, llm)
    doc2 = tmp_path / "req2.md"
    doc2.write_text("- REQ-002 订单查询：按单号查询订单\n", encoding="utf-8")

    await runner.run(_JOB, [_doc(tmp_path), doc2], _repos(), "http://rest:9000")

    items = json.loads((tmp_path / _JOB / "requirement_items.json").read_text(encoding="utf-8"))
    assert items["source_files"] == [str(_doc(tmp_path)), str(doc2)]
    assert items["source_file"] == str(_doc(tmp_path))


def test_repository_inputs_baseline_branch_passthrough() -> None:
    """C-09：baseline_branch 透传（空/缺省归 None 不比对）。"""
    inputs = repository_inputs_from_config(
        [
            {"name": "a", "path": "/abs/a", "branch": "feature", "baseline_branch": "main"},
            {"name": "b", "path": "/abs/b", "branch": "main", "baseline_branch": ""},
            {"name": "c", "path": "/abs/c", "branch": "main"},
        ]
    )
    assert inputs[0].baseline_branch == "main"
    assert inputs[0].to_dict()["baseline_branch"] == "main"
    assert inputs[1].baseline_branch is None
    assert "baseline_branch" not in inputs[1].to_dict()
    assert inputs[2].baseline_branch is None
    assert "baseline_branch" not in inputs[2].to_dict()
