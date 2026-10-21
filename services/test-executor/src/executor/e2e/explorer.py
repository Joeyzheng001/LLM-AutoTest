"""Complete page/state observations with explicit exploration gaps."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from common.e2e_contracts import fingerprint, same_origin
from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from executor.e2e.evidence import Evidence, safe_url
from executor.nodes.e2e_surface import ProbeOptions, _expand_menu_tree, _fill_login, _submit_login


def frame_identity(frame: Any, page: Any) -> dict[str, str] | None:
    if frame == page.main_frame:
        return None
    parsed = urlsplit(frame.url)
    return {
        "url": urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
        "name": frame.name,
    }


def semantic_selectors(element: dict[str, Any]) -> list[str]:
    values = []
    text = " ".join(str(element.get("text") or "").split())
    role = element.get("role") or {"button": "button", "a": "link", "select": "combobox"}.get(
        str(element.get("tag"))
    )
    name = (
        element.get("aria")
        or element.get("label")
        or (text if role in {"button", "link", "tab", "menuitem"} else "")
    )
    if element.get("testid"):
        values.append("testid=" + element["testid"])
    if element.get("aria"):
        values.append("label=" + element["aria"])
    if element.get("placeholder"):
        values.append("placeholder=" + element["placeholder"])
    if element.get("label"):
        values.append("label=" + element["label"])
    if role and name:
        values.append(f"role={role}[name={name}]")
    if element.get("tag") in {"li", "p", "h1", "h2", "h3", "td", "th"} and text:
        values.append("text=" + text)
    if role in {"alert", "status"} and text:
        values.append("text=" + text)
    return list(dict.fromkeys(values))


async def observe(page: Any) -> dict[str, Any]:
    frames = []
    for frame in page.frames:
        if frame.url == "about:blank" and frame != page.main_frame:
            continue
        try:
            elements = await frame.locator(
                "input,textarea,select,button,a,[role],label,[data-testid],li,p,h1,h2,h3,td,th"
            ).evaluate_all("""nodes => nodes.map(n=>({
                tag:n.tagName.toLowerCase(),text:n.innerText||n.textContent||'',aria:n.getAttribute('aria-label'),
                role:n.getAttribute('role'),placeholder:n.getAttribute('placeholder'),testid:n.getAttribute('data-testid'),
                label:n.labels?Array.from(n.labels).map(l=>l.innerText).join(' '):'',
                type:n.getAttribute('type'),value:n.type==='password'?'<redacted>':n.value,
                selected:n.getAttribute('aria-selected'),expanded:n.getAttribute('aria-expanded'),
                disabled:!!n.disabled,checked:n.checked,visible:!!n.getClientRects().length,
                options:n.options?Array.from(n.options).map(o=>({label:o.text,value:o.value,disabled:o.disabled})):undefined
            }))""")
            for element in elements:
                element["selectors"] = semantic_selectors(element)
            frames.append(
                {
                    "identity": frame_identity(frame, page),
                    "url": safe_url(frame.url),
                    "name": frame.name,
                    "elements": elements,
                    "text": await frame.locator("body").inner_text(),
                }
            )
        except BrowserError as exc:
            frames.append(
                {
                    "identity": frame_identity(frame, page),
                    "url": safe_url(frame.url),
                    "error": str(exc),
                }
            )
    return {"url": safe_url(page.url), "title": await page.title(), "frames": frames}


async def explore(
    config: dict[str, Any],
    auth: dict[str, Any] | None = None,
    writer: Callable[[str, bytes], str] | None = None,
    auth_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    import uuid

    run_id = str(config.get("run_id") or uuid.uuid4().hex)
    evidence = (
        Evidence(
            run_id,
            "E2E-000",
            writer,
            tuple(str((auth or {}).get(k, "")) for k in ("username", "password")),
        )
        if writer
        else None
    )
    base = str(config["base_url"]).rstrip("/")
    surface: dict[str, Any] = {
        "contract_version": "2.0",
        "base_url": base,
        "path_basis": "origin",
        "routes": [],
        "warnings": [],
        "pending": [],
    }
    seen = set()
    max_routes = int(config.get("max_routes", 0))

    async def capture(page: Any, state: str, entry_steps: list[dict[str, Any]], role: str) -> None:
        value = await observe(page)
        parsed = urlsplit(page.url)
        path = urlunsplit(("", "", parsed.path or "/", parsed.query, parsed.fragment))
        key = fingerprint({"observation": value, "role": role})
        if key in seen:
            return
        seen.add(key)
        if max_routes and len(surface["routes"]) >= max_routes:
            surface["pending"].append(
                {"url": safe_url(page.url), "state": state, "reason": "探索预算已满，可续跑"}
            )
            return
        identity = "STATE-" + key[:16]
        route = {
            "state_id": identity,
            "path": path,
            "url": safe_url(page.url),
            "title": value["title"],
            "state": state,
            "role": role,
            "entry_steps": entry_steps,
            "frames": value["frames"],
            "selectors": list(
                dict.fromkeys(
                    s
                    for f in value["frames"]
                    for e in f.get("elements", [])
                    for s in e["selectors"]
                )
            ),
        }
        if evidence:
            route["screenshot_ref"] = await evidence.screenshot(page, identity)
            route["snapshot_ref"] = evidence.write(identity + ".json", value)
        surface["routes"].append(route)

    async with async_playwright() as pw:
        launch = {"channel": config["browser_channel"]} if config.get("browser_channel") else {}
        browser = await pw.chromium.launch(**launch)
        try:
            context_options: dict[str, Any] = {
                "ignore_https_errors": bool(config.get("insecure_tls", True))
            }
            if config.get("storage_state"):
                context_options["storage_state"] = config["storage_state"]
            context = await browser.new_context(**context_options)
            try:
                page = await context.new_page()
                page.set_default_timeout(int(config.get("step_timeout_ms", 15000)))
                page.set_default_navigation_timeout(int(config.get("navigation_timeout_ms", 30000)))
                reused = config.get("auth_mode") == "reuse" and (
                    auth_headers or config.get("storage_state")
                )
                if reused and auth_headers:

                    async def attach(route: Any) -> None:
                        if same_origin(base, route.request.url):
                            headers = dict(await route.request.all_headers())
                            headers.update(auth_headers)
                            await route.continue_(headers=headers)
                        else:
                            await route.continue_()

                    await context.route("**/*", attach)
                await page.goto(base, wait_until="load")
                role = "authenticated" if reused else "anonymous"
                await capture(page, "entry", [], role)
                if auth and not reused:
                    await page.goto(
                        str(
                            config.get("browser_login_url")
                            or auth.get("browser_login_url")
                            or urljoin(base + "/", "login")
                        ),
                        wait_until="load",
                    )
                    await capture(page, "login", [], "anonymous")
                    if not await _fill_login(page, auth) or not await _submit_login(page):
                        surface["warnings"].append("浏览器登录失败，已保留未探索的登录后页面")
                        surface["pending"].append(
                            {"state": "authenticated", "reason": "登录未完成"}
                        )
                        return surface
                    role = "authenticated"
                    await capture(page, "authenticated", [], role)
                entries = []
                if config.get("explore_menu", True):
                    options = ProbeOptions.from_config(
                        {**config, "max_routes": max_routes or 1000000}
                    )
                    entries.extend(await _expand_menu_tree(page, options))
                for href in await page.locator("a").evaluate_all(
                    'nodes=>nodes.map(n=>n.getAttribute("href"))'
                ):
                    if isinstance(href, str) and href.startswith("/") and not href.startswith("//"):
                        entries.append({"path": href})
                urls = list(
                    dict.fromkeys([base] + [urljoin(base, str(e["path"])) for e in entries])
                )
                for url in urls:
                    if not same_origin(base, url):
                        continue
                    if max_routes and len(surface["routes"]) >= max_routes:
                        surface["pending"].append({"url": safe_url(url), "reason": "探索预算已满"})
                        continue
                    try:
                        await page.goto(url, wait_until="load")
                        await capture(page, "page", [], role)
                        if not config.get("explore_interactions", True):
                            continue
                        for frame in list(page.frames):
                            frame_ref = frame_identity(frame, page)
                            tabs = frame.get_by_role("tab")
                            for index in range(await tabs.count()):
                                tab = tabs.nth(index)
                                if not await tab.is_visible():
                                    continue
                                label = " ".join((await tab.inner_text()).split())
                                await tab.click()
                                entry_step = {
                                    "action": "click",
                                    "intent": "打开业务标签页",
                                    "selector": f"role=tab[name={label}]",
                                    "frame": frame_ref,
                                }
                                await capture(page, "tab:" + label, [entry_step], role)
                            if config.get("deep_read_options", True):
                                controls = frame.locator('[role="combobox"],.el-select,.ant-select')
                                for index in range(await controls.count()):
                                    control = controls.nth(index)
                                    if not await control.is_visible():
                                        continue
                                    try:
                                        await control.click(timeout=3000)
                                        await capture(page, "expanded-select", [], role)
                                    finally:
                                        await page.keyboard.press("Escape")
                    except BrowserError as exc:
                        surface["pending"].append({"url": safe_url(url), "reason": str(exc)})
            finally:
                await context.close()
        finally:
            await browser.close()
    if evidence:
        surface["evidence_errors"] = evidence.errors
        evidence.write("surface.json", surface)
    return surface


def canonical_target(target: dict[str, Any], frames: list[dict[str, Any]]) -> dict[str, Any]:
    """Bind weak-model frame bookkeeping through unique observed ownership."""
    selector = target.get("selector")
    if not selector:
        return target
    given = target.get("frame")
    owners = []
    for frame in frames:
        if not any(
            selector in element.get("selectors", []) for element in frame.get("elements", [])
        ):
            continue
        identity = frame.get("identity")
        if given is not None and given != identity:
            if not isinstance(given, dict):
                continue
            url_match = not given.get("url") or given["url"] in {
                frame.get("url"),
                (identity or {}).get("url"),
            }
            name_match = not given.get("name") or given["name"] == frame.get("name")
            if not url_match or not name_match:
                continue
        owners.append(frame)
    unique = {json_identity(frame.get("identity")): frame for frame in owners}
    if len(unique) != 1:
        raise ValueError("目标定位器没有唯一可核验的 frame 归属")
    frame = next(iter(unique.values()))
    allowed = {s for element in frame.get("elements", []) for s in element.get("selectors", [])}
    if any(container not in allowed for container in target.get("within") or []):
        raise ValueError("目标容器没有当前 frame 的观察依据")
    return {**target, "frame": frame.get("identity")}


def json_identity(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True)
