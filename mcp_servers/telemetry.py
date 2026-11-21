"""OpenTelemetry for the MCP servers: one span per tool call, joined to the agent's trace.

The agent's httpx calls carry a W3C `traceparent` header. Each tool span is parented on it, and the
Kubernetes / Prometheus HTTP calls made inside are child spans (httpx instrumentation), so one trace goes
API -> graph node -> MCP tool -> Kubernetes API.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagate import extract

_tracer = trace.get_tracer("mcp_servers")


def setup(service_name: str) -> None:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()


def _incoming_headers() -> dict[str, str]:
    try:
        from mcp.server.lowlevel.server import request_ctx

        request = request_ctx.get().request
        return {k.lower(): v for k, v in request.headers.items()} if request is not None else {}
    except (LookupError, AttributeError):
        return {}


@contextmanager
def tool_span(tool: str, who: Any, namespace: str) -> Iterator[None]:
    parent = extract(_incoming_headers())
    token = otel_context.attach(parent)
    try:
        with _tracer.start_as_current_span(
            f"mcp.tool {tool}",
            kind=trace.SpanKind.SERVER,
            attributes={
                "mcp.tool": tool,
                "k8s.namespace": namespace,
                "k8s.impersonate.user": who.user,
                "k8s.impersonate.groups": list(who.groups),
                "tenant.id": who.tenant,
            },
        ):
            yield
    finally:
        otel_context.detach(token)
