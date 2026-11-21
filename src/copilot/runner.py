"""Run the graph and turn LangGraph's stream into a flat sequence of timeline events.

Stream modes used (with subgraphs=True, so nested agents are visible):
- custom:   events nodes emit themselves (agent steps, tool results, routing, approvals, ...)
- updates:  top-level node completions and `__interrupt__` (the approval request)
- messages: tokens of the final report, so the UI can render it as it is written
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from copilot.graph.common import message_text
from copilot.observability.tracing import GraphTracer, langsmith_masking, run_span

TOP_LEVEL_NODES = {
    "recall",
    "supervisor",
    "triage",
    "diagnosis",
    "remediation",
    "approval_gate",
    "execute_action",
    "verify",
    "finalize",
}


async def stream_events(
    graph: Any, inputs: Any, config: dict[str, Any], context: Any, trace_attributes: dict[str, Any] | None = None
) -> AsyncIterator[dict[str, Any]]:
    actor = getattr(context, "actor", None)
    attributes = {
        "thread.id": (config.get("configurable") or {}).get("thread_id"),
        "tenant.id": getattr(actor, "tenant_id", None),
        "enduser.id": getattr(actor, "user_id", None),  # Keycloak sub (pseudonymous), never the username
        "run.kind": "resume" if inputs is None or not isinstance(inputs, dict) else "start",
        **(trace_attributes or {}),
    }
    with run_span("incident.run", attributes), langsmith_masking():
        async for event in _stream(
            graph, inputs, {**config, "callbacks": [*(config.get("callbacks") or []), GraphTracer()]}, context
        ):
            yield event


async def _stream(graph: Any, inputs: Any, config: dict[str, Any], context: Any) -> AsyncIterator[dict[str, Any]]:
    async for ns, mode, data in graph.astream(
        inputs,
        config,
        context=context,
        stream_mode=["custom", "updates", "messages"],
        subgraphs=True,
        durability="sync",  # every step is on disk before the next starts (crash-safe resume)
    ):
        if mode == "custom":
            yield data
        elif mode == "updates" and not ns:
            for node, update in data.items():
                if node == "__interrupt__":
                    for intr in update:
                        yield {"type": "approval_required", "interrupt_id": intr.id, "request": intr.value}
                elif node in TOP_LEVEL_NODES:
                    yield {"type": "node_done", "node": node}
        elif mode == "messages":
            chunk, meta = data
            if meta.get("langgraph_node") == "finalize" and not ns:
                text = message_text(chunk)
                if text:
                    yield {"type": "token", "text": text}


async def run_outcome(graph: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Where the thread stands after a run: awaiting approval, completed, or still has work."""
    snapshot = await graph.aget_state(config)
    pending = [i.value for i in snapshot.interrupts if isinstance(i.value, dict)]
    if pending:
        return {"status": "awaiting_approval", "approval_request": pending[0]}
    if snapshot.next:
        return {"status": "interrupted", "next": list(snapshot.next)}
    return {"status": "completed", "final_status": snapshot.values.get("status")}
