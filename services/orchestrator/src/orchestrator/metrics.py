"""轻量 Prometheus 指标注册器。

不引入额外运行时依赖；多实例部署时每个进程暴露自己的指标，由 Prometheus
按实例抓取并聚合。标签只允许固定方法、路由模板和状态码，避免 job_id 造成高基数。
"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock


def _label(value: str) -> str:
    """转义 Prometheus label 值。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class MetricsRegistry:
    """进程内计数器/仪表盘，输出 Prometheus text exposition 格式。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._requests: defaultdict[tuple[str, str, int], int] = defaultdict(int)
        self._counters: defaultdict[str, int] = defaultdict(int)
        self._gauges: dict[str, float] = {}

    def record_request(self, method: str, route: str, status_code: int) -> None:
        """记录请求总数；route 必须是模板而非带 job_id 的原始路径。"""
        with self._lock:
            self._requests[(method.upper(), route, status_code)] += 1

    def increment(self, name: str, value: int = 1) -> None:
        """增加无标签计数器。"""
        with self._lock:
            self._counters[name] += value

    def set_gauge(self, name: str, value: float) -> None:
        """设置无标签仪表值。"""
        with self._lock:
            self._gauges[name] = value

    def render(self) -> str:
        """生成稳定排序的 Prometheus 文本。"""
        with self._lock:
            requests = dict(self._requests)
            counters = dict(self._counters)
            gauges = dict(self._gauges)
        lines = [
            "# HELP autotest_http_requests_total HTTP requests handled by orchestrator",
            "# TYPE autotest_http_requests_total counter",
        ]
        for (method, route, status), value in sorted(requests.items()):
            lines.append(
                f'autotest_http_requests_total{{method="{_label(method)}",'
                f'route="{_label(route)}",status="{status}"}} {value}'
            )
        for name, value in sorted(counters.items()):
            lines.extend([f"# TYPE {name} counter", f"{name} {value}"])
        for gauge_name, gauge_value in sorted(gauges.items()):
            lines.extend(
                [f"# TYPE {gauge_name} gauge", f"{gauge_name} {gauge_value:g}"]
            )
        return "\n".join(lines) + "\n"


__all__ = ["MetricsRegistry"]
