"""testcase_generate 节点：接口 + 需求 → 测试用例与脚本（F-GEN-01~04 / R-GEN-01~04）。

归属（docs/11 泳道 / docs/12 产物布局）：GEN 节点属丙（test-executor），
`testcases.json` 与 `scripts/` 均为丙写——本函数的 `store` 参数须传入
**test-executor 身份**的 ArtifactStore（Prompt 加载器复用甲的 prompts_loader）。
单体过渡期由编排进程驱动；服务拆分后本模块随迁 test-executor。

分工红线（脚本不经 LLM）：
- LLM 只产用例元数据（category/title/params/expected/溯源）；
- pytest 脚本由程序模板渲染（F-GEN-03：可加载性由模板保证 + py_compile 校验）；
- `id`（TC-###）/ `script_path` / `loadable` / `low_confidence` 全部由代码生成或回填。

程序化校验：
- R-GEN-02：params 参数名 ⊆ 接口定义参数集合——违规用例丢弃，带反馈重生成一次；
  二次仍违规 → 该接口用例放弃，记 X-GEN-02 告警；
- R-GEN-03：脚本 py_compile，失败记 loadable=false；
- X-GEN-01：接口零用例（LLM 漏产）→ 告警跳过，其他接口不受影响；
- X-GEN-03：接口含 unknown 参数 → 用例标 low_confidence。

完成判据（docs/12 §1.3）：loadable 全真 → completed；存在不可加载脚本 →
mark_degraded（manual_intervention，D1：不阻塞下游）。零用例 → X-GEN-02 节点失败。
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from orchestrator.artifacts import ArtifactStore
from orchestrator.engine import Engine, JobState
from orchestrator.errors import X_LLM_02, X_ORCH_01, PipelineError
from orchestrator.llm import LlmResult
from orchestrator.llm_batch import chunk_json_items
from orchestrator.llm_repair import complete_with_json_repair
from orchestrator.nodes.req_parse import _strip_code_fence
from orchestrator.prompts_loader import PromptSpec, load_prompt

NODE_NAME = "testcase_generate"

_VALID_CATEGORIES = ("normal", "boundary", "exception")

_SCRIPT_TEMPLATE = '''"""{case_id}: {title!r}

interface: {interface_id}
requirement: {requirement_id}
generated-by: LLM-AutoTest job {job_id}（脚本由程序模板渲染，元数据来自 LLM 用例设计）
"""

PARAMS = {params!r}
EXPECTED = {expected!r}


def {func_name}(run_context):
    """{case_id}：由 test-executor 提供运行上下文并按 EXPECTED 断言。"""
    response = run_context.invoke(PARAMS)
    run_context.assert_result(response, EXPECTED)
'''


class SupportsComplete(Protocol):
    """LLM 客户端协议（生产为 LlmClient，测试可注入桩）。"""

    async def complete(self, node: str, prompt: PromptSpec, user_content: str) -> LlmResult:
        """按协议执行一次补全。"""
        ...


async def run_testcase_generate(
    engine: Engine,
    state: JobState,
    store: ArtifactStore,
    llm: SupportsComplete,
    prompts_dir: Path,
    auth: dict[str, Any] | None = None,
    e2e: dict[str, Any] | None = None,
) -> Path:
    """执行 testcase_generate 全流程：读前置 → LLM 两轮 → 校验渲染 → 落盘。

    auth（C-17）：鉴权凭据随上下文发给 LLM——登录类接口 normal 用例用真实
    凭据、exception 用错误凭据（用户裁决；凭据暴露面扩大已记录 docs/13）。

    Returns:
        testcases.json 产物路径。

    Raises:
        PipelineError: 零用例（X-GEN-02）/ LLM 输出不可解析（X-LLM-02）/ 前置缺失（X-ORCH-01）。
    """
    engine.check_runnable(state, NODE_NAME)
    engine.mark_running(state, NODE_NAME)
    try:
        requirements = _load_json_array(store, state.job_id, "requirement_items.json", "items")
        interfaces = _load_json_array(store, state.job_id, "interfaces.json", "interfaces")
        mappings = _load_json_array(store, state.job_id, "review_mapping.json", "mappings")
        # 测试点是用例设计的中间证据，而不是另一次不稳定的长 JSON 模型输出。
        # 它按需求、接口映射及评审风险程序化派生，随后随同批上下文交给模型扩展成可执行案例。
        testpoints = _derive_testpoints(requirements, interfaces, mappings)

        prompt = load_prompt(prompts_dir, NODE_NAME)
        # 以接口为分批边界：每批只携带相关需求与映射，避免用例 JSON 随接口数线性膨胀。
        valid: list[dict[str, Any]] = []
        retry_feedback: list[dict[str, Any]] = []
        interface_batches = chunk_json_items(interfaces)
        for index, batch in enumerate(interface_batches, start=1):
            user_content = _build_batch_context(
                batch, requirements, mappings, testpoints, auth, index, len(interface_batches)
            )
            raw_cases = await complete_with_json_repair(
                llm,
                NODE_NAME,
                prompt,
                user_content,
                partial(_parse_llm_cases, interfaces=batch),
            )
            batch_valid, violations = _filter_param_violations(raw_cases, batch)
            if violations:
                feedback = json.dumps(
                    {
                        "violations": violations,
                        "note": (
                            "以下用例参数名不在接口定义内，请仅用接口定义的参数名重新生成这些用例"
                        ),
                    },
                    ensure_ascii=False,
                )
                raw_cases2 = await complete_with_json_repair(
                    llm,
                    NODE_NAME,
                    prompt,
                    f"{user_content}\n\n{feedback}",
                    partial(_parse_llm_cases, interfaces=batch),
                )
                valid2, violations2 = _filter_param_violations(raw_cases2, batch)
                batch_valid = _merge_rounds(batch_valid, valid2)
                retry_feedback.extend(violations2)
            valid.extend(batch_valid)

        warnings: list[str] = []
        # X-GEN-02：二次仍违规的接口——整接口用例放弃（非部分保留）
        abandoned = sorted({v["interface_id"] for v in retry_feedback})
        for interface_id in abandoned:
            warnings.append(
                f"X-GEN-02: 接口 {interface_id} 参数一致性连续 2 次不通过，"
                "该接口用例放弃（生成质量告警）"
            )
        if abandoned:
            valid = [c for c in valid if c["interface_id"] not in set(abandoned)]
        # X-GEN-01：零用例接口跳过并记录
        covered_interfaces = {case["interface_id"] for case in valid}
        for interface in interfaces:
            if_id = str(interface.get("id", ""))
            if if_id and if_id not in covered_interfaces:
                warnings.append(f"X-GEN-01: 接口 {if_id} 未产出用例，跳过并记录")

        if not valid:
            raise PipelineError(
                code="X-GEN-02",
                message="全部接口用例生成失败（参数一致性二次不过或漏产）",
                node_id=NODE_NAME,
                details={"warnings": warnings},
            )

        # 元数据补全 + 脚本渲染（TC-### 编号、模板脚本、loadable、low_confidence）
        cases, any_unloadable = _finalize_cases(valid, state.job_id, store, interfaces)
        e2e_cases: list[dict[str, Any]] = []
        if e2e and bool(e2e.get("generate_cases")):
            surface = await _inspect_e2e_surface(
                str(e2e.get("base_url", "")), bool(e2e.get("insecure_tls", True)), auth
            )
            if surface["routes"]:
                e2e_cases = await _generate_e2e_cases(
                    llm, prompts_dir, requirements, testpoints, surface
                )
            else:
                warnings.append("X-E2E-01: 未探测到可操作的前端页面，跳过 E2E 用例生成")
        if any_unloadable:
            warnings.append("R-GEN-03: 存在不可加载脚本（loadable=false），详见用例清单")

        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "job_id": state.job_id,
            "cases": cases,
            "testpoints": testpoints,
        }
        if e2e_cases:
            payload["e2e_cases"] = e2e_cases
        if warnings:
            payload["warnings"] = warnings
        try:
            path = store.write(state.job_id, "testcases.json", payload)
        except PipelineError as exc:
            if exc.code == X_ORCH_01:
                raise PipelineError(
                    code=X_LLM_02,
                    message=f"LLM 输出未过 Schema 校验：{exc.message}",
                    node_id=NODE_NAME,
                    details=exc.details,
                ) from exc
            raise
        if any_unloadable:
            # docs/12 §1.3：loadable 非全真 → 降级（D1：不阻塞下游）
            engine.mark_degraded(
                state, NODE_NAME, "存在不可加载脚本（R-GEN-03），相关用例需人工修复后复跑"
            )
        else:
            engine.mark_completed(state, NODE_NAME)
        return path
    except PipelineError as exc:
        engine.mark_failed(state, NODE_NAME, exc)
        raise


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


def _build_batch_context(
    interfaces: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    testpoints: list[dict[str, Any]],
    auth: dict[str, Any] | None,
    index: int,
    total: int,
) -> str:
    """构造一批用例生成上下文，只携带本批接口相关的需求。"""
    interface_ids = {str(item.get("id", "")) for item in interfaces}
    batch_mappings = [
        {
            "requirement_id": mapping.get("requirement_id"),
            "interface_id": mapping.get("interface_id"),
            "status": mapping.get("status"),
        }
        for mapping in mappings
        if str(mapping.get("interface_id", "")) in interface_ids
    ]
    requirement_ids = {str(mapping.get("requirement_id", "")) for mapping in batch_mappings}
    context: dict[str, Any] = {
        "batch": {"index": index, "total": total},
        "interfaces": interfaces,
        "requirements": [
            item for item in requirements if str(item.get("id", "")) in requirement_ids
        ],
        "mappings": batch_mappings,
        "testpoints": [
            point for point in testpoints if str(point.get("interface_id", "")) in interface_ids
        ],
    }
    if auth:
        # 凭据只出现在本次模型请求内，调用留痕仅记录摘要；不写入用例产物。
        context["auth"] = {
            "login_url": auth.get("login_url"),
            "username": auth.get("username"),
            "password": auth.get("password"),
        }
    return json.dumps(context, ensure_ascii=False)


def _derive_testpoints(
    requirements: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从需求映射派生可追溯测试点，避免模型只围绕 HTTP 参数拼接口用例。

    每个已映射的需求-接口关系至少有一个主业务测试点；评审为 partial、missing
    或 undetermined 时额外生成风险测试点。没有映射的需求也保留为需求级风险点，
    供后续 E2E/人工测试设计使用，绝不伪造接口归属。
    """
    interface_ids = {str(item.get("id", "")) for item in interfaces}
    requirement_by_id = {str(item.get("id", "")): item for item in requirements}
    points: list[dict[str, Any]] = []

    def append_point(
        requirement_id: str,
        interface_id: str | None,
        source: str,
        scenario: str,
        hints: list[str],
        source_ref: str,
    ) -> None:
        points.append(
            {
                "id": f"TP-{len(points) + 1:03d}",
                "requirement_id": requirement_id,
                **({"interface_id": interface_id} if interface_id else {}),
                "source": source,
                "source_ref": source_ref,
                "scenario": scenario,
                "case_split_hints": hints,
            }
        )

    mapped_requirements: set[str] = set()
    for mapping in mappings:
        requirement_id = str(mapping.get("requirement_id", ""))
        interface_id = str(mapping.get("interface_id", ""))
        if requirement_id not in requirement_by_id or interface_id not in interface_ids:
            continue
        mapped_requirements.add(requirement_id)
        requirement = requirement_by_id[requirement_id]
        title = str(requirement.get("title", requirement_id))
        description = str(requirement.get("description", "")).strip()
        append_point(
            requirement_id,
            interface_id,
            "REQ",
            f"验证“{title}”主业务流程" + (f"：{description}" if description else ""),
            ["正常主流程", "输入边界与空值", "异常/权限/业务拒绝"],
            f"需求 {requirement_id} ↔ 接口 {interface_id}",
        )
        status = str(mapping.get("status", ""))
        if status in {"partial", "missing", "undetermined"}:
            append_point(
                requirement_id,
                interface_id,
                "RISK",
                f"确认“{title}”在实现状态为 {status} 时的遗漏、降级或不一致行为",
                ["关键业务路径", "异常提示与数据一致性", "配置/会话/权限等服务端行为"],
                f"代码评审映射状态：{status}",
            )
    for requirement_id, requirement in requirement_by_id.items():
        if requirement_id in mapped_requirements:
            continue
        append_point(
            requirement_id,
            None,
            "RISK",
            (
                f"需求“{requirement.get('title', requirement_id)}”尚未映射到接口，"
                "需通过页面流程或人工确认实现证据"
            ),
            ["端到端主路径", "用户可见反馈", "实现与需求的可追溯性"],
            f"需求 {requirement_id} 无接口映射",
        )
    return points


async def _generate_e2e_cases(
    llm: SupportsComplete,
    prompts_dir: Path,
    requirements: list[dict[str, Any]],
    testpoints: list[dict[str, Any]],
    surface: dict[str, Any],
) -> list[dict[str, Any]]:
    """基于真实页面探测结果生成，禁止模型杜撰路由或 selector。"""
    prompt = load_prompt(prompts_dir, "e2e_testcase_generate")
    allowed_selectors = {selector for route in surface["routes"] for selector in route["selectors"]}
    allowed_paths = {route["path"] for route in surface["routes"]}
    content = json.dumps(
        {
            "surface": surface,
            "requirements": requirements,
            "testpoints": testpoints,
            "max_cases": 12,
        },
        ensure_ascii=False,
    )
    raw_cases = await complete_with_json_repair(
        llm,
        "testcase_generate",
        prompt,
        content,
        partial(_parse_e2e_cases, allowed_selectors=allowed_selectors, allowed_paths=allowed_paths),
    )
    result: list[dict[str, Any]] = []
    for sequence, case in enumerate(raw_cases[:12], start=1):
        entry = {
            "id": f"E2E-{sequence:03d}",
            "case_type": "e2e",
            "requirement_id": str(case["requirement_id"]),
            "title": str(case["title"]),
            "preconditions": [str(x) for x in case.get("preconditions", [])],
            "steps": case["steps"],
            "expected": str(case["expected"]),
        }
        result.append(entry)
    return result


async def _inspect_e2e_surface(
    base_url: str, ignore_https_errors: bool, auth: dict[str, Any] | None = None
) -> dict[str, Any]:
    """只读探测根路径与登录页，产出可验证的路由和语义定位器白名单。"""
    base = base_url.rstrip("/") + "/"
    routes: list[dict[str, Any]] = []
    for path in ("/", "/login"):
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                context = await browser.new_context(ignore_https_errors=ignore_https_errors)
                page = await context.new_page()
                try:
                    await page.goto(
                        urljoin(base, path.lstrip("/")),
                        wait_until="domcontentloaded",
                        timeout=15_000,
                    )
                    await page.wait_for_timeout(300)
                    elements = await page.locator(
                        "input, textarea, select, button, a"
                    ).evaluate_all(
                        """nodes => nodes.slice(0, 80).map(n => ({tag:n.tagName.toLowerCase(),
                        text:(n.innerText || n.textContent || '').trim(),
                        aria:n.getAttribute('aria-label'),
                        placeholder:n.getAttribute('placeholder')}))"""
                    )
                    selectors = _observed_selectors(elements)
                    if selectors:
                        routes.append(
                            {
                                "path": path,
                                "title": await page.title(),
                                "selectors": selectors,
                                "visible_text": (await page.locator("body").inner_text())[:800],
                            }
                        )
                finally:
                    await context.close()
                    await browser.close()
        except PlaywrightError:
            continue
    if auth:
        routes.extend(await _inspect_authenticated_routes(base_url, ignore_https_errors, auth))
    unique = {str(route["path"]): route for route in routes}
    return {"base_url": base_url.rstrip("/"), "routes": list(unique.values())}


async def _inspect_authenticated_routes(
    base_url: str, ignore_https_errors: bool, auth: dict[str, Any]
) -> list[dict[str, Any]]:
    """真实登录后探测首页与导航链接；只读访问，不提交任何业务表单。"""
    base = base_url.rstrip("/") + "/"
    discovered: list[dict[str, Any]] = []
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context(ignore_https_errors=ignore_https_errors)
            page = await context.new_page()
            try:
                await page.goto(
                    urljoin(base, "login"), wait_until="domcontentloaded", timeout=15_000
                )
                await page.get_by_placeholder("用户名", exact=False).fill(
                    str(auth.get("username", ""))
                )
                await page.get_by_placeholder("密码", exact=False).fill(
                    str(auth.get("password", ""))
                )
                await page.get_by_role("button", name="登 录", exact=False).click()
                await page.wait_for_timeout(800)
                paths = {urlparse(page.url).path or "/"}
                for href in await page.locator("a").evaluate_all(
                    "nodes => nodes.map(n => n.getAttribute('href'))"
                ):
                    if isinstance(href, str) and href.startswith("/") and not href.startswith("//"):
                        paths.add(href.split("?", 1)[0])
                for path in sorted(paths)[:10]:
                    await page.goto(
                        urljoin(base, path.lstrip("/")),
                        wait_until="domcontentloaded",
                        timeout=15_000,
                    )
                    await page.wait_for_timeout(200)
                    elements = await page.locator(
                        "input, textarea, select, button, a"
                    ).evaluate_all(
                        """nodes => nodes.slice(0, 80).map(n => ({
                        tag:n.tagName.toLowerCase(),
                        text:(n.innerText || n.textContent || '').trim(),
                        aria:n.getAttribute('aria-label'),
                        placeholder:n.getAttribute('placeholder')}))"""
                    )
                    selectors = _observed_selectors(elements)
                    if selectors:
                        discovered.append(
                            {
                                "path": path,
                                "title": await page.title(),
                                "selectors": selectors,
                                "visible_text": (await page.locator("body").inner_text())[:800],
                            }
                        )
            finally:
                await context.close()
                await browser.close()
    except PlaywrightError:
        return []
    return discovered


def _observed_selectors(elements: list[dict[str, Any]]) -> list[str]:
    selectors: list[str] = []
    for element in elements:
        tag = str(element.get("tag", ""))
        text = " ".join(str(element.get("text") or "").split())
        placeholder = str(element.get("placeholder") or "").strip()
        aria = str(element.get("aria") or "").strip()
        if tag in {"input", "textarea"} and placeholder:
            selectors.append(f"placeholder={placeholder}")
        if aria:
            selectors.append(f"label={aria}")
        if tag == "button" and text:
            selectors.append(f"role=button[name={text}]")
        if tag == "a" and text:
            selectors.append(f"role=link[name={text}]")
    return list(dict.fromkeys(selectors))


def _parse_e2e_cases(
    text: str,
    allowed_selectors: set[str] | None = None,
    allowed_paths: set[str] | None = None,
) -> list[dict[str, Any]]:
    """严格收敛 E2E 动作白名单，避免模型产生任意 JS 或危险浏览器操作。"""
    try:
        payload = json.loads(_strip_code_fence(text))
    except json.JSONDecodeError as exc:
        raise PipelineError(
            code=X_LLM_02, message=f"E2E 用例输出不是合法 JSON：{exc}", node_id=NODE_NAME
        ) from exc
    # 不同模型常把同一集合命名为 e2e_cases/items/results，或直接输出数组。
    # 仅归一化容器名；条目字段、动作和真实 DOM 白名单仍在下方严格校验。
    entries: Any
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = next(
            (
                payload.get(key)
                for key in ("cases", "e2e_cases", "items", "results")
                if isinstance(payload.get(key), list)
            ),
            None,
        )
        if entries is None and isinstance(payload.get("data"), dict):
            data = payload["data"]
            entries = next(
                (
                    data.get(key)
                    for key in ("cases", "e2e_cases", "items")
                    if isinstance(data.get(key), list)
                ),
                None,
            )
    else:
        entries = None
    if not isinstance(entries, list):
        raise PipelineError(
            code=X_LLM_02,
            message="E2E 用例输出缺少 cases 数组（或结构不符）",
            node_id=NODE_NAME,
        )
    allowed_actions = {"goto", "click", "fill", "check", "select", "expect_text", "expect_url"}
    cases: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(key), str) and entry[key].strip()
            for key in ("requirement_id", "title", "expected")
        ):
            raise PipelineError(
                code=X_LLM_02, message="E2E 用例缺少需求、标题或预期", node_id=NODE_NAME
            )
        steps = entry.get("steps")
        if not isinstance(steps, list) or not steps:
            raise PipelineError(code=X_LLM_02, message="E2E 用例缺少步骤", node_id=NODE_NAME)
        normalized_steps: list[dict[str, str]] = []
        for step in steps:
            if not isinstance(step, dict) or str(step.get("action", "")) not in allowed_actions:
                raise PipelineError(
                    code=X_LLM_02, message="E2E 用例含不支持的浏览器动作", node_id=NODE_NAME
                )
            action = str(step["action"])
            value = step.get("value")
            selector = step.get("selector")
            if action == "goto":
                if not isinstance(value, str) or not value.startswith("/"):
                    raise PipelineError(
                        code=X_LLM_02,
                        message="E2E goto 仅允许站内相对路径",
                        node_id=NODE_NAME,
                    )
                if allowed_paths is not None and value not in allowed_paths:
                    raise PipelineError(
                        code=X_LLM_02,
                        message=f"E2E goto 不是已探测路由：{value}",
                        node_id=NODE_NAME,
                    )
                normalized_steps.append({"action": action, "value": value})
            elif action == "expect_url":
                # URL 断言不定位单个元素；Prompt 明确允许 selector 为空。
                if not isinstance(value, str) or not value.startswith("/"):
                    raise PipelineError(
                        code=X_LLM_02,
                        message="E2E expect_url 仅允许站内相对路径",
                        node_id=NODE_NAME,
                    )
                normalized_steps.append({"action": action, "value": value})
            elif not isinstance(selector, str) or not selector.strip():
                raise PipelineError(
                    code=X_LLM_02, message="E2E 步骤缺少 selector", node_id=NODE_NAME
                )
            else:
                if allowed_selectors is not None and selector not in allowed_selectors:
                    raise PipelineError(
                        code=X_LLM_02,
                        message=f"E2E selector 未在真实页面探测结果中出现：{selector}",
                        node_id=NODE_NAME,
                    )
                normalized = {"action": action, "selector": selector}
                if value is not None:
                    normalized["value"] = str(value)
                normalized_steps.append(normalized)
        copied = dict(entry)
        copied["steps"] = normalized_steps
        cases.append(copied)
    return cases


def _parse_llm_cases(text: str, interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """解析 LLM 输出 cases：结构校验 + interface_id 虚构防护。

    Raises:
        PipelineError: X-LLM-02——不可解析 / 缺 cases / 虚构 interface_id / 非法 category。
    """
    cleaned = _strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise PipelineError(
            code=X_LLM_02,
            message=f"LLM 输出不是合法 JSON：{exc}",
            node_id=NODE_NAME,
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise PipelineError(
            code=X_LLM_02,
            message="LLM 输出缺少 cases 数组（或结构不符）",
            node_id=NODE_NAME,
        )
    if_ids = {str(i.get("id", "")) for i in interfaces}
    cases: list[dict[str, Any]] = []
    for raw in data["cases"]:
        if not isinstance(raw, dict):
            raise PipelineError(
                code=X_LLM_02,
                message="LLM 输出的 cases 含非对象条目",
                node_id=NODE_NAME,
            )
        if_id = str(raw.get("interface_id", ""))
        if if_id not in if_ids:
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 引用了不存在的接口：{if_id}（禁止虚构）",
                node_id=NODE_NAME,
            )
        category = str(raw.get("category", ""))
        if category not in _VALID_CATEGORIES:
            raise PipelineError(
                code=X_LLM_02,
                message=f"LLM 输出了非法 category：{category}（三类之外）",
                node_id=NODE_NAME,
            )
        cases.append(dict(raw))
    return cases


def _filter_param_violations(
    cases: list[dict[str, Any]], interfaces: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """R-GEN-02：params 参数名 ⊆ 接口定义参数集合。

    Returns:
        (通过校验的用例, 违规记录清单[{interface_id, params, message}]).
    """
    params_by_if: dict[str, set[str]] = {}
    for interface in interfaces:
        if_id = str(interface.get("id", ""))
        params_by_if[if_id] = {
            str(p.get("name")) for p in interface.get("params") or [] if p.get("name")
        }
    valid: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for case in cases:
        if_id = str(case.get("interface_id", ""))
        allowed = params_by_if.get(if_id, set())
        extra = (
            {str(k) for k in (case.get("params") or {}).keys() if str(k) not in allowed}
            if isinstance(case.get("params"), dict)
            else set()
        )
        if extra:
            violations.append(
                {
                    "interface_id": if_id,
                    "params": case.get("params"),
                    "message": f"清单外参数：{sorted(extra)}（允许：{sorted(allowed) or '无'})",
                }
            )
        else:
            valid.append(case)
    return valid, violations


def _merge_rounds(
    first: list[dict[str, Any]], second: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """合并两轮通过校验的用例（同接口同类目去重，第一轮优先）。"""
    seen: set[tuple[str, str]] = set()
    merged: list[dict[str, Any]] = []
    for case in first + second:
        key = (str(case.get("interface_id", "")), str(case.get("category", "")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(case)
    return merged


def _finalize_cases(
    cases: list[dict[str, Any]],
    job_id: str,
    store: ArtifactStore,
    interfaces: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """补全元数据并渲染脚本：TC-### / script_path / loadable / low_confidence。

    Returns:
        (最终用例清单, 是否存在不可加载脚本).
    """
    # unknown 参数接口集合（X-GEN-03 判定基准：接口定义为准，不经 LLM）
    unknown_ifs: set[str] = {
        str(interface.get("id", ""))
        for interface in interfaces
        if any(str(p.get("type")) == "unknown" for p in interface.get("params") or [])
    }
    finalized: list[dict[str, Any]] = []
    any_unloadable = False
    for seq, case in enumerate(cases, start=1):
        case_id = f"TC-{seq:03d}"
        entry: dict[str, Any] = {
            "id": case_id,
            "interface_id": case["interface_id"],
            "category": case["category"],
            "title": str(case.get("title", f"{case['interface_id']} {case['category']} 用例")),
            "expected": str(case.get("expected", "")),
        }
        if case.get("requirement_id"):
            entry["requirement_id"] = str(case["requirement_id"])
        if isinstance(case.get("params"), dict) and case["params"]:
            entry["params"] = case["params"]
        if str(case.get("auth_mode", "")) in {"inherit", "missing", "invalid"}:
            entry["auth_mode"] = str(case["auth_mode"])

        # low_confidence：接口定义含 unknown 参数（X-GEN-03，代码判定）
        if case["interface_id"] in unknown_ifs:
            entry["low_confidence"] = True

        # 脚本渲染（R-GEN-03：py_compile 校验 loadable）
        # 文件名/函数名用下划线形式（test_TC_001.py / test_tc_001）：pytest 可收集
        stem = case_id.replace("-", "_")
        rel_path = f"scripts/test_{stem}.py"
        script = _SCRIPT_TEMPLATE.format(
            case_id=case_id,
            title=entry["title"],
            interface_id=entry["interface_id"],
            requirement_id=entry.get("requirement_id", "-"),
            job_id=job_id,
            params=entry.get("params", {}),
            expected=entry["expected"],
            func_name=f"test_{stem.lower()}",
        )
        store.write_raw(job_id, rel_path, script)
        entry["script_path"] = rel_path
        entry["loadable"] = _py_compile_ok(store.job_dir(job_id) / rel_path, rel_path)
        any_unloadable = any_unloadable or not entry["loadable"]
        finalized.append(entry)
    return finalized, any_unloadable


def _py_compile_ok(path: Path, rel_path: str) -> bool:
    """R-GEN-03：语法校验（py_compile 语义；失败不中断，记 loadable=false）。"""
    try:
        source = path.read_text(encoding="utf-8")
        compile(source, rel_path, "exec")
    except (OSError, SyntaxError, ValueError):
        return False
    return True
