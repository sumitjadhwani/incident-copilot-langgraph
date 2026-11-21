"""MCP server: metrics from Prometheus through a fixed query catalog.

The model never writes PromQL: it picks a metric name and the server builds the query. Prometheus has no
per-tenant authorization, so this server enforces it: a caller may only query namespaces of their own tenant
(`<tenant>-*`). In production this would sit behind a label-enforcing proxy or a multi-tenant store.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any

import httpx

from mcp_servers.common import build_server, check_name, current_identity, fail
from mcp_servers.telemetry import tool_span

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus.monitoring.svc:9090")
POD = "{svc}-[a-z0-9]+-[a-z0-9]+"  # pods of a Deployment: <name>-<rs hash>-<pod hash>

CATALOG: dict[str, tuple[str, str, str]] = {
    "memory_usage": (
        'max(container_memory_working_set_bytes{{namespace="{ns}",pod=~"'
        + POD
        + '",container!="",container!="POD"}}) / 1048576',
        "MiB",
        "Max container working-set memory across pods",
    ),
    "cpu_usage": (
        'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}",pod=~"'
        + POD
        + '",container!="",container!="POD"}}[2m]))',
        "cores",
        "Total CPU usage across pods",
    ),
    "restart_count": (
        'sum(kube_pod_container_status_restarts_total{{namespace="{ns}",pod=~"' + POD + '"}})',
        "restarts",
        "Container restarts across current pods",
    ),
    "http_5xx_rate": (
        '100 * sum(rate(http_requests_total{{namespace="{ns}",app="{svc}",status=~"5.."}}[2m]))'
        ' / clamp_min(sum(rate(http_requests_total{{namespace="{ns}",app="{svc}"}}[2m])), 1e-9)',
        "percent",
        "Share of requests returning 5xx",
    ),
    "http_p99_latency": (
        "1000 * histogram_quantile(0.99, sum by (le) ("
        'rate(http_request_duration_seconds_bucket{{namespace="{ns}",app="{svc}"}}[2m])))',
        "ms",
        "p99 request latency",
    ),
}

mcp = build_server(
    "observability",
    "Metrics for services in the caller's tenant namespaces, from a fixed catalog of Prometheus queries.",
)
_http = httpx.AsyncClient(base_url=PROMETHEUS_URL, timeout=15)


def build_query(namespace: str, service: str, metric: str) -> tuple[str, str, str]:
    if metric not in CATALOG:
        fail("not_found", f"unknown metric '{metric}'; supported: {sorted(CATALOG)}")
    template, unit, description = CATALOG[metric]
    return template.format(ns=namespace, svc=service), unit, description


@mcp.tool()
async def query_metrics(namespace: str, service: str, metric: str, window_minutes: int = 60) -> dict[str, Any]:
    """Time series for a service: memory_usage, cpu_usage, restart_count, http_5xx_rate or http_p99_latency."""
    check_name("namespace", namespace)
    check_name("service", service)
    who = current_identity(write=False)
    if not namespace.startswith(f"{who.tenant}-"):
        fail("forbidden", f"tenant '{who.tenant}' may not read metrics of namespace '{namespace}'")
    window_minutes = max(5, min(int(window_minutes), 24 * 60))
    query, unit, description = build_query(namespace, service, metric)
    with tool_span("query_metrics", who, namespace):
        end = time.time()
        step = max(15, window_minutes * 60 // 40)
        try:
            r = await _http.get(
                "/api/v1/query_range",
                params={"query": query, "start": end - window_minutes * 60, "end": end, "step": step},
            )
        except httpx.TransportError as exc:
            fail("unavailable", f"prometheus unreachable: {exc}")
        if r.status_code >= 500:
            fail("unavailable", f"prometheus {r.status_code}")
        if r.status_code != 200:
            fail("invalid", r.text[:300])
        series = r.json()["data"]["result"]
        values = series[0]["values"] if series else []
        points = []
        for ts, raw in values:
            v = float(raw)
            if math.isfinite(v):
                points.append({"minutes_ago": round((end - float(ts)) / 60, 1), "value": round(v, 3)})
        nums = [p["value"] for p in points]
        return {
            "service": service,
            "metric": metric,
            "unit": unit,
            "description": description,
            "window_minutes": window_minutes,
            "summary": {"min": min(nums), "max": max(nums), "latest": nums[-1]} if nums else None,
            "points": points,
            **({} if nums else {"note": "no data for this service and metric in the window"}),
        }


def main() -> None:
    from mcp_servers.telemetry import setup

    setup("mcp-observability")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
