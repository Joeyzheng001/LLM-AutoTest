"""Business browser execution with per-step evidence and immutable assertions."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from common.e2e_contracts import ASSERTIONS, fingerprint, same_origin, validate_case
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright, expect

from executor.e2e.evidence import Evidence, now, redact, safe_url


class Blocked(Exception):
    pass


def locator(scope: Any, selector: str) -> Any:
    for prefix, method in [
        ("text=", "get_by_text"),
        ("label=", "get_by_label"),
        ("placeholder=", "get_by_placeholder"),
        ("testid=", "get_by_test_id"),
    ]:
        if selector.startswith(prefix):
            kwargs = {} if prefix == "testid=" else {"exact": True}
            return getattr(scope, method)(selector[len(prefix) :], **kwargs)
    matched = re.fullmatch(r"role=([\w-]+)(?:\[name=(.*)\])?", selector)
    if matched:
        return scope.get_by_role(matched[1], name=matched[2], exact=True)
    raise Blocked(f"不支持的语义定位器：{selector}")


def resolve(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {k: resolve(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, variables) for v in value]
    if not isinstance(value, str):
        return value
    exact = re.fullmatch(r"\{\{([A-Z][A-Z0-9_]*)\}\}", value)
    if exact:
        if exact[1] not in variables:
            raise Blocked(f"变量 {exact[1]} 尚未由准备或提取动作生成")
        return variables[exact[1]]

    def replace(match: re.Match[str]) -> str:
        if match[1] not in variables:
            raise Blocked(f"变量 {match[1]} 尚未准备")
        return str(variables[match[1]])

    return re.sub(r"\{\{([A-Z][A-Z0-9_]*)\}\}", replace, value)


def frame_scope(page: Any, frame: dict[str, Any] | None) -> Any:
    if not frame:
        return page
    matches = [
        f
        for f in page.frames
        if (not frame.get("name") or f.name == frame["name"])
        and (
            not frame.get("url")
            or f.url == frame["url"]
            or (urlsplit(f.url)._replace(query="", fragment="").geturl() == frame["url"])
        )
    ]
    if len(matches) != 1:
        raise Blocked(f"目标 frame 尚未唯一定位：{frame}")
    return matches[0]


async def execute_case(
    case: dict[str, Any],
    config: dict[str, Any],
    writer: Callable[[str, bytes], str],
    auth: dict[str, Any] | None = None,
    auth_headers: dict[str, str] | None = None,
    recover: Callable[..., Any] | None = None,
    progress: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    run_id = str(config.get("run_id") or uuid.uuid4().hex)
    started = time.monotonic()
    auth = config.get("roles", {}).get(case.get("role")) or auth or {}
    evidence = Evidence(
        run_id,
        str(case["id"]),
        writer,
        tuple(
            str(profile.get(k, ""))
            for profile in [auth, *config.get("roles", {}).values()]
            for k in ("username", "password")
        ),
    )
    result: dict[str, Any] = {
        "case_id": case["id"],
        "case_type": "e2e",
        "interface_id": "UI-E2E-001",
        "timestamp": now(),
        "run_id": run_id,
        "title": case.get("title", ""),
        "expected": case.get("expected", ""),
        "passed": False,
        "assertion_scope": "ui_steps",
        "category": case.get("scenario_type")
        if case.get("scenario_type") in {"normal", "boundary", "exception"}
        else "e2e",
        "scenario_type": case.get("scenario_type", "normal"),
        "step_results": [],
        "assertion_results": [],
        "execution_state": "not_executed",
        "outcome": "blocked",
        "rule_refs": case.get("rule_refs", []),
    }
    for key in ("requirement_id", "claim_id", "testpoint_id", "priority"):
        if case.get(key):
            result[key] = case[key]
    try:
        case = validate_case(case)
    except ValueError as exc:
        result.update({"failure_reason": str(exc), "execution_blockers": [str(exc)]})
        return result
    if case.get("execution_blockers"):
        result.update(
            {
                "failure_reason": "；".join(case["execution_blockers"]),
                "execution_blockers": case["execution_blockers"],
            }
        )
        return result
    variables: dict[str, Any] = {
        "RUN_ID": run_id,
        "CASE_ID": case["id"],
        "UNIQUE": uuid.uuid4().hex,
        "AUTH_USERNAME": auth.get("username", ""),
        "AUTH_PASSWORD": auth.get("password", ""),
    }
    base_url = str(config["base_url"]).rstrip("/")
    records: list[dict[str, Any]] = []
    console: list[dict[str, Any]] = []
    captures: set[asyncio.Task[Any]] = set()
    pages: dict[str, Any] = {}
    actor_contexts: list[Any] = []
    phase = "setup"
    current: dict[str, Any] | None = None
    failed_current: dict[str, Any] | None = None
    context = browser = None
    cleanup_failed = False
    trace_path: Path | None = None

    async def response_record(response: Any) -> None:
        record: dict[str, Any] = {
            "timestamp": now(),
            "url": safe_url(response.url),
            "status": response.status,
            "method": response.request.method,
        }
        try:
            headers = await response.all_headers()
            record["headers"] = headers
            record["request"] = {
                "headers": await response.request.all_headers(),
                "body": response.request.post_data,
            }
            if "json" in headers.get("content-type", "") or "text/" in headers.get(
                "content-type", ""
            ):
                text = await response.text()
                try:
                    record["body"] = json.loads(text)
                except ValueError:
                    record["body"] = text
        except Exception as exc:
            record["capture_error"] = str(exc)
        records.append(record)

    def listen(page: Any) -> None:
        page.on(
            "console",
            lambda m: console.append({"type": m.type, "text": m.text, "timestamp": now()}),
        )
        page.on(
            "pageerror",
            lambda e: console.append({"type": "pageerror", "text": str(e), "timestamp": now()}),
        )
        page.on(
            "requestfailed",
            lambda r: records.append(
                {
                    "timestamp": now(),
                    "url": safe_url(r.url),
                    "method": r.method,
                    "failure": r.failure,
                }
            ),
        )

        def record(response: Any) -> None:
            task = asyncio.create_task(response_record(response))
            captures.add(task)
            task.add_done_callback(captures.discard)

        page.on("response", record)

    async def step_run(raw: dict[str, Any], index: int, step_phase: str) -> None:
        nonlocal current
        step = resolve(raw, variables)
        page = pages.get(str(step.get("page", "main")))
        if page is None and step["action"] not in {"open_popup", "open_actor"}:
            raise Blocked(f"窗口 {step.get('page')} 未打开")
        if step["action"] in {"open_popup", "open_actor"}:
            page = pages["main"]
        if page is None:
            raise Blocked("目标窗口不可用")
        step_id = f"{step_phase}-{index:04d}"
        current = {
            "step_id": step_id,
            "phase": step_phase,
            "index": index,
            "intent": step["intent"],
            "action": step["action"],
            "step": redact(raw),
            "started_at": now(),
            "status": "running",
            "page_url_before": safe_url(page.url),
            "frame": step.get("frame"),
        }
        result["step_results"].append(current)
        tick = time.monotonic()
        current["before_ref"] = await evidence.screenshot(page, f"{step_id}-before")
        observed = await evidence.observe(page, f"{step_id}-before-state")
        if progress:
            progress(
                {
                    "event": "step_started",
                    "case_id": case["id"],
                    "step_id": step_id,
                    "intent": step["intent"],
                }
            )
        target = None
        assertion_invoked = False
        try:
            scope = page
            from executor.e2e.planner import direct_target

            exact_target = direct_target(raw, observed["observation"])
            if exact_target:
                step = {**step, **exact_target}
                if "select_value" in exact_target:
                    step.update({"action": "select", "value": exact_target["select_value"]})
                current["recovery"] = exact_target
            if step.get("target_intent") and not step.get("selector"):
                if recover is None:
                    raise Blocked("当前控件需要现场观察定位，尚未配置任务模型")
                replacement = await recover(raw, observed["observation"])
                if not replacement:
                    raise Blocked("现场观察未找到唯一匹配的业务控件")
                step = {**step, **replacement}
                if "select_value" in replacement:
                    step.update({"action": "select", "value": replacement["select_value"]})
                scope = frame_scope(page, step.get("frame"))
                for container in step.get("within") or []:
                    scope = locator(scope, container)
                current["recovery"] = replacement
            scope = frame_scope(page, step.get("frame"))
            for container in step.get("within") or []:
                scope = locator(scope, container)
            target = locator(scope, step["selector"]) if step.get("selector") else None
            # Only locator recovery is permitted. A submitted action is never
            # replayed after a timeout: its side effects may already have happened.
            if (
                target is not None
                and step["action"] not in ASSERTIONS
                and step["action"] != "upload"
            ):
                try:
                    await target.wait_for(
                        state="visible", timeout=int(config.get("step_timeout_ms", 15000))
                    )
                except BrowserError:
                    if recover is None:
                        raise Blocked("目标控件不可达，尚不能归因为产品缺陷") from None
                    latest = await evidence.observe(page, f"{step_id}-relocate-state")
                    replacement = await recover(raw, latest["observation"])
                    if not replacement:
                        raise Blocked("自动重新定位未取得唯一控件，保留原业务意图") from None
                    step = {**step, **replacement}
                    if "select_value" in replacement:
                        step.update({"action": "select", "value": replacement["select_value"]})
                    scope = frame_scope(page, step.get("frame"))
                    for container in step.get("within") or []:
                        scope = locator(scope, container)
                    target = locator(scope, step["selector"])
                    await target.wait_for(state="visible")
                    current["recovery"] = replacement
            old_pages = set(pages)
            if step["action"] == "open_actor":
                if browser is None:
                    raise Blocked("浏览器尚未准备")
                profile = config.get("roles", {}).get(step.get("resource"))
                if not isinstance(profile, dict):
                    raise Blocked("目标业务角色未配置登录资料")
                actor_context = await browser.new_context(
                    ignore_https_errors=bool(config.get("insecure_tls", True)),
                    accept_downloads=True,
                )
                actor_contexts.append(actor_context)
                actor_page = await actor_context.new_page()
                actor_page.set_default_timeout(int(config.get("step_timeout_ms", 15000)))
                pages[str(step.get("page") or step["resource"])] = actor_page
                from executor.nodes.e2e_surface import _fill_login, _submit_login

                await actor_page.goto(
                    str(
                        profile.get("browser_login_url")
                        or config.get("browser_login_url")
                        or urljoin(base_url + "/", "login")
                    )
                )
                if not await _fill_login(actor_page, profile) or not await _submit_login(
                    actor_page
                ):
                    raise Blocked("指定角色未完成登录")
                page = actor_page
                actual = {
                    "actor": step["resource"],
                    "page": str(step.get("page") or step["resource"]),
                }
            else:
                assertion_invoked = step["action"] in ASSERTIONS
                actual = await action(
                    step,
                    page,
                    scope,
                    target,
                    pages,
                    variables,
                    records,
                    config,
                    base_url,
                    evidence,
                    step_id,
                )
            if step["action"] in {"open_popup", "open_actor"}:
                page = pages[str(step.get("page") or step.get("resource"))]
            elif step["action"] == "close_page":
                page = pages["main"]
            for name in set(pages) - old_pages:
                listen(pages[name])
            if step["action"] in ASSERTIONS:
                assertion = {
                    "path": step.get("selector") or step.get("path") or "page.url",
                    "op": step["action"],
                    "expected": step.get("value"),
                    "actual": actual,
                    "found": True,
                    "passed": True,
                }
                result["assertion_results"].append(assertion)
                current["assertion"] = assertion
            current["status"] = "passed"
            current["actual"] = actual
        except BaseException as exc:
            current.update(
                {"status": "failed", "error": str(exc), "error_type": type(exc).__name__}
            )
            if assertion_invoked and not isinstance(exc, Blocked):
                assertion = {
                    "path": step.get("selector") or step.get("path") or "page.url",
                    "op": step["action"],
                    "expected": step.get("value"),
                    "actual": await assertion_actual(step, page, target, records),
                    "found": await target_exists(target),
                    "passed": False,
                }
                result["assertion_results"].append(assertion)
                current["assertion"] = assertion
            raise
        finally:
            current["duration_ms"] = int((time.monotonic() - tick) * 1000)
            current["after_ref"] = await evidence.screenshot(page, f"{step_id}-after")
            current["after_state_ref"] = (await evidence.observe(page, f"{step_id}-after-state"))[
                "snapshot_ref"
            ]
            current["page_url_after"] = safe_url(page.url)
            evidence.write("progress.json", result["step_results"])
            if progress:
                progress(
                    {
                        "event": "step_completed",
                        "case_id": case["id"],
                        "step_id": step_id,
                        "status": current["status"],
                        "step_result": redact(current, evidence.secrets),
                    }
                )

    try:
        async with async_playwright() as pw:
            launch = {"channel": config["browser_channel"]} if config.get("browser_channel") else {}
            browser = await pw.chromium.launch(**launch)
            try:
                context_options: dict[str, Any] = {
                    "ignore_https_errors": bool(config.get("insecure_tls", True)),
                    "accept_downloads": True,
                }
                if config.get("storage_state") and case.get("role") != "anonymous":
                    context_options["storage_state"] = config["storage_state"]
                context = await browser.new_context(**context_options)
                await context.tracing.start(screenshots=True, snapshots=True, sources=True)
                page = await context.new_page()
                pages["main"] = page
                listen(page)
                page.set_default_timeout(int(config.get("step_timeout_ms", 15000)))
                page.set_default_navigation_timeout(int(config.get("navigation_timeout_ms", 30000)))
                if (
                    config.get("auth_mode") == "reuse"
                    and auth_headers
                    and case.get("role", "default") == "default"
                ):

                    async def attach(route: Any) -> None:
                        if same_origin(base_url, route.request.url):
                            headers = dict(await route.request.all_headers())
                            headers.update(auth_headers)
                            await route.continue_(headers=headers)
                        else:
                            await route.continue_()

                    await context.route("**/*", attach)
                elif (
                    auth
                    and case.get("role") != "anonymous"
                    and config.get("auto_login", True)
                    and not config.get("storage_state")
                ):
                    from executor.nodes.e2e_surface import _fill_login, _submit_login

                    await page.goto(
                        str(
                            config.get("browser_login_url")
                            or auth.get("browser_login_url")
                            or urljoin(base_url + "/", "login")
                        )
                    )
                    if not await _fill_login(page, auth) or not await _submit_login(page):
                        raise Blocked("浏览器登录未完成，业务用例尚未开始")
                try:
                    for phase in ("setup", "precondition_checks", "steps"):
                        for index, step in enumerate(case.get(phase, []), 1):
                            result["execution_state"] = "executed"
                            await step_run(step, index, phase)
                    result.update(
                        {"passed": True, "outcome": "passed", "execution_state": "executed"}
                    )
                except Exception:
                    failed_current = current
                    raise
                finally:
                    for index, step in enumerate(case.get("cleanup", []), 1):
                        try:
                            await step_run(step, index, "cleanup")
                        except Exception as exc:
                            cleanup_failed = True
                            result.setdefault("cleanup_errors", []).append(str(exc))
            finally:
                if context:
                    if captures:
                        await asyncio.gather(*list(captures), return_exceptions=True)
                    evidence.write("network.json", records)
                    evidence.write("console.json", console)
                    with tempfile.TemporaryDirectory() as directory:
                        trace_path = Path(directory) / "trace.zip"
                        try:
                            await context.tracing.stop(path=str(trace_path))
                            # Playwright traces may include secrets; raw traces
                            # are opt-in until a complete trace redactor exists.
                            if config.get("retain_trace", False):
                                result["trace_ref"] = evidence.write(
                                    "trace.zip", trace_path.read_bytes()
                                )
                        except Exception as exc:
                            evidence.errors.append(f"trace: {exc}")
                    result["evidence"] = {
                        "kind": "browser",
                        "page_url": safe_url(pages["main"].url),
                        "screenshot_ref": await evidence.screenshot(pages["main"], "final"),
                        "network_ref": f"{evidence.prefix}/network.json",
                        "console_ref": f"{evidence.prefix}/console.json",
                    }
                    for actor_context in actor_contexts:
                        await actor_context.close()
                    await context.close()
                if browser:
                    await browser.close()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if failed_current is not None:
            current = failed_current
        assertion_failure = (
            phase == "steps"
            and isinstance(exc, AssertionError)
            and "strict mode violation" not in str(exc).lower()
        )
        if assertion_failure:
            result.update(
                {
                    "outcome": "assertion_failed",
                    "failure_kind": "assertion",
                    "execution_state": "executed",
                }
            )
        elif isinstance(exc, BrowserError) and any(
            marker in str(exc)
            for marker in (
                "Executable doesn",
                "ERR_CONNECTION",
                "ERR_NAME_",
                "ERR_CERT_",
                "browserType.launch",
            )
        ):
            result.update(
                {
                    "outcome": "environment_failed",
                    "failure_kind": "environment",
                    "execution_state": "executed",
                }
            )
        elif isinstance(exc, Blocked) or phase in {"setup", "precondition_checks"}:
            result.update(
                {
                    "outcome": "blocked",
                    "execution_state": "not_executed"
                    if not result["step_results"]
                    else "observed_unjudged",
                }
            )
        elif isinstance(exc, (BrowserTimeout, BrowserError, AssertionError)):
            result.update(
                {
                    "outcome": "automation_error",
                    "failure_kind": "environment",
                    "execution_state": "executed",
                }
            )
        else:
            result.update(
                {
                    "outcome": "environment_failed",
                    "failure_kind": "environment",
                    "execution_state": "executed",
                }
            )
        result.update(
            {
                "passed": False,
                "failure_reason": str(exc),
                "failed_step_id": current.get("step_id") if current else None,
            }
        )
    if cleanup_failed:
        result["passed"] = False
        if result.get("outcome") == "passed":
            result.update(
                {
                    "outcome": "automation_error",
                    "failure_kind": "environment",
                    "failure_reason": "数据清理未完成，详见清理步骤",
                }
            )
    if not result["assertion_results"] and result["passed"]:
        result.update(
            {
                "passed": False,
                "outcome": "unjudged",
                "execution_state": "observed_unjudged",
                "failure_reason": "业务断言未执行",
            }
        )
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    result["evidence_complete"] = not evidence.errors
    result["evidence_errors"] = evidence.errors
    result["evidence_files"] = evidence.files
    result["network_requests"] = len(records)
    result["case_fingerprint"] = fingerprint(
        {
            k: case.get(k)
            for k in (
                "rule_refs",
                "scenario_type",
                "role",
                "precondition_checks",
                "expected",
                "steps",
                "setup",
                "cleanup",
            )
        }
    )
    evidence.write("result.json", result)
    return redact(result, evidence.secrets)


async def action(
    step: dict[str, Any],
    page: Any,
    scope: Any,
    target: Any,
    pages: dict[str, Any],
    variables: dict[str, Any],
    records: list[dict[str, Any]],
    config: dict[str, Any],
    base_url: str,
    evidence: Evidence,
    step_id: str,
) -> Any:
    kind, value = step["action"], step.get("value")
    origin = urlsplit(base_url)
    site_url = f"{origin.scheme}://{origin.netloc}"
    if kind == "goto":
        await page.goto(site_url + str(value), wait_until="domcontentloaded")
    elif kind in {"click", "dblclick", "check", "uncheck", "hover"}:
        await getattr(target, kind)()
    elif kind == "fill":
        await target.fill(str(value))
    elif kind == "press":
        await target.press(str(value))
    elif kind == "select":
        await target.select_option(value)
    elif kind == "scroll":
        await target.scroll_into_view_if_needed()
    elif kind == "drag":
        await target.drag_to(locator(scope, step["destination"]))
    elif kind == "capture_text":
        variables[step["variable"]] = await target.inner_text()
        return variables[step["variable"]]
    elif kind == "capture_value":
        variables[step["variable"]] = await target.input_value()
        return variables[step["variable"]]
    elif kind == "upload":
        resource = config.get("files", {}).get(step["resource"])
        if not resource or not Path(resource).is_file():
            raise Blocked("上传文件未在任务 files 中准备")
        await target.set_input_files(resource)
    elif kind == "download":
        async with page.expect_download() as pending:
            await target.click()
        download = await pending
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "download"
            await download.save_as(output)
            suffix = Path(download.suggested_filename).suffix.lower()
            if suffix not in {".pdf", ".csv", ".xlsx", ".txt", ".zip", ".json"}:
                raise Blocked(f"下载类型尚不支持留存：{suffix}")
            ref = evidence.write(f"{step_id}-download{suffix}", output.read_bytes())
            return {"filename": download.suggested_filename, "evidence_ref": ref}
    elif kind == "open_popup":
        async with page.expect_popup() as pending:
            await target.click()
        pages[step["page"]] = await pending
        await pages[step["page"]].wait_for_load_state("domcontentloaded")
    elif kind == "close_page":
        if step["page"] == "main":
            raise Blocked("主窗口保留到证据与清理结束")
        await pages.pop(step["page"]).close()
    elif kind == "screenshot":
        return await evidence.screenshot(page, f"{step_id}-checkpoint")
    elif kind == "fixture":
        definition = config.get("data_fixtures", {}).get(step["resource"])
        if not isinstance(definition, dict):
            raise Blocked(f"测试数据准备未配置：{step['resource']}")
        path = str(definition.get("path", ""))
        if not path.startswith("/") or path.startswith("//"):
            raise Blocked("准备接口只能使用配置的站内路径")
        response = await page.context.request.fetch(
            site_url + path,
            method=definition.get("method", "POST"),
            data=resolve(definition.get("body"), variables),
            headers=definition.get("headers"),
            params=resolve(definition.get("query"), variables),
        )
        if response.status not in definition.get("expected_statuses", [200]):
            raise Blocked(f"准备接口状态 {response.status}")
        body = await response.json()
        for name, path in definition.get("extract", {}).items():
            found = body
            for part in str(path).split("."):
                found = found[int(part)] if isinstance(found, list) else found[part]
            variables[name] = found
        return {"status": response.status, "body": body}
    elif kind == "expect_url":
        await expect(page).to_have_url(
            re.compile(re.escape(site_url + str(value)) + r"(?:[?#].*)?$")
        )
        return safe_url(page.url)
    elif kind == "expect_text":
        await expect(target).to_contain_text(str(value))
        return await target.inner_text()
    elif kind == "expect_exact_text":
        await expect(target).to_have_text(str(value))
        return await target.inner_text()
    elif kind == "expect_visible":
        await expect(target).to_be_visible()
        return True
    elif kind == "expect_hidden":
        await expect(target).to_be_hidden()
        return True
    elif kind == "expect_value":
        await expect(target).to_have_value(str(value))
        return await target.input_value()
    elif kind == "expect_count":
        await expect(target).to_have_count(int(str(value)))
        return await target.count()
    elif kind == "expect_checked":
        await expect(target).to_be_checked()
        return await target.is_checked()
    elif kind == "expect_unchecked":
        await expect(target).not_to_be_checked()
        return await target.is_checked()
    elif kind == "expect_enabled":
        await expect(target).to_be_enabled()
        return await target.is_enabled()
    elif kind == "expect_disabled":
        await expect(target).to_be_disabled()
        return await target.is_disabled()
    elif kind == "expect_options":
        actual = await target.locator("option").all_text_contents()
        if actual != value:
            raise AssertionError(f"选项集合不一致：实际 {actual!r}，预期 {value!r}")
        return actual
    elif kind == "expect_response":
        deadline = time.monotonic() + int(config.get("step_timeout_ms", 15000)) / 1000
        matched = []
        while time.monotonic() < deadline:
            matched = [
                r
                for r in records
                if r.get("method") == step["method"]
                and fnmatch.fnmatch(urlsplit(r.get("url", "")).path, step["path"])
            ]
            if matched:
                break
            await asyncio.sleep(0.1)
        if not matched:
            raise AssertionError(f"未观察到业务请求 {step['method']} {step['path']}")
        actual = matched[-1]
        for condition in step.get("assertions", []):
            from common.response_assertions import evaluate_response_assertions

            outcomes = evaluate_response_assertions(actual, [condition])
            if not all(o["passed"] for o in outcomes):
                raise AssertionError(json.dumps(outcomes, ensure_ascii=False))
        return actual
    else:
        raise Blocked(f"尚不支持动作 {kind}")
    return None


async def assertion_actual(
    step: dict[str, Any], page: Any, target: Any, records: list[dict[str, Any]]
) -> Any:
    try:
        kind = step["action"]
        if kind == "expect_url":
            return safe_url(page.url)
        if kind == "expect_response":
            return [
                r
                for r in records
                if r.get("method") == step.get("method")
                and fnmatch.fnmatch(urlsplit(r.get("url", "")).path, step.get("path", ""))
            ]
        count = await target.count()
        if kind == "expect_count":
            return count
        if count != 1:
            return {"matching_elements": count}
        if kind in {"expect_text", "expect_exact_text"}:
            return await target.inner_text(timeout=1000)
        if kind == "expect_value":
            return await target.input_value(timeout=1000)
        if kind in {"expect_checked", "expect_unchecked"}:
            return await target.is_checked(timeout=1000)
        if kind in {"expect_enabled", "expect_disabled"}:
            return await target.is_enabled(timeout=1000)
        if kind in {"expect_visible", "expect_hidden"}:
            return await target.is_visible()
        if kind == "expect_options":
            return await target.locator("option").all_text_contents()
    except Exception as exc:
        return {"capture_error": str(exc)}
    return None


async def target_exists(target: Any) -> bool | None:
    try:
        return await target.count() > 0 if target is not None else True
    except BrowserError:
        return None
