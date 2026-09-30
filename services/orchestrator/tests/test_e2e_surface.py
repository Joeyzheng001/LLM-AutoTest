"""e2e_surface 探测模块测试：配置解析、选择器白名单生成、surface 契约。

探测本体（Playwright 交互）需真实浏览器与被测系统，属联调范畴——
真实环境验证记录见 MR 描述；这里覆盖全部纯逻辑路径。
"""

from __future__ import annotations

from executor.nodes.e2e_surface import (
    ProbeOptions,
    SurfaceResult,
    _observed_selectors,
)


class TestProbeOptions:
    def test_defaults(self) -> None:
        options = ProbeOptions.from_config(None)
        assert options.explore_menu is True
        assert options.explore_interactions is True
        assert options.deep_read_options is True
        assert options.max_routes == 25
        assert options.max_elements == 400
        assert options.browser_channel == ""

    def test_config_override(self) -> None:
        options = ProbeOptions.from_config(
            {
                "explore_menu": False,
                "deep_read_options": False,
                "max_routes": 5,
                "browser_channel": "msedge",
            }
        )
        assert options.explore_menu is False
        assert options.deep_read_options is False
        assert options.max_routes == 5
        assert options.browser_channel == "msedge"

    def test_unknown_keys_ignored(self) -> None:
        """E2eConfig 的其他字段（base_url/generate_cases 等）不应报错。"""
        options = ProbeOptions.from_config(
            {"base_url": "http://x", "generate_cases": True, "auth_mode": "browser"}
        )
        assert options.max_routes == 25

    def test_negative_values_clamped(self) -> None:
        options = ProbeOptions.from_config(
            {"max_routes": -3, "max_menu_depth": 0, "max_interactions": -1}
        )
        assert options.max_routes == 1
        assert options.max_menu_depth == 1
        assert options.max_interactions == 0


class TestObservedSelectors:
    def test_legacy_four_kinds_kept(self) -> None:
        """兼容原实现的四类定位器格式（消费方白名单依赖该格式）。"""
        selectors = _observed_selectors(
            [
                {"tag": "input", "placeholder": "用户名"},
                {"tag": "button", "text": "登 录"},
                {"tag": "a", "text": "首页"},
                {"tag": "div", "aria": "关闭"},
            ]
        )
        assert "placeholder=用户名" in selectors
        assert "role=button[name=登 录]" in selectors
        assert "role=link[name=首页]" in selectors
        assert "label=关闭" in selectors

    def test_new_tab_and_li(self) -> None:
        selectors = _observed_selectors(
            [
                {"tag": "div", "role": "tab", "text": "自动审批"},
                {"tag": "li", "text": "审批设置"},
            ]
        )
        assert "role=tab[name=自动审批]" in selectors
        assert "text=审批设置" in selectors

    def test_long_li_text_excluded(self) -> None:
        """超长 li 文本（父菜单吞并子树的场景）不进白名单。"""
        selectors = _observed_selectors(
            [{"tag": "li", "text": "这是一个超过二十个字符的合并菜单文本示例项目啊"}]
        )
        assert selectors == []

    def test_dedup_preserves_order(self) -> None:
        selectors = _observed_selectors(
            [
                {"tag": "button", "text": "查询"},
                {"tag": "button", "text": "查询"},
                {"tag": "button", "text": "重置"},
            ]
        )
        assert selectors == ["role=button[name=查询]", "role=button[name=重置]"]


class TestSurfaceContract:
    def test_to_surface_keys(self) -> None:
        """消费方（_generate_e2e_cases 白名单构造）依赖的三个键必须存在。"""
        surface = SurfaceResult().to_surface("http://host:6080/")
        assert set(surface) >= {"base_url", "path_basis", "routes"}
        assert surface["base_url"] == "http://host:6080"
        assert surface["routes"] == []

    def test_route_extra_fields_are_additive(self) -> None:
        """新增字段（menu_text/interactions/option_values）不改变白名单构造。"""
        result = SurfaceResult(
            routes=[
                {
                    "path": "/a",
                    "title": "",
                    "selectors": ["role=tab[name=自动审批]"],
                    "visible_text": "",
                    "menu_text": "审批设置",
                    "interactions": [{"kind": "tab", "label": "自动审批", "options": []}],
                    "option_values": "豁免结果: 通过、指令审批、禁止",
                }
            ]
        )
        surface = result.to_surface("http://host")
        allowed_selectors = {
            selector
            for route in surface["routes"]
            for selector in route["selectors"]
        }
        allowed_paths = {route["path"] for route in surface["routes"]}
        assert allowed_selectors == {"role=tab[name=自动审批]"}
        assert allowed_paths == {"/a"}
