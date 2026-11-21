"""Tracing: span tree for a run, trace ids on audit rows, PII masking before export."""

from __future__ import annotations

from dataclasses import replace

import pytest
from langchain_core.tools import tool
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from copilot.approval.service import get_pending_approval
from copilot.observability.tracing import GraphTracer, RedactingExporter, current_run_span, redact, run_span
from copilot.runner import stream_events
from copilot.tools.mcp_backend import READ_SERVER, McpBackend
from copilot.tools.mock_backend import ScenarioBackend
from tests.helpers import ALICE, BOB, Harness, make_settings

MEMORY = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def provider():
    current = trace.get_tracer_provider()
    if not isinstance(current, TracerProvider):
        p = TracerProvider()
        p.add_span_processor(SimpleSpanProcessor(RedactingExporter(MEMORY)))
        trace.set_tracer_provider(p)
    yield


@pytest.fixture(autouse=True)
def clear():
    MEMORY.clear()


async def drain(h: Harness, inputs, actor) -> None:
    async for _ in stream_events(h.graph, inputs, h.config, h.context(actor)):
        pass


async def test_run_produces_nested_spans_and_audit_trace_ids():
    h = Harness()
    await drain(h, h.inputs(), h.context().actor)
    pending = await get_pending_approval(h.graph, h.thread_id)
    command = await h.approvals.decide(
        thread=h.thread, approver=BOB, decision="approve", action_hash=pending.payload["action_hash"]
    )
    await drain(h, command, BOB)

    spans = MEMORY.get_finished_spans()
    runs = [s for s in spans if s.name == "incident.run"]
    assert len(runs) == 2
    assert runs[0].attributes["tenant.id"] == "acme" and runs[0].attributes["enduser.id"] == "u-alice"
    assert runs[1].attributes["enduser.id"] == "u-bob" and runs[1].attributes["run.kind"] == "resume"

    names = {s.name for s in spans}
    for node in ("triage", "diagnosis", "remediation", "approval_gate", "execute_action", "verify", "finalize"):
        assert f"graph.node {node}" in names, node
    assert any(n.startswith("tool describe_pod") for n in names)

    # Every node/tool span belongs to one of the two run traces.
    run_traces = {r.context.trace_id for r in runs}
    assert {s.context.trace_id for s in spans} <= run_traces

    # Investigator nodes nest under the diagnosis node (subgraph -> Send branch).
    by_id = {s.context.span_id: s for s in spans}
    investigate = next(s for s in spans if s.name == "graph.node investigate")
    assert by_id[investigate.parent.span_id].name == "graph.node diagnosis"

    # The executed audit row points at the resume run's trace.
    executed = next(r for r in h.audit.records if r.status == "executed")
    assert executed.trace_id == format(runs[1].context.trace_id, "032x")


class SpanProbeBackend(ScenarioBackend):
    """Records which span is current when a backend call is made the way McpBackend makes it."""

    def __init__(self, scenario) -> None:
        super().__init__(scenario)
        self.seen: dict[str, set[str]] = {}

    def _probe(self, call: str) -> None:
        with current_run_span():
            self.seen.setdefault(call, set()).add(getattr(trace.get_current_span(), "name", "?"))

    async def describe_pod(self, namespace, pod):
        self._probe("describe_pod")
        return await super().describe_pod(namespace, pod)

    async def apply_action(self, action, actor):
        self._probe("apply_action")
        return await super().apply_action(action, actor)


async def test_backend_calls_run_under_their_tool_or_node_span():
    h = Harness(scenario_id="oomkilled_loop")
    h.backend = backend = SpanProbeBackend(h.scenario)
    await drain(h, h.inputs(), h.context().actor)
    pending = await get_pending_approval(h.graph, h.thread_id)
    command = await h.approvals.decide(
        thread=h.thread, approver=BOB, decision="approve", action_hash=pending.payload["action_hash"]
    )
    await drain(h, command, BOB)

    assert backend.seen["describe_pod"] == {"tool describe_pod"}
    assert backend.seen["apply_action"] == {"graph.node execute_action"}


async def test_subgraph_node_is_not_doubled():
    h = Harness()
    await drain(h, h.inputs(), h.context().actor)
    spans = MEMORY.get_finished_spans()
    by_id = {s.context.span_id: s for s in spans}
    for s in spans:
        if s.parent is not None and s.parent.span_id in by_id:
            assert by_id[s.parent.span_id].name != s.name, s.name


async def test_mcp_call_gets_one_client_span_under_the_calling_tool():
    """McpBackend: `tools/call <tool>` span under our tool span; the adapter's LangChain tool adds no span."""
    seen: list[str] = []

    @tool(response_format="content_and_artifact")
    async def describe_pod(namespace: str, pod: str) -> tuple[str, dict]:
        """Adapter stand-in."""
        seen.append(trace.get_current_span().name)  # where the HTTP requests would be made
        return "", {"structured_content": {"name": pod}}

    mcp = McpBackend(make_settings(tool_backend="mcp"), replace(ALICE, access_token="t"))
    mcp._tools = {READ_SERVER: {"describe_pod": describe_pod}}

    @tool
    async def outer_describe_pod(pod: str) -> dict:
        """Our read tool."""
        return await mcp.describe_pod("acme-prod", pod)

    with run_span("incident.run", {}):
        result = await outer_describe_pod.ainvoke({"pod": "p1"}, config={"callbacks": [GraphTracer()]})

    assert result == {"name": "p1"}
    assert seen == ["tools/call describe_pod"]
    spans = {s.name: s for s in MEMORY.get_finished_spans()}
    assert set(spans) == {"incident.run", "tool outer_describe_pod", "tools/call describe_pod"}
    assert spans["tools/call describe_pod"].parent.span_id == spans["tool outer_describe_pod"].context.span_id


def test_exporter_masks_pii():
    with run_span("demo", {"note": "mail bob@acme.example", "auth": "Bearer abcdefghijklmnop"}):
        pass
    (span,) = MEMORY.get_finished_spans()
    assert span.attributes["note"] == "mail <email>"
    assert span.attributes["auth"] == "Bearer <token>"


def test_redact_jwt_and_secrets():
    jwt_like = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTIz"
    assert redact(f"token {jwt_like}") == "token <jwt>"
    assert redact("DB_PASSWORD: s3cr3t, user=x") == "DB_PASSWORD: <redacted>, user=x"
    assert redact("client_secret=abc123") == "client_secret=<redacted>"
    assert redact("max_tokens: 100") == "max_tokens: 100"
    assert redact("password=hunter2") == "password=<redacted>"
