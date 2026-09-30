"""E2E 页面探测（surface）：登录后逐层展开菜单，产出可验证的路由与语义定位器白名单。

归属：GEN 节点的 E2E 支线（原实现内联在 testcase_generate.py，因该文件已逾 2200 行
且探测属纯 Playwright 逻辑、与用例生成无关，故抽为独立模块以便单测）。

设计约束（承接原实现的既有契约，消费方不得破坏）：
- 返回结构 ``{"base_url", "path_basis", "routes": [...]}``；每条 route 至少含
  ``path`` / ``selectors`` / ``visible_text``。新增字段一律追加，不改既有键名——
  ``testcase_generate._generate_e2e_cases`` 依赖
  ``{s for r in routes for s in r["selectors"]}`` 与 ``{r["path"] for r in routes}``
  构造白名单，探不到即等于模型不能生成，这是 E2E 用例覆盖面的结构性上游。
- 只读探测：只做导航、展开、读取，绝不提交业务表单、不写数据。
- 探测失败不抛出：E2E 是可选产物，调用方统一降级为告警（X-E2E-01/02）。

相对旧实现的四点能力提升：
1. 菜单树逐层展开——旧版只探 ``/`` 与 ``/login``，深层页面（如设置页里的 tab）
   完全不可达，E2E 用例只能停在首页。
2. 取消每页 80 元素截断——深层页控件多，截断会丢掉后续 selector。
3. 页面内交互探测——点开 tab / 展开下拉 / 打开弹窗，**记录真实选项值**。
   旧版 ``_observed_selectors`` 只收选择器不收值，导致模型看不到页面的真实枚举，
   也就无法发现「UI 取值与需求/后端字典不一致」这类缺陷。
4. 枚举对照的数据基础——``option_values`` 随 surface 传给 E2E 生成 prompt，
   由模型将真实选项值与需求枚举比对（见 e2e_testcase_generate.md 1.2 的
   「枚举一致性校验」规则），不一致时生成暴露差异的用例。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError

logger = logging.getLogger(__name__)

# ── 探测预算（默认值，可由调用方覆盖）────────────────────────────────────────
DEFAULT_MAX_ROUTES = 25
DEFAULT_MAX_ELEMENTS = 400
DEFAULT_MAX_MENU_DEPTH = 4
DEFAULT_MAX_INTERACTIONS = 12
# 单个页面元素取值时的最大收集量，防止超长下拉（如几千条字典项）拖垮探测
MAX_OPTION_VALUES = 60


@dataclass
class ProbeOptions:
    """探测深度与预算；全部可由 E2eConfig 覆盖。

    Attributes:
        explore_menu: 是否逐层展开左侧菜单（关闭则退化为旧版行为）。
        explore_interactions: 是否点开 tab / 下拉 / 弹窗读取真实取值。
        deep_read_options: 是否进一步展开下拉读取选项（比点开 tab 更慢）。
        max_routes: 最多探测多少条路由。
        max_elements: 单页最多收集多少元素（0 表示不限制）。
        max_menu_depth: 菜单展开最大层级。
        max_interactions: 单页最多触发多少次交互探测。
        page_timeout_ms: 单次导航超时。
        settle_ms: 页面稳定等待。
    """

    explore_menu: bool = True
    explore_interactions: bool = True
    deep_read_options: bool = True
    max_routes: int = DEFAULT_MAX_ROUTES
    max_elements: int = DEFAULT_MAX_ELEMENTS
    max_menu_depth: int = DEFAULT_MAX_MENU_DEPTH
    max_interactions: int = DEFAULT_MAX_INTERACTIONS
    page_timeout_ms: int = 15_000
    settle_ms: int = 300
    # 浏览器通道：默认捆绑 chromium；离线环境装不了捆绑浏览器时可用系统
    # Edge/Chrome（"msedge" / "chrome"）。
    browser_channel: str = ""

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> ProbeOptions:
        """从 E2E 配置字典构造；未知键忽略，缺失取默认。

        刻意不抛异常：探测深度是调优项，配置写错不应让 E2E 整条支线失败。
        """
        if not config:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in config.items() if k in known}
        options = cls(**kwargs)
        # 负值保护：max_elements / max_interactions 等按「0 或正数」语义使用
        options.max_routes = max(1, options.max_routes)
        options.max_elements = max(0, options.max_elements)
        options.max_menu_depth = max(1, options.max_menu_depth)
        options.max_interactions = max(0, options.max_interactions)
        return options


@dataclass
class SurfaceResult:
    """探测结果；``routes`` 即白名单来源。"""

    routes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 探测到的「元素文本 -> 选项取值」明细，供枚举对照与调试
    option_values: list[dict[str, Any]] = field(default_factory=list)

    def to_surface(self, base_url: str) -> dict[str, Any]:
        normalized_base = base_url.rstrip("/")
        return {
            "base_url": normalized_base,
            # goto.value 由执行端**字符串拼接**在 base_url 之后（见 auto_execute._run_e2e_step），
            # 所以 routes[].path 是「相对 base_url」的路径，不是站内绝对路径。
            # base_url 自带路径前缀时（如 http://host:6080/home），首页对应 path 是 "/"，
            # 而不是 "/home"——不写清楚模型会按站内绝对路径写，全部被白名单拒绝。
            "path_basis": (
                f"goto/expect_url 的 value 相对 {normalized_base} 拼接；"
                f'例如 value="/" 打开 {normalized_base}/'
            ),
            "routes": self.routes,
        }


# ── 元素收集（放开 80 截断，并补充 name/role 等语义属性）───────────────────────
_ELEMENT_QUERY = """
(nodes, limit) => {
  const list = limit > 0 ? nodes.slice(0, limit) : nodes;
  return list.map(n => ({
    tag: n.tagName.toLowerCase(),
    text: (n.innerText || n.textContent || '').trim(),
    aria: n.getAttribute('aria-label'),
    placeholder: n.getAttribute('placeholder'),
    role: n.getAttribute('role'),
    type: n.getAttribute('type'),
    name: n.getAttribute('name'),
    // el-* / ant-* 类组件库常用 class 表达控件语义，但仅作为最后兜底，不直接进白名单
    cls: n.getAttribute('class'),
  }));
}
"""

# 探测用元素范围：较旧版增加 [role=tab] / li（菜单项常为 li 而非 a）
_ELEMENT_SELECTOR = "input, textarea, select, button, a, [role='tab'], [role='button'], li"


def _observed_selectors(elements: list[dict[str, Any]]) -> list[str]:
    """把探测到的元素转成语义定位器白名单。

    兼容原实现产出的四类（placeholder / label / role=button / role=link），
    并按同样格式补充 tab 与可点击 li（菜单项在两个被测系统里都是 li，
    旧版只收集 a，导致菜单路径整体缺失）。
    """
    selectors: list[str] = []
    for element in elements:
        tag = str(element.get("tag", ""))
        text = " ".join(str(element.get("text") or "").split())
        placeholder = str(element.get("placeholder") or "").strip()
        aria = str(element.get("aria") or "").strip()
        role = str(element.get("role") or "").strip()

        if tag in {"input", "textarea"} and placeholder:
            selectors.append(f"placeholder={placeholder}")
        if aria:
            selectors.append(f"label={aria}")
        if tag == "button" and text:
            selectors.append(f"role=button[name={text}]")
        if tag == "a" and text:
            selectors.append(f"role=link[name={text}]")
        if role == "tab" and text:
            selectors.append(f"role=tab[name={text}]")
        # 菜单/树节点：li 上有可见文本且非超长，视为可点击项
        if tag == "li" and text and len(text) <= 20 and "\n" not in text:
            selectors.append(f"text={text}")
    return list(dict.fromkeys(selectors))


# ── 交互探测：点开 tab / 下拉，记录真实取值 ──────────────────────────────────
_TAB_LIKE_SELECTOR = "[role='tab'], .el-tabs__item, .ant-tabs-tab"
_SELECT_LIKE_SELECTOR = ".el-select, .ant-select, [role='combobox']"
_DIALOG_LIKE_SELECTOR = ".el-dialog, .ant-modal, [role='dialog']"


async def _read_select_options(page: Any, max_values: int = MAX_OPTION_VALUES) -> list[str]:
    """读取当前展开的下拉选项文本（只读，不选择）。

    这是「读到真实枚举值」的关键：旧实现只记录控件存在，
    因此 UI 上写的是「指令审批」还是「审批」这类信息完全丢失。
    """
    options: list[str] = []
    for selector in (
        ".el-select-dropdown__item",
        ".ant-select-item-option-content",
        "[role='option']",
        ".el-select-dropdown li",
    ):
        try:
            # 只读可见项：Element UI 的 dropdown 面板挂在 body 上且关闭后仍留在
            # DOM 里，不过滤可见性会把之前打开过的所有下拉选项累积读进来，
            # 造成每个下拉都"读到"同一坨合并数据。
            texts = await page.locator(selector).locator(
                "visible=true"
            ).all_inner_texts()
        except PlaywrightError:
            continue
        for text in texts:
            cleaned = " ".join(str(text or "").split())
            if cleaned and cleaned not in options:
                options.append(cleaned)
            if len(options) >= max_values:
                return options
        if options:
            break
    return options


async def _probe_interactions(
    target: Any, options: ProbeOptions
) -> list[dict[str, Any]]:
    """点开页面内的 tab 与下拉，收集「控件 -> 真实取值」。

    ``target`` 可以是 Page 或 Frame（微前端里业务控件在子 iframe）。
    Frame 没有 keyboard / wait_for_timeout，统一从宿主 Page 取。

    只读：只触发展开类交互，不点击提交/保存/删除类按钮。
    返回 [{"kind": "tab"|"select", "label": str, "options": [str]}]。
    """
    # Frame.page 是属性、返回宿主 Page；Page 没有 .page 属性 —— 统一归一化
    host_page = getattr(target, "page", None) or target

    async def settle(ms: int) -> None:
        await host_page.wait_for_timeout(ms)

    findings: list[dict[str, Any]] = []
    budget = options.max_interactions

    # 1) 页内 tab：点开后读取该 tab 面板的文本摘要
    try:
        tab_count = await target.locator(_TAB_LIKE_SELECTOR).count()
    except PlaywrightError:
        tab_count = 0
    for index in range(min(tab_count, budget)):
        try:
            tab = target.locator(_TAB_LIKE_SELECTOR).nth(index)
            label = " ".join((await tab.inner_text()).split())[:40]
            if not label:
                continue
            await tab.click(timeout=3_000)
            await settle(options.settle_ms)
            findings.append({"kind": "tab", "label": label, "options": []})
        except PlaywrightError:
            continue

    if not options.deep_read_options:
        return findings

    # 2) 下拉：点开后读取真实选项值，读毕按 Escape 收起（不改变页面状态）
    # 注意：多 tab 页面上，非当前 tab 的下拉都不可见——预算只对「可见控件」
    # 消耗，否则前排不可见控件会白白耗尽循环名额，导致一个选项都读不到。
    try:
        select_count = await target.locator(_SELECT_LIKE_SELECTOR).count()
    except PlaywrightError:
        select_count = 0
    remaining = max(0, budget - len(findings))
    for index in range(select_count):
        if remaining <= 0:
            break
        try:
            control = target.locator(_SELECT_LIKE_SELECTOR).nth(index)
            if not await control.is_visible():
                continue
            remaining -= 1
            # 取该下拉的 form-item 标签作为语义名（Element UI 结构）
            label = ""
            try:
                label = " ".join(
                    (
                        await control.locator(
                            "xpath=ancestor::div[contains(@class,'el-form-item')][1]"
                            "//label"
                        ).first.inner_text()
                    ).split()
                )[:40]
            except PlaywrightError:
                pass
            await control.click(timeout=3_000)
            await settle(options.settle_ms)
            values = await _read_select_options(target)
            await host_page.keyboard.press("Escape")
            await settle(120)
            if values:
                findings.append({"kind": "select", "label": label, "options": values})
        except PlaywrightError:
            continue

    return findings


# ── 菜单树逐层展开：到达深层页面 ─────────────────────────────────────────────
# 左侧导航候选择器（覆盖本项目已知的两套前端：Element UI 菜单 / 通用 nav）
_MENU_CONTAINER_SELECTORS = (
    # 自定义菜单（本项目实际使用）：ul.menu-wrapper 及其子菜单 ul.submenu
    "ul.menu-wrapper",
    "ul.submenu",
    # 组件库回退
    ".el-menu",
    ".el-menu--vertical",
    "nav",
    "[class*='sidebar']",
)
# 展开型菜单项（点击后展开子菜单而非跳转）
_SUBMENU_TOGGLE_SELECTOR = ".el-submenu__title, .el-sub-menu__title, [aria-expanded]"


async def _collect_menu_items(page: Any) -> list[dict[str, Any]]:
    """收集当前可见的菜单项（文本 + 是否为展开型）。

    定位策略按「语义 class → 结构 → 组件库」三级回退：

    1. 语义 class（本项目实际使用）：父项 ``p.main-menu-name``、
       子项 ``p.normal-menu``。这是最可靠的一级——直接给出菜单项文本，
       不受子菜单嵌套影响。
    2. 结构：``ul.menu-wrapper > li``，用「只取自身文本节点、排除后代 ul」
       的方式提取（子菜单嵌套在 li 内，innerText 会把整棵子树吞进来）。
    3. 组件库：Element UI / Ant Design 菜单类名。

    ``toggle=True`` 表示点击它是展开子菜单而非跳转。
    """
    # 一级：明确的语义 class（父项 main-menu-name + 子项 normal-menu 一并收集）
    try:
        nodes = page.locator("p.main-menu-name, p.normal-menu")
        total = await nodes.count()
    except PlaywrightError:
        total = 0
    items: list[dict[str, Any]] = []
    for index in range(min(total, 200)):
        try:
            node = nodes.nth(index)
            # 刻意不过滤可见性：本项目的子菜单默认隐藏、hover 后才显示
            # （实测 p.normal-menu 共 63 个但未 hover 时全部不可见）。
            # 按 is_visible() 过滤会丢掉所有子项，只探到 5 个父菜单。
            text = " ".join((await node.inner_text()).split())
            if not text or len(text) > 30:
                continue
            is_toggle = False
            try:
                is_toggle = bool(
                    await node.evaluate(
                        """n => {
                            const li = n.closest('li');
                            return !!(li && li.querySelector('ul.submenu'));
                        }"""
                    )
                )
            except PlaywrightError:
                pass
            items.append({"text": text, "toggle": is_toggle, "index": index})
        except PlaywrightError:
            continue
    if items:
        unique: dict[str, dict[str, Any]] = {}
        for item in items:
            unique.setdefault(item["text"], item)
        return list(unique.values())

    # 二级：结构回退（组件库或未知结构）
    return await _collect_menu_items_by_structure(page)


async def _collect_menu_items_by_structure(page: Any) -> list[dict[str, Any]]:
    """结构回退：只取节点自身文本节点，排除后代 ul（子菜单嵌套在 li 内）。"""
    direct_text_js = """
    n => {
        let out = '';
        for (const child of n.childNodes) {
            if (child.nodeType === 3) out += child.textContent;
            else if (child.nodeType === 1 && (child.tagName || '').toUpperCase() !== 'UL') {
                out += child.innerText || child.textContent || '';
            }
        }
        return out.trim();
    }
    """
    for container_selector in _MENU_CONTAINER_SELECTORS:
        try:
            container = page.locator(container_selector).first
            if await container.count() == 0:
                continue
            nodes = container.locator("li")
            total = await nodes.count()
        except PlaywrightError:
            continue
        items: list[dict[str, Any]] = []
        for index in range(min(total, 200)):
            try:
                node = nodes.nth(index)
                if not await node.is_visible():
                    continue
                text = " ".join(str(await node.evaluate(direct_text_js) or "").split())
                if not text or len(text) > 30:
                    continue
                is_toggle = False
                try:
                    is_toggle = bool(
                        await node.evaluate(
                            """n => {
                                if (n.querySelector('ul.submenu')) return true;
                                return !!n.querySelector(
                                    '.el-submenu__title,.el-sub-menu__title,[aria-expanded]'
                                );
                            }"""
                        )
                    )
                except PlaywrightError:
                    pass
                items.append({"text": text, "toggle": is_toggle, "index": index})
            except PlaywrightError:
                continue
        if items:
            unique: dict[str, dict[str, Any]] = {}
            for item in items:
                unique.setdefault(item["text"], item)
            return list(unique.values())
    return []


async def _expand_menu_tree(page: Any, options: ProbeOptions) -> list[dict[str, Any]]:
    """逐层展开菜单，返回探测到的 (菜单文本, 落地路径) 列表。

    旧实现只从首页 ``<a href>`` 收集路径，本项目两个被测系统的菜单项是
    ``li`` + JS 路由，没有 href，因此深层页面（如「审批设置 → 第 4 个 tab」）
    完全探测不到。这里改为真实点击逐层展开。
    """
    discovered: list[dict[str, Any]] = []
    visited_paths: set[str] = set()
    clicked: set[str] = set()  # 已点击过的菜单文本，避免重复点击与死循环

    for depth in range(options.max_menu_depth):
        items = await _collect_menu_items(page)
        progressed = False
        for item in items:
            if len(discovered) >= options.max_routes:
                return discovered
            text = str(item["text"])
            if text in clicked:
                continue
            clicked.add(text)

            # 优先用语义 class 精确定位（text= 会命中不可见的同名元素）；
            # 文本包含 & 等字符时 text= 引擎也不可靠，故按 class + 文本过滤。
            try:
                target = page.locator(
                    "p.main-menu-name, p.normal-menu"
                ).filter(has_text=text).locator("visible=true").first
                if await target.count() == 0:
                    target = page.locator(f"text={text}").locator("visible=true").first
                if await target.count() == 0:
                    continue
                # 先 hover 展开子菜单（本项目子菜单由 hover 触发），再点击进入
                if item.get("toggle"):
                    try:
                        await target.hover(timeout=3_000)
                        await page.wait_for_timeout(options.settle_ms)
                    except PlaywrightError:
                        pass
                await target.click(timeout=3_000)
                await page.wait_for_timeout(options.settle_ms)
            except PlaywrightError:
                continue

            # 展开型菜单点击后仍停留原页，但会使子菜单可见——
            # 因此「progressed」的判据不能只看路径是否新增，还要看是否展开出了新项。
            path = urlparse(page.url).path or "/"
            is_new_path = path not in visited_paths
            if is_new_path:
                visited_paths.add(path)
                discovered.append({"menu_text": text, "path": path})
            progressed = True

        if not progressed:
            break

    return discovered


# ── 主入口 ───────────────────────────────────────────────────────────────────
def _content_frames(page: Any) -> list[Any]:
    """返回值得探测的 frame：主 frame + 有实际内容的子 frame。

    被测系统是微前端架构：路由页只是壳，业务内容（tab / 表单 / 下拉）都在
    子 iframe（另一端口）里。只查主 frame 会漏掉全部业务控件——
    这正是「审批设置页探测不到 5 个 tab」的原因。
    """
    frames = [page.main_frame]
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        url = str(frame.url or "")
        if not url or url == "about:blank":
            continue
        frames.append(frame)
    return frames


async def _probe_page(
    page: Any, options: ProbeOptions, path: str
) -> dict[str, Any] | None:
    """对当前页面产出一条 route（含交互探测结果）；无可操作元素时返回 None。

    元素与交互都按「主 frame + 内容子 frame」合并收集（微前端穿透）。
    """
    selectors: list[str] = []
    for frame in _content_frames(page):
        try:
            elements = await frame.locator(_ELEMENT_SELECTOR).evaluate_all(
                _ELEMENT_QUERY, options.max_elements
            )
        except PlaywrightError:
            continue
        selectors.extend(_observed_selectors(elements))
    selectors = list(dict.fromkeys(selectors))
    if not selectors:
        return None

    route: dict[str, Any] = {
        "path": path,
        "title": "",
        "selectors": selectors,
        "visible_text": "",
    }
    try:
        route["title"] = await page.title()
    except PlaywrightError:
        pass
    # 可见文本优先取内容 frame（壳 frame 的 body 常是菜单）
    for frame in reversed(_content_frames(page)):
        try:
            text = (await frame.locator("body").inner_text())[:800]
        except PlaywrightError:
            continue
        if text.strip():
            route["visible_text"] = text
            break

    if options.explore_interactions:
        interactions: list[dict[str, Any]] = []
        for frame in _content_frames(page):
            try:
                interactions.extend(await _probe_interactions(frame, options))
            except PlaywrightError:
                continue
            if len(interactions) >= options.max_interactions:
                break
        if interactions:
            # 追加字段：既有消费方只用 path/selectors，新增键不影响白名单构造
            route["interactions"] = interactions[: options.max_interactions]
            # 把下拉的真实选项值也纳入可见文本，供模型对照需求枚举
            option_dump = "；".join(
                f"{i.get('label') or i['kind']}: " + "、".join(i.get("options", []))
                for i in interactions
                if i.get("options")
            )
            if option_dump:
                route["option_values"] = option_dump[:1200]
    return route


async def inspect_e2e_surface(
    base_url: str,
    ignore_https_errors: bool,
    auth: dict[str, Any] | None = None,
    options: ProbeOptions | None = None,
) -> SurfaceResult:
    """探测前端可操作面，产出白名单与告警。

    流程：
      1. 匿名探测 ``/`` 与 ``/login``（保持旧行为，未配 auth 时也能用）；
      2. 有 auth 时真实登录，逐层展开左侧菜单，逐页收集 selector 与交互取值。

    任何 Playwright 异常都不外抛——E2E 是可选产物，调用方统一降级为告警。
    """
    from playwright.async_api import async_playwright

    opts = options or ProbeOptions()
    base = base_url.rstrip("/") + "/"
    result = SurfaceResult()

    try:
        async with async_playwright() as playwright:
            launch_kwargs: dict[str, Any] = {}
            if opts.browser_channel:
                launch_kwargs["channel"] = opts.browser_channel
            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context(
                    ignore_https_errors=ignore_https_errors
                )
                page = await context.new_page()
                try:
                    # 1) 匿名页
                    for path in ("/", "/login"):
                        try:
                            await page.goto(
                                urljoin(base, path.lstrip("/")),
                                wait_until="domcontentloaded",
                                timeout=opts.page_timeout_ms,
                            )
                            await page.wait_for_timeout(opts.settle_ms)
                        except PlaywrightError:
                            continue
                        route = await _probe_page(page, opts, path)
                        if route:
                            result.routes.append(route)

                    # 2) 登录后深层探测
                    if auth and opts.explore_menu:
                        authenticated = await _probe_authenticated(
                            page, base, auth, opts, result
                        )
                        if not authenticated:
                            result.warnings.append(
                                "X-E2E-03: 登录后未探测到额外路由，深层页面 E2E 覆盖可能不足"
                            )
                finally:
                    await context.close()
            finally:
                await browser.close()
    except PlaywrightError as exc:
        logger.warning("E2E surface 探测失败：%s", exc)
        result.warnings.append(f"X-E2E-02: E2E 页面探测失败：{str(exc)[:200]}")

    # 去重（同 path 保留首个）
    unique: dict[str, dict[str, Any]] = {}
    for route in result.routes:
        unique.setdefault(str(route["path"]), route)
    result.routes = list(unique.values())[: opts.max_routes]
    return result


async def _probe_authenticated(
    page: Any,
    base: str,
    auth: dict[str, Any],
    options: ProbeOptions,
    result: SurfaceResult,
) -> bool:
    """登录并逐层探测；返回是否发现新路由。

    登录控件用「候选词 + 回退」定位，不写死单一系统的文案：原实现固定用
    ``get_by_placeholder("用户名")``，但被测系统的实际 placeholder 是
    「请输入登录账号」——``exact=False`` 只放宽大小写、不做子串匹配，因此
    登录必然超时，深层页面从未被探测到（旧版 E2E 只覆盖 ``/`` 与 ``/login``
    的根因之一）。这里改为按候选关键词逐个尝试。
    """
    try:
        await page.goto(
            urljoin(base, "login"),
            wait_until="domcontentloaded",
            timeout=options.page_timeout_ms,
        )
        await page.wait_for_timeout(options.settle_ms)
        if not await _fill_login(page, auth):
            result.warnings.append(
                "X-E2E-02: 未定位到登录输入框，深层页面探测跳过（检查 auth 或页面文案）"
            )
            return False
        if not await _submit_login(page):
            # 登录未生效时继续探测只会得到未登录页面，白名单会误导模型，
            # 因此明确中止并告警，而不是拿匿名页面继续。
            result.warnings.append(
                "X-E2E-02: 已填入凭据但未离开登录页，深层页面探测中止（检查凭据或按钮文案）"
            )
            return False
        await page.wait_for_timeout(800)
    except PlaywrightError as exc:
        logger.warning("E2E 登录失败：%s", exc)
        result.warnings.append(f"X-E2E-02: E2E 登录失败：{str(exc)[:150]}")
        return False

    known = {str(route["path"]) for route in result.routes}

    # 展开菜单树拿路径
    try:
        entries = await _expand_menu_tree(page, options) if options.explore_menu else []
    except PlaywrightError:
        entries = []

    # 补传统 href 收集（有些系统菜单是真链接）
    try:
        hrefs = await page.locator("a").evaluate_all(
            "nodes => nodes.map(n => n.getAttribute('href'))"
        )
        for href in hrefs:
            if isinstance(href, str) and href.startswith("/") and not href.startswith("//"):
                entries.append(
                    {"menu_text": "", "path": href.split("?", 1)[0]}
                )
    except PlaywrightError:
        pass

    found_new = False
    for entry in entries:
        if len(result.routes) >= options.max_routes:
            break
        path = str(entry["path"])
        if path in known:
            continue
        try:
            await page.goto(
                urljoin(base, path.lstrip("/")),
                wait_until="domcontentloaded",
                timeout=options.page_timeout_ms,
            )
            # 微前端页面的业务内容在子 iframe 里、加载明显慢于壳页面
            # （实测 2~4s；只等 settle_ms 会漏掉 iframe 里的全部控件）
            await page.wait_for_timeout(max(options.settle_ms, 1_500))
        except PlaywrightError:
            continue
        route = await _probe_page(page, options, path)
        if route:
            if entry.get("menu_text"):
                route["menu_text"] = entry["menu_text"]
            result.routes.append(route)
            known.add(path)
            found_new = True
    return found_new


# ── 登录定位（按候选关键词回退，不写死单一系统文案）──────────────────────────
# 用户名/账号类输入框的常见 placeholder 片段；命中任一即可
_USERNAME_HINTS = ("用户名", "账号", "账户", "用户", "account", "user", "login")
# 密码类
_PASSWORD_HINTS = ("密码", "口令", "password", "passwd", "pwd")
# 登录按钮文案（按优先级）
_SUBMIT_HINTS = ("登 录", "登录", "登陆", "登入", "Login", "Sign in")


async def _fill_by_placeholder_hints(page: Any, hints: tuple[str, ...], value: str) -> bool:
    """按 placeholder 候选词逐个尝试填写；成功返回 True。

    用 ``input[placeholder*=...]`` 做子串匹配——Playwright 的
    ``get_by_placeholder(exact=False)`` 只放宽大小写与首尾空白，不匹配子串，
    这正是原实现在「请输入登录账号」上失败的原因。
    """
    for hint in hints:
        try:
            locator = page.locator(
                f"input[placeholder*='{hint}'], textarea[placeholder*='{hint}']"
            ).first
            if await locator.count() == 0:
                continue
            if not await locator.is_visible():
                continue
            await locator.fill(value, timeout=5_000)
            return True
        except PlaywrightError:
            continue
    return False


async def _fill_login(page: Any, auth: dict[str, Any]) -> bool:
    """填写登录表单；用户名与密码都成功才返回 True。

    回退顺序：placeholder 候选词 → 按 input[type=password] 兜底定位密码框，
    再用「剩余的第一个可见 text 输入框」作为用户名框。
    """
    username = str(auth.get("username", ""))
    password = str(auth.get("password", ""))

    ok_user = await _fill_by_placeholder_hints(page, _USERNAME_HINTS, username)
    ok_pass = await _fill_by_placeholder_hints(page, _PASSWORD_HINTS, password)

    if not ok_pass:
        try:
            pwd = page.locator("input[type='password']").first
            if await pwd.count() > 0 and await pwd.is_visible():
                await pwd.fill(password, timeout=5_000)
                ok_pass = True
        except PlaywrightError:
            pass

    if not ok_user:
        try:
            # 用户名 = 第一个可见的非密码输入框
            candidate = page.locator(
                "input[type='text'], input:not([type]), input[type='tel']"
            ).first
            if await candidate.count() > 0 and await candidate.is_visible():
                await candidate.fill(username, timeout=5_000)
                ok_user = True
        except PlaywrightError:
            pass

    return ok_user and ok_pass


async def _submit_login(page: Any) -> bool:
    """点击登录按钮并确认已离开登录页；按文案候选与 button[type=submit] 回退。

    只点击不校验会出现「以为登录成功、实际仍在 /login」的静默失败——
    随后所有深层探测都建立在未登录页面上，最终表现为「探测不到任何深层路由」。

    点击后**轮询等待跳转**（最长 8s）：实测被测系统登录接口较慢，固定短等待
    （如 1.2s）会在跳转发生前误判失败。
    """
    login_url = page.url
    for hint in _SUBMIT_HINTS:
        try:
            locator = page.get_by_role("button", name=hint, exact=False).first
            if await locator.count() == 0 or not await locator.is_visible():
                continue
            await locator.click(timeout=5_000)
            if await _wait_leave_login(page, login_url):
                return True
        except PlaywrightError:
            continue
    # 回退：提交型按钮
    try:
        locator = page.locator("button[type='submit'], input[type='submit']").first
        if await locator.count() > 0:
            await locator.click(timeout=5_000)
            if await _wait_leave_login(page, login_url):
                return True
    except PlaywrightError:
        pass
    return not _is_login_page(page, login_url)


async def _wait_leave_login(
    page: Any, login_url: str, timeout_ms: int = 8_000, poll_ms: int = 400
) -> bool:
    """轮询等待页面离开登录页；超时返回 False。"""
    waited = 0
    while waited < timeout_ms:
        await page.wait_for_timeout(poll_ms)
        waited += poll_ms
        if not _is_login_page(page, login_url):
            return True
    return False


def _is_login_page(page: Any, login_url: str) -> bool:
    """判断是否仍停留在登录页（按路径与登录表单特征）。"""
    try:
        path = urlparse(page.url).path.rstrip("/").lower()
    except Exception:
        path = ""
    if path.endswith("/login") or path == "":
        # 路径相同不一定是失败（SPA 可能只在同页换视图），再看登录表单是否还在
        return True
    return False
