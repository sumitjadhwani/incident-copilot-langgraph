"""OpenTelemetry tracing and PII masking (OTel export and LangSmith).

- `setup_tracing` installs a tracer provider exporting OTLP/HTTP (e.g. to Jaeger) through a redacting
  exporter, and instruments httpx so calls to the MCP servers carry `traceparent`. The MCP servers continue
  the same trace down to the Kubernetes API.
- `GraphTracer` is a LangChain callback handler that turns LangGraph nodes, LLM calls (with token usage)
  and tool calls into nested spans under the run span; `current_run_span` puts outgoing MCP calls under the
  tool or node span that made them.
- `run_span` opens the per-run root span, tagged with tenant, user id (Keycloak `sub`, not the username)
  and thread id.
- `langsmith_masking` makes LangSmith traces go through an anonymizer with the same patterns.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from functools import lru_cache
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

TRACER_NAME = "incident_copilot"
LLM_PROVIDER_HOSTS = ("api.deepseek.com", "generativelanguage.googleapis.com")
UNTRACED_TAG = "copilot:untraced"  # LangChain runs with this tag get no span (their children still do)
_tracer = trace.get_tracer(TRACER_NAME)

# --------------------------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------------------------

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "<jwt>"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer <token>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    # Any key containing password/secret/api key (DB_PASSWORD=..., client_secret: ...): keep the key, mask the value.
    (re.compile(r"(?i)\b(\w*(?:password|passwd|secret|api[_-]?key)\w*)(\s*[=:]\s*)[^\s,;]+"), r"\1\2<redacted>"),
)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        for pattern, repl in _PATTERNS:
            value = pattern.sub(repl, value)
        return value
    if isinstance(value, list | tuple):
        return type(value)(redact(v) for v in value)
    return value


class _RedactedSpan:
    """Read-only view of a span with redacted attributes and event attributes (duck-types ReadableSpan)."""

    def __init__(self, span: ReadableSpan) -> None:
        self._span = span

    def __getattr__(self, name: str) -> Any:
        return getattr(self._span, name)

    @property
    def attributes(self) -> dict[str, Any]:
        return {k: redact(v) for k, v in (self._span.attributes or {}).items()}

    @property
    def events(self) -> tuple[Any, ...]:
        from opentelemetry.sdk.trace import Event

        return tuple(
            Event(e.name, {k: redact(v) for k, v in (e.attributes or {}).items()}, e.timestamp)
            for e in self._span.events
        )


class RedactingExporter(SpanExporter):
    """Masks PII and credentials before spans leave the process."""

    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans: Any) -> SpanExportResult:
        return self.inner.export([_RedactedSpan(s) for s in spans])  # type: ignore[list-item]

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


# --------------------------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------------------------

_configured = False


def setup_tracing(endpoint: str, service_name: str) -> bool:
    """Install the global tracer provider (idempotent). Returns False when tracing is disabled."""
    global _configured
    if not endpoint:
        return False
    if _configured:
        return True
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # LLM provider requests are already covered by `llm <model>` spans (with token usage); a bare `POST` span per
    # call under the run root would only add noise. Read by the httpx instrumentor when it is installed below.
    os.environ.setdefault("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", ",".join(LLM_PROVIDER_HOSTS))
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    exporter = RedactingExporter(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    _configured = True
    return True


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


@contextmanager
def run_span(name: str, attributes: dict[str, Any]) -> Iterator[trace.Span]:
    with _tracer.start_as_current_span(name, attributes={k: v for k, v in attributes.items() if v is not None}) as span:
        yield span


# --------------------------------------------------------------------------------------------
# LangGraph / LangChain spans
# --------------------------------------------------------------------------------------------


def _preview(data: Any, limit: int = 500) -> str:
    text = data if isinstance(data, str) else json.dumps(data, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


class GraphTracer(BaseCallbackHandler):
    """Nodes, LLM calls and tool calls as nested OTel spans.

    LangChain also reports internal runnables (sequences, routers); those are not given spans, and their
    children attach to the nearest traced ancestor, so the trace shows the graph's shape.
    """

    run_inline = True  # keep callbacks ordered with the run

    def __init__(self) -> None:
        self._root = trace.get_current_span()
        self._spans: dict[UUID, trace.Span] = {}
        self._anchor: dict[UUID, trace.Span] = {}  # untraced run -> span its children hang off

    def _parent(self, parent_run_id: UUID | None) -> trace.Span:
        if parent_run_id is None:
            return self._root
        return self._spans.get(parent_run_id) or self._anchor.get(parent_run_id) or self._root

    def span_for(self, run_id: UUID) -> trace.Span | None:
        return self._spans.get(run_id) or self._anchor.get(run_id)

    def _start(self, run_id: UUID, parent_run_id: UUID | None, name: str, attrs: dict[str, Any]) -> None:
        parent = self._parent(parent_run_id)
        # A compiled subgraph used as a node reports itself again under the node's name: one span is enough.
        if getattr(parent, "name", None) == name:
            self._anchor[run_id] = parent
            return
        ctx = trace.set_span_in_context(parent)
        self._spans[run_id] = _tracer.start_span(
            name, context=ctx, attributes={k: v for k, v in attrs.items() if v is not None}
        )

    def _end(self, run_id: UUID, error: BaseException | None = None, attrs: dict[str, Any] | None = None) -> None:
        self._anchor.pop(run_id, None)
        span = self._spans.pop(run_id, None)
        if span is None:
            return
        for k, v in (attrs or {}).items():
            if v is not None:
                span.set_attribute(k, v)
        if error is not None:
            span.record_exception(error)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(error)[:200]))
        span.end()

    # chains (graph nodes) -------------------------------------------------------------------
    def on_chain_start(
        self,
        serialized: Any,
        inputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        name: str | None = None,
        **kw: Any,
    ) -> None:
        meta = metadata or {}
        node = meta.get("langgraph_node")
        if node and name == node:
            self._start(
                run_id,
                parent_run_id,
                f"graph.node {node}",
                {
                    "langgraph.node": node,
                    "langgraph.step": meta.get("langgraph_step"),
                    "langgraph.checkpoint_ns": meta.get("checkpoint_ns"),
                },
            )
        else:
            self._anchor[run_id] = self._parent(parent_run_id)

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kw: Any) -> None:
        self._end(run_id)

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kw: Any) -> None:
        # GraphInterrupt is how interrupt() pauses a run; it is not a failure.
        self._end(run_id, None if type(error).__name__ in ("GraphInterrupt", "ParentCommand") else error)

    # LLM calls ------------------------------------------------------------------------------
    def on_chat_model_start(
        self,
        serialized: Any,
        messages: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        invocation_params: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        **kw: Any,
    ) -> None:
        params = invocation_params or {}
        model = params.get("model") or params.get("model_name") or (metadata or {}).get("ls_model_name")
        self._start(
            run_id,
            parent_run_id,
            f"llm {model}",
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.system": (metadata or {}).get("ls_provider"),
                "gen_ai.request.model": model,
                "gen_ai.request.tools": len(params.get("tools") or []) or None,
            },
        )

    def on_llm_end(self, response: Any, *, run_id: UUID, **kw: Any) -> None:
        usage: dict[str, Any] = {}
        try:
            msg = response.generations[0][0].message
            usage = getattr(msg, "usage_metadata", None) or {}
        except (IndexError, AttributeError):
            pass
        self._end(
            run_id,
            attrs={
                "gen_ai.usage.input_tokens": usage.get("input_tokens"),
                "gen_ai.usage.output_tokens": usage.get("output_tokens"),
            },
        )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kw: Any) -> None:
        self._end(run_id, error)

    # tools ----------------------------------------------------------------------------------
    def on_tool_start(
        self,
        serialized: Any,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        inputs: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        **kw: Any,
    ) -> None:
        if UNTRACED_TAG in (tags or ()):
            self._anchor[run_id] = self._parent(parent_run_id)
            return
        tool = (serialized or {}).get("name") or kw.get("name") or "tool"
        self._start(
            run_id,
            parent_run_id,
            f"tool {tool}",
            {
                "tool.name": tool,
                "tool.args": redact(_preview({k: v for k, v in (inputs or {}).items() if k != "runtime"} or input_str)),
            },
        )

    def on_tool_end(self, output: Any, *, run_id: UUID, **kw: Any) -> None:
        self._end(run_id)

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kw: Any) -> None:
        self._end(run_id, error)


@contextmanager
def current_run_span() -> Iterator[None]:
    """Make the span of the LangChain run we are executing inside the current OTel span.

    `GraphTracer` creates spans from callbacks without attaching them to the context (attach/detach across
    LangChain's tasks is not reliable), so outgoing HTTP calls would otherwise hang off the run root. Code that
    calls another service (the MCP backend) wraps the call in this, so the HTTP span nests under its tool or
    node span and the trace continues into the MCP server from there.
    """
    from langchain_core.runnables.config import var_child_runnable_config

    manager = (var_child_runnable_config.get() or {}).get("callbacks")
    run_id = getattr(manager, "parent_run_id", None)
    span = None
    if run_id is not None:
        for handler in getattr(manager, "handlers", None) or []:
            if isinstance(handler, GraphTracer):
                span = handler.span_for(run_id)
                break
    if span is None:
        yield
        return
    # GraphTracer records errors and ends the span itself.
    with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
        yield


@contextmanager
def mcp_client_span(server: str, tool: str) -> Iterator[trace.Span]:
    """Client-side span for one MCP tool call (OTel GenAI/MCP naming: `tools/call <tool>`)."""
    with _tracer.start_as_current_span(
        f"tools/call {tool}",
        kind=trace.SpanKind.CLIENT,
        attributes={"mcp.method.name": "tools/call", "gen_ai.tool.name": tool, "mcp.server": server},
    ) as span:
        yield span


# --------------------------------------------------------------------------------------------
# LangSmith masking
# --------------------------------------------------------------------------------------------


@lru_cache
def _masked_langsmith_client() -> Any:
    from langsmith import Client
    from langsmith.anonymizer import create_anonymizer

    rules = [{"pattern": p.pattern, "replace": r if "\\" not in r else "<redacted>"} for p, r in _PATTERNS]
    return Client(anonymizer=create_anonymizer(rules))


def langsmith_masking() -> Any:
    """Context manager routing LangChain's LangSmith tracer through an anonymizing client."""
    from langsmith.utils import tracing_is_enabled

    if not tracing_is_enabled():
        return nullcontext()
    from langsmith import tracing_context

    return tracing_context(client=_masked_langsmith_client())
