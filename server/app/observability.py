"""可观测性：请求计数、延迟、状态码分布，以及 Prometheus 文本输出。

零依赖实现，够用且能直接接 Grafana Agent / Prometheus 的 textfile 或抓取端点。
生产规模上去后换 ``prometheus_client`` 只需替换 ``observe()`` 与 ``render()``。
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

_STARTED_AT = time.time()


@dataclass
class Metrics:
    requests_total: int = 0
    by_status: Counter = field(default_factory=Counter)
    by_method: Counter = field(default_factory=Counter)
    by_path: Counter = field(default_factory=Counter)
    error_total: int = 0
    latency_ms_sum: float = 0.0
    latency_ms_max: float = 0.0
    slow_total: int = 0
    started_at: float = _STARTED_AT
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def observe(self, *, method: str, path: str, status: int, elapsed_ms: float) -> None:
        with self._lock:
            self.requests_total += 1
            self.by_method[method] += 1
            self.by_status[str(status)] += 1
            self.by_path[_bucket(path)] += 1
            self.latency_ms_sum += elapsed_ms
            self.latency_ms_max = max(self.latency_ms_max, elapsed_ms)
            if status >= 500:
                self.error_total += 1
            if elapsed_ms >= 1000:
                self.slow_total += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = max(1, self.requests_total)
            return {
                "uptime_s": round(time.time() - self.started_at, 1),
                "requests_total": self.requests_total,
                "errors_total": self.error_total,
                "slow_requests": self.slow_total,
                "latency_ms_avg": round(self.latency_ms_sum / total, 2),
                "latency_ms_max": round(self.latency_ms_max, 2),
                "by_status": dict(self.by_status),
                "by_method": dict(self.by_method),
                "top_paths": self.by_path.most_common(10),
            }

    def reset(self) -> None:
        with self._lock:
            self.__init__()  # type: ignore[misc]


METRICS = Metrics()


def _bucket(path: str) -> str:
    """把具体 id 折叠成模板，避免指标基数爆炸（/api/wiki/pages/12 → /api/wiki/pages/{id}）。"""
    parts = [part for part in path.split("/") if part]
    out = []
    for part in parts:
        if part.isdigit():
            out.append("{id}")
        else:
            out.append(part)
    return "/" + "/".join(out)


def prometheus_text(extra_gauges: dict[str, float] | None = None) -> str:
    snapshot = METRICS.snapshot()
    lines = [
        "# HELP finonto_uptime_seconds 进程运行时长",
        "# TYPE finonto_uptime_seconds gauge",
        f"finonto_uptime_seconds {snapshot['uptime_s']}",
        "# HELP finonto_requests_total HTTP 请求总数",
        "# TYPE finonto_requests_total counter",
        f"finonto_requests_total {snapshot['requests_total']}",
        "# HELP finonto_errors_total 5xx 数量",
        "# TYPE finonto_errors_total counter",
        f"finonto_errors_total {snapshot['errors_total']}",
        "# HELP finonto_slow_requests 耗时超过 1s 的请求数",
        "# TYPE finonto_slow_requests counter",
        f"finonto_slow_requests {snapshot['slow_requests']}",
        "# HELP finonto_latency_ms_avg 平均延迟（毫秒）",
        "# TYPE finonto_latency_ms_avg gauge",
        f"finonto_latency_ms_avg {snapshot['latency_ms_avg']}",
        "# HELP finonto_latency_ms_max 最大延迟（毫秒）",
        "# TYPE finonto_latency_ms_max gauge",
        f"finonto_latency_ms_max {snapshot['latency_ms_max']}",
        "# HELP finonto_requests_by_status 按状态码分组的请求数",
        "# TYPE finonto_requests_by_status counter",
    ]
    for status, count in snapshot["by_status"].items():
        lines.append(f'finonto_requests_by_status{{status="{status}"}} {count}')
    lines.append("# HELP finonto_requests_by_path 按路径模板分组的请求数")
    lines.append("# TYPE finonto_requests_by_path counter")
    for path, count in METRICS.snapshot()["top_paths"]:
        lines.append(f'finonto_requests_by_path{{path="{path}"}} {count}')
    for name, value in (extra_gauges or {}).items():
        lines.append(f"# TYPE finonto_{name} gauge")
        lines.append(f"finonto_{name} {value}")
    return "\n".join(lines) + "\n"
