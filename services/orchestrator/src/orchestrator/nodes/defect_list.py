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

from orchestrator.artifacts import ArtifactStore
from orchestrator.engine import Engine, JobState
from orchestrator.errors import X_ORCH_01, PipelineError

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
        interface_by_id = {
            str(i.get("id", "")): i for i in interfaces.get("interfaces", []) if i.get("id")
        }

        defects: list[dict[str, Any]] = []
        # R-DEF-01：仅断言失败进清单（环境失败隔离，不入）
        for index, result in enumerate(r for r in results if r.get("failure_kind") == "assertion"):
            case_id = str(result.get("case_id", ""))
            case = case_by_id.get(case_id, {})
            if_id = str(result.get("interface_id", ""))
            interface = interface_by_id.get(if_id, {})
            params = case.get("params") if isinstance(case.get("params"), dict) else {}
            reproduce: dict[str, Any] = {
                "script_path": str(case.get("script_path", ""))  # F-DEF-01 用例脚本引用
            }
            if params:
                reproduce["params"] = params
            defects.append(
                {
                    "id": f"DEF-{index + 1:03d}",
                    "case_id": case_id,
                    "interface_id": if_id,
                    "failure_reason": str(result.get("failure_reason", "")),
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
