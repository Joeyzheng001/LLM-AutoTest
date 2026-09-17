"""retest_regression 节点：缺陷复测 + 回归沉淀（F-DEF-02/03、F-REG-01~04）。

归属（docs/12 产物布局）：REG 节点属丙（test-executor），`regression_index.json`
与 `defects.json`（复测状态更新）均丙写——本函数的 `store` 参数须传入
**test-executor 身份** 的 ArtifactStore（执行 Runner 复用 auto_execute 模块）。

核心口径：
- F-DEF-02：复测 = 对全量缺陷重跑关联用例（R-DEF-02 双向关联）；
- F-DEF-03 状态流转：复测通过 → closed_suggested（关闭建议）；
  仍失败 → retained（保留）——history 记时间戳，可审计（R-DEF-03）；
- F-REG-01：归档 = **移动**（非复制）复测通过脚本至回归目录，
  原执行目录只含未沉淀用例；
- F-REG-02：溯源随档（requirement_id + interface_id）；
- R-REG-02：回归目录按协议维度组织（scripts/regression/<protocol>/）；
- R-REG-03 幂等：同用例重档更新 archived_at、version 递增（X-REG-01）；
- X-DEF-02：复测用例脚本已被删除 → 报错指明缺失用例，该项跳过（警告记录）；
- X-DEF-01：复测时被测代码已变更 → based_on_new_version 标注——单体演示态
  无版本比对机制，**不加此标注**（不谎报「基于新版本」也不谎报「同版本」）；
- X-REG-02：回归漂移交人工判断——首次复测不涉及回归套件重跑，不产漂移项；
- 全程序化节点（无 Prompt，数值/溯源不经 LLM）。
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from orchestrator.artifacts import ArtifactStore
from orchestrator.engine import Engine, JobState
from orchestrator.errors import X_ORCH_01, PipelineError
from orchestrator.nodes.auto_execute import (
    InvokeRequest,
    MockDemoRunner,
    RestRunner,
    _judge_assertion,
    auth_exempt_paths,
    login_for_headers,
    prefers_django_trailing_slash,
    probe_rest,
)

NODE_NAME = "retest_regression"

_REGRESSION_ROOT = "scripts/regression"


async def run_retest_regression(
    engine: Engine,
    state: JobState,
    store: ArtifactStore,
    rest_base_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
    auth: dict[str, Any] | None = None,
    insecure_tls: bool = True,
) -> Path:
    """复测全量缺陷并沉淀回归资产：defects.json（状态流转）+ regression_index.json。

    Args:
        store: test-executor 身份的产物卷（两产物均丙写）。
        rest_base_url: REST 被测/Mock 服务地址（复测重跑共用）。
        transport: 测试注入（MockTransport），生产留 None。
        auth: C-15 鉴权配置（与 auto_execute 同口径，凭据头注入复测请求）。

    Returns:
        regression_index.json 产物路径。

    Raises:
        PipelineError: 前置产物缺失（X-ORCH-01）；跳序（X-ORCH-02）；登录失败（X-EXEC-01）。
    """
    engine.check_runnable(state, NODE_NAME)
    engine.mark_running(state, NODE_NAME)
    try:
        result = await retest_defects(
            store,
            state.job_id,
            rest_base_url,
            transport,
            auth=auth,
            insecure_tls=insecure_tls,
        )
        engine.mark_completed(state, NODE_NAME)
        return Path(str(result["regression_index_path"]))
    except PipelineError as exc:
        engine.mark_failed(state, NODE_NAME, exc)
        raise


async def retest_defects(
    te_store: ArtifactStore,
    job_id: str,
    rest_base_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
    only_defect_ids: set[str] | None = None,
    auth: dict[str, Any] | None = None,
    insecure_tls: bool = True,
) -> dict[str, Any]:
    """复测核心（供 9 节点流水线与 REST API 单条复测共用）。

    Args:
        te_store: test-executor 身份产物卷（defects.json / regression_index.json 丙写）。
        only_defect_ids: None → 全量复测（F-DEF-02）；指定集合 → 仅复测该批缺陷
            （Web 缺陷闭环页「单条复测」按钮，07 §3.5）。
        auth: C-15 鉴权配置（登录失败 → X-EXEC-01，凭据头注入复测请求）。

    Returns:
        {"retested": [缺陷 ID 清单], "archived": [归档条目], "warnings": [告警],
        "regression_index_path": 产物路径}。

    Raises:
        PipelineError: 前置产物缺失（X-ORCH-01）。
    """
    defects_payload = _load_json(te_store, job_id, "defects.json")
    testcases = _load_json(te_store, job_id, "testcases.json")
    interfaces = _load_json(te_store, job_id, "interfaces.json")

    defects: list[dict[str, Any]] = list(defects_payload.get("defects", []))
    case_by_id = {
        str(c.get("id", "")): c for c in testcases.get("cases", []) if c.get("id")
    }
    interface_by_id = {
        str(i.get("id", "")): i for i in interfaces.get("interfaces", []) if i.get("id")
    }

    # REST 证书策略与登录独立，复测与首次执行同一口径。
    auth_headers: dict[str, str] = {}
    verify = not insecure_tls
    if auth:
        auth_headers = await login_for_headers(auth, transport)

    rest_ready = await probe_rest(rest_base_url, transport, verify)
    rest_runner = RestRunner(
        rest_base_url,
        transport,
        auth_headers,
        verify,
        auth_exempt_paths(auth),
        prefers_django_trailing_slash(auth),
    )
    demo_runner = MockDemoRunner()
    warnings: list[str] = []
    archived: list[dict[str, Any]] = []
    retested: list[str] = []
    job_dir = te_store.job_dir(job_id)

    for defect in defects:
        if only_defect_ids is not None and str(defect.get("id", "")) not in only_defect_ids:
            continue
        case_id = str(defect.get("case_id", ""))
        case = case_by_id.get(case_id)
        script_rel = str((case or {}).get("script_path", ""))
        script_path = job_dir / script_rel if script_rel else None
        if_id = str(defect.get("interface_id", ""))
        interface = interface_by_id.get(if_id, {})
        protocol = str(interface.get("protocol", "rest"))

        # 复测环境探测（R-EXEC-02 同源口径）：REST 不可达 → 跳过该项，
        # 不判缺陷状态流转（环境失败不判为缺陷，X-EXEC-01 同源精神）
        if protocol == "rest" and not rest_ready:
            warnings.append(
                f"复测环境不可达（X-EXEC-01 同源）：缺陷 {defect.get('id')}"
                f"（用例 {case_id}）跳过复测，状态保持不变"
            )
            continue

        # X-DEF-02：脚本已被人工删除/修改 → 指明缺失用例，该项跳过
        if case is None or script_path is None or not script_path.is_file():
            warnings.append(
                f"X-DEF-02：用例 {case_id}（缺陷 {defect.get('id')}）脚本缺失"
                f"（{script_rel or '<无 script_path>'}），复测跳过该项"
            )
            continue

        # 复测重跑（R-DEF-02：重跑关联用例；判定与 auto_execute 同源：
        # status token > 2xx + 语义等值对；404 归环境失败不判缺陷修复）
        runner = rest_runner if protocol == "rest" else demo_runner
        params = case.get("params") if isinstance(case.get("params"), dict) else {}
        response = await runner.invoke(
            InvokeRequest(interface=interface, params=params)
        )
        retest_passed = False
        if response.ok and response.status is not None:
            if response.status == 404:
                # 环境类（接口未部署）——复测不计通过也不计修复失败，环境待确认
                retest_passed = False
            else:
                retest_passed, _failures = _judge_assertion(
                    str(case.get("expected", "")), response.status, response.body
                )

        now = _now_iso()
        history: list[dict[str, Any]] = list(defect.get("history", []))
        history.append({"event": "retest_started", "timestamp": now})
        if retest_passed:
            # F-DEF-03：复测通过 → 关闭建议
            history.append({"event": "retest_passed", "timestamp": now})
            history.append({"event": "close_suggested", "timestamp": now})
            defect["status"] = "closed_suggested"
            # F-REG-01/02：归档 = 移动至回归目录 + 溯源随档；
            # 溯源缺失（requirement_id 为空）则跳过归档并告警——不造数据
            requirement_id = str(case.get("requirement_id", ""))
            if requirement_id:
                target_rel = _archive_script(job_dir, script_path, protocol)
                archived.append(
                    {
                        "case_id": case_id,
                        "script_path": target_rel,
                        "requirement_id": requirement_id,
                        "interface_id": if_id,
                        "protocol": protocol,
                        "archived_at": now,
                    }
                )
            else:
                warnings.append(
                    f"F-REG-02 溯源缺失：用例 {case_id}（缺陷 {defect.get('id')}）"
                    "无 requirement_id，跳过归档（脚本留原执行目录）"
                )
        else:
            # F-DEF-03：仍失败 → 保留（脚本留原目录，未沉淀）
            history.append({"event": "retest_failed", "timestamp": now})
            history.append({"event": "retained", "timestamp": now})
            defect["status"] = "retained"
        defect["history"] = history
        retested.append(str(defect.get("id", "")))

    # 基线沉淀：本轮已经真实执行通过的接口用例同样可成为回归候选。
    # 不收 E2E（其页面状态/环境依赖更强）、环境降级、不可加载或无溯源的用例。
    archived_case_ids = {str(item["case_id"]) for item in archived}
    try:
        execution = _load_json(te_store, job_id, "execution.json")
    except PipelineError:
        execution = {"results": []}
    for result in execution.get("results", []):
        case_id = str(result.get("case_id", ""))
        case = case_by_id.get(case_id)
        if (
            result.get("passed") is not True
            or result.get("case_type") == "e2e"
            or case is None
            or case_id in archived_case_ids
            or case.get("loadable") is not True
        ):
            continue
        requirement_id = str(case.get("requirement_id", ""))
        interface_id = str(case.get("interface_id", ""))
        interface = interface_by_id.get(interface_id, {})
        script_rel = str(case.get("script_path", ""))
        script_path = job_dir / script_rel if script_rel else None
        if not requirement_id or script_path is None:
            continue
        protocol = str(interface.get("protocol", "rest"))
        # 已归档脚本在重跑时不应因原路径不存在而从索引消失。
        existing_rel = f"{_REGRESSION_ROOT}/{protocol}/{script_path.name}"
        existing_path = job_dir / existing_rel
        if script_path.is_file():
            archived_path = _archive_script(job_dir, script_path, protocol)
        elif existing_path.is_file():
            archived_path = existing_rel
        else:
            continue
        archived.append(
            {
                "case_id": case_id,
                "script_path": archived_path,
                "requirement_id": requirement_id,
                "interface_id": interface_id,
                "protocol": protocol,
                "archived_at": _now_iso(),
                "source": "execution_passed",
            }
        )
        archived_case_ids.add(case_id)

    defects_payload["defects"] = defects
    if warnings:
        defects_payload["warnings"] = warnings
    te_store.write(job_id, "defects.json", defects_payload)
    path = te_store.write(
        job_id, "regression_index.json", _build_index(job_id, archived, te_store)
    )
    return {
        "retested": retested,
        "archived": archived,
        "warnings": warnings,
        "regression_index_path": str(path),
    }


def _archive_script(job_dir: Path, script_path: Path, protocol: str) -> str:
    """移动脚本至回归目录（R-REG-01 移动非复制；R-REG-02 按协议组织）。

    幂等（R-REG-03）：目标已存在（重档）时覆盖移动，版本由索引递增（X-REG-01）。
    """
    target = job_dir / _REGRESSION_ROOT / protocol / script_path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(script_path), str(target))
    return f"{_REGRESSION_ROOT}/{protocol}/{script_path.name}"


def _build_index(
    job_id: str, archived: list[dict[str, Any]], store: ArtifactStore
) -> dict[str, Any]:
    """构造 regression_index.json（含 R-REG-03/X-REG-01 幂等版本合并）。"""
    # 旧索引存在 → 同用例重档：更新 archived_at、version+1（X-REG-01）
    existing: dict[str, dict[str, Any]] = {}
    try:
        old = store.read_json(job_id, "regression_index.json")
        for suite in old.get("suites", []):
            for old_script in suite.get("scripts", []):
                existing[str(old_script.get("case_id"))] = dict(old_script)
    except FileNotFoundError:
        pass

    suites: dict[str, list[dict[str, Any]]] = {}
    for entry in archived:
        case_id = str(entry["case_id"])
        old_entry = existing.get(case_id)
        script: dict[str, Any] = {
            "case_id": case_id,
            "script_path": str(entry["script_path"]),
            "requirement_id": str(entry["requirement_id"]),
            "interface_id": str(entry["interface_id"]),
            "archived_at": str(entry["archived_at"]),
        }
        if old_entry is not None:
            script["version"] = int(old_entry.get("version", 1)) + 1
        suites.setdefault(str(entry["protocol"]), []).append(script)

    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "job_id": job_id,
        "suites": [
            {"protocol": protocol, "scripts": scripts}
            for protocol, scripts in sorted(suites.items())
        ],
    }
    if not suites:
        payload["empty_reason"] = (
            "本轮没有同时满足“真实执行通过、可加载脚本、需求与接口溯源完整”的接口用例；"
            "E2E 需连续两次通过后才会沉淀。"
        )
    return payload


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
