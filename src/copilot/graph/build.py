r"""Graph construction.

START -> recall -> supervisor --Command--> triage      (subgraph: ReAct + structured result)
                     ^                     diagnosis   (subgraph: propose -> Send x N -> conclude)
                     |                     remediation (subgraph: runbook + ReAct + proposal)
                     +----------------------/
          supervisor --> approval_gate --(interrupt / auto-approve)--> execute_action -> verify
                              \--> finalize <------------------------------/
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from copilot.config import Models, Settings, get_settings, make_models
from copilot.context import CopilotContext
from copilot.graph.agents import build_diagnosis, build_remediation, build_triage
from copilot.graph.common import LLM_RETRY, TOOL_RETRY
from copilot.graph.gate import after_execute, approval_gate, execute_action, make_verify
from copilot.graph.state import IncidentState
from copilot.graph.supervisor import make_finalize, make_supervisor, recall


def build_graph(
    models: Models | None = None,
    *,
    settings: Settings | None = None,
    checkpointer: Any = None,
    store: Any = None,
):
    """Compile the incident graph.

    Args:
        models: Chat models; defaults to the configured provider. Tests pass fakes.
        settings: Budgets and policy; defaults to environment settings.
        checkpointer: LangGraph checkpointer. Required for the approval interrupt.
        store: LangGraph Store for long-term memory (optional).
    """
    settings = settings or get_settings()
    models = models or make_models(settings)

    builder = StateGraph(IncidentState, context_schema=CopilotContext)
    builder.add_node("recall", recall)
    builder.add_node(
        "supervisor",
        make_supervisor(settings),
        destinations=("triage", "diagnosis", "remediation", "approval_gate", "finalize"),
    )
    builder.add_node("triage", build_triage(models, settings))
    builder.add_node("diagnosis", build_diagnosis(models, settings))
    builder.add_node("remediation", build_remediation(models, settings))
    builder.add_node("approval_gate", approval_gate, destinations=("execute_action", "finalize"))
    builder.add_node("execute_action", execute_action, retry_policy=TOOL_RETRY)
    builder.add_node("verify", make_verify(settings), retry_policy=TOOL_RETRY)
    builder.add_node("finalize", make_finalize(models), retry_policy=LLM_RETRY)

    builder.add_edge(START, "recall")
    builder.add_edge("recall", "supervisor")
    for phase in ("triage", "diagnosis", "remediation"):
        builder.add_edge(phase, "supervisor")
    builder.add_conditional_edges("execute_action", after_execute, {"verify": "verify", "finalize": "finalize"})
    builder.add_edge("verify", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer, store=store, name="incident_copilot")
