"""defect_list 节点：断言失败用例 → 本地缺陷记录（F-DEF-01）。

归属（docs/12 产物布局）：DEF 节点属丙（test-executor），`defects.json` 丙写——
本函数的 `store` 参数须传入 **test-executor 身份** 的 ArtifactStore。

核心口径：
- R-DEF-01：仅「断言失败」（failure_kind=assertion）用例进缺陷清单；
  「环境失败」隔离于待环境确认（不入本清单，X-DEF-02 衔接 X-EXEC-01）；
- F-DEF-01：复现步骤 = 用例脚本引用（script_path + params 程序回填）；
- R-DEF-02：缺陷与用例经 case_id 双向关联（复测即重跑关联用例）；
- R-DEF-03：history 记时间戳流转，可审计生命周期（本节点记 discovered 起点）；
- X-DEF-03：is_fallback 从接口定义透传（演示埋入缺陷标注）；
- DEF-### 编号程序分配（不经 LLM）；
- 全程序化节点（无 Prompt，数值/溯源不经 LLM）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common.artifacts import ArtifactStore
from common.engine import Engine, JobState
from common.errors import X_ORCH_01, PipelineError

from orchestrator.nodes.auto_execute import _expected_statuses

NODE_NAME = "defect_list"


async def run_defect_list(
    engine: Engine, state: JobState, store: ArtifactStore
) -> Path:
    """生成缺陷清单：execution.json 断言失败用例 → defects.json。

    Args:
        store: test-executor 身份的产物卷（defects.json 丙写）。

    Returns:
        defects.json 产物路径。

    Raises:
        PipelineError: 前置产物缺失（X-ORCH-01）；跳序（X-ORCH-02）。
    """
    engine.check_runnable(state, NODE_NAME)
    engine.mark_running(state, NODE_NAME)
    try:
        execution = _load_json(store, state.job_id, "execution.json")
        testcases = _load_json(store, state.job_id, "testcases.json")
        interfaces = _load_json(store, state.job_id, "interfaces.json")

        results = execution.get("results", [])
        case_by_id = {
            str(c.get("id", "")): c for c in testcases.get("cases", []) if c.get("id")
        }
        # E2E 用例与接口用例同源于 testcases.json，但不具备 REST interface_id；
        # 必须独立回填，否则 E2E 缺陷页会错误展示接口缺陷上下文。
        e2e_case_by_id = {
            str(c.get("id", "")): c for c in testcases.get("e2e_cases", []) if c.get("id")
        }
        interface_by_id = {
            str(i.get("id", "")): i for i in interfaces.get("interfaces", []) if i.get("id")
        }

        defects: list[dict[str, Any]] = []
        # R-DEF-01：仅断言失败进清单（环境失败隔离，不入）
        for index, result in enumerate(r for r in results if r.get("failure_kind") == "assertion"):
            case_id = str(result.get("case_id", ""))
            is_e2e = result.get("case_type") == "e2e" or case_id.startswith("E2E-")
            case = (e2e_case_by_id if is_e2e else case_by_id).get(case_id, {})
            if_id = str(result.get("interface_id", ""))
            interface = interface_by_id.get(if_id, {})
            params = case.get("params") if isinstance(case.get("params"), dict) else {}
            reproduce: dict[str, Any] = {
                "script_path": str(case.get("script_path", ""))  # F-DEF-01 用例脚本引用
            }
            if params:
                reproduce["params"] = params
            raw_signature = interface.get("signature")
            signature = raw_signature if isinstance(raw_signature, dict) else {}
            actual: dict[str, Any] = {}
            if result.get("actual_status") is not None:
                actual["status"] = result["actual_status"]
            if result.get("actual_body") is not None:
                actual["body"] = _safe_actual_body(result["actual_body"])
            defects.append(
                {
                    "id": f"DEF-{index + 1:03d}",
                    "case_id": case_id,
                    "interface_id": if_id,
                    "failure_reason": str(result.get("failure_reason", "")),
                    "triage": _triage_assertion(result, case, interface),
                    # 缺陷页的定位事实全部从前置产物回填，非 LLM 摘要。
                    "interface": {
                        "protocol": "e2e" if is_e2e else str(interface.get("protocol", "")),
                        "method": "浏览器" if is_e2e else str(signature.get("method", "")),
                        "path": "前端页面" if is_e2e else str(signature.get("path", "")),
                        "summary": (
                            str(case.get("title", ""))
                            if is_e2e
                            else str(signature.get("summary", ""))
                        ),
                    },
                    "testcase": {
                        "title": str(case.get("title", "")),
                        "category": "e2e" if is_e2e else str(case.get("category", "")),
                        "expected": str(case.get("expected", result.get("expected", ""))),
                    },
                    "actual": actual,
                    "reproduce": reproduce,
                    "status": "discovered",
                    # X-DEF-03：演示埋入缺陷标注随接口兜底标记透传
                    "is_fallback": bool(interface.get("is_fallback", False)),
                    "history": [
                        {
                            "event": "discovered",
                            "timestamp": str(result.get("timestamp", _now_iso())),
                        }
                    ],
                }
            )

        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "job_id": state.job_id,
            "defects": defects,
        }
        path = store.write(state.job_id, "defects.json", payload)
        # 零缺陷也是合法产出（全通过/全环境失败）——判据是产物过校验
        engine.mark_completed(state, NODE_NAME)
        return path
    except PipelineError as exc:
        engine.mark_failed(state, NODE_NAME, exc)
        raise


def _load_json(store: ArtifactStore, job_id: str, filename: str) -> dict[str, Any]:
    """读前置产物（缺失 → X-ORCH-01）。"""
    try:
        return store.read_json(job_id, filename)
    except FileNotFoundError as exc:
        raise PipelineError(
            code=X_ORCH_01,
            message=f"前置产物 {filename} 缺失",
            node_id=NODE_NAME,
        ) from exc


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（横切约定）。"""
    return datetime.now(UTC).isoformat()


def _safe_actual_body(value: Any) -> Any:
    """缺陷展示保留实际响应，同时限制体积并掩盖常见凭据字段。"""
    sensitive = {"password", "token", "access_token", "refresh_token", "authorization", "cookie"}
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if str(key).lower() in sensitive else _safe_actual_body(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_actual_body(item) for item in value[:50]]
    text = str(value)
    return text if len(text) <= 4_000 else f"{text[:4_000]}…（响应已截断）"


def _triage_assertion(
    result: dict[str, Any], case: dict[str, Any], interface: dict[str, Any]
) -> dict[str, str]:
    """R-DEF-04：用状态码、预期和已知参数形态进行保守分流。

    该规则不判断业务实现正确性：400/422 的“正常成功”更可能是造数不全；500 是
    高优先级服务端候选；2xx 未满足 4xx 预期常见于前置状态或断言假设，必须核证。
    """
    status = result.get("actual_status")
    expected = str(case.get("expected", ""))
    params = case.get("params") if isinstance(case.get("params"), dict) else {}
    definitions = interface.get("params", [])
    if not isinstance(definitions, list):
        definitions = []
    has_unknown_container = any(
        isinstance(item, dict)
        and item.get("type") == "unknown"
        and item.get("in") in {"body", "query"}
        for item in definitions
    )
    expected_statuses, expected_classes = _expected_statuses(expected)
    expects_success = (
        any(200 <= value < 300 for value in expected_statuses) or 2 in expected_classes
    )
    expects_client_error = (
        any(400 <= value < 500 for value in expected_statuses) or 4 in expected_classes
    )

    if isinstance(status, int) and status >= 500:
        return {
            "kind": "product_suspect",
            "confidence": "high",
            "reason": "服务端返回 5xx；在非环境失败前提下优先按真实服务端缺陷候选排查。",
        }
    if (
        isinstance(status, int)
        and 400 <= status < 500
        and expects_success
        and has_unknown_container
    ):
        return {
            "kind": "input_construction",
            "confidence": "high",
            "reason": (
                "成功场景收到 4xx，且接口参数仍为 unknown 容器；优先补齐真实必填字段、"
                "关联数据和业务前置。"
            ),
        }
    if isinstance(status, int) and 200 <= status < 300 and expects_client_error:
        return {
            "kind": "assertion_or_setup",
            "confidence": "medium",
            "reason": (
                "接口接受了本次异常输入；需核对用例前置、参数是否真正触发异常，"
                "以及需求是否要求后端拒绝。"
            ),
        }
    if params:
        return {
            "kind": "needs_evidence",
            "confidence": "low",
            "reason": "状态码与断言不一致，但现有接口契约不足以区分实现问题与业务数据/预期问题。",
        }
    return {
        "kind": "needs_evidence",
        "confidence": "low",
        "reason": "缺少可判定的请求参数或服务端契约证据，需要人工核对。",
    }
