"""Supervisor, memory recall and finalize.

The supervisor routes with `Command(goto=...)`. Its rules are plain code over the structured
results the agents produce: the agents decide *what is true* (actionable? root cause? confidence?),
the supervisor decides *what runs next*. That keeps routing predictable, testable and cheap.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from copilot.config import Models, Settings
from copilot.context import CopilotContext
from copilot.graph.common import emit, message_text
from copilot.graph.prompts import REPORT_PROMPT
from copilot.memory.store import recall_similar, remember_incident

Route = Literal["triage", "diagnosis", "remediation", "approval_gate", "finalize"]


def next_step(state: dict, settings: Settings) -> tuple[Route, str]:
    triage = state.get("triage")
    if triage is None:
        return "triage", "start with triage"
    if not triage.get("actionable", True):
        return "finalize", "triage found no actionable problem"

    diagnosis = state.get("diagnosis")
    attempts = state.get("diagnosis_attempts", 0)
    if diagnosis is None:
        return "diagnosis", "triage found an actionable problem"
    if diagnosis.get("category") == "no_issue":
        return "finalize", "diagnosis found no issue"
    weak = diagnosis.get("confidence") == "low" or diagnosis.get("category") == "unknown"
    if weak and attempts < settings.max_diagnosis_attempts:
        return "diagnosis", f"diagnosis attempt {attempts} was inconclusive; trying new hypotheses"
    if weak:
        return "finalize", "diagnosis stayed inconclusive; escalating to a human"

    if not state.get("remediation_done"):
        return "remediation", f"root cause identified ({diagnosis.get('category')})"
    if (state.get("proposal") or {}).get("action"):
        return "approval_gate", "an action was proposed"
    return "finalize", "no safe automated action; escalating to a human"


def make_supervisor(settings: Settings):
    async def supervisor(state: dict, runtime: Runtime[CopilotContext]) -> Command[Route]:
        goto, reason = next_step(state, settings)
        emit(runtime, {"type": "route", "to": goto, "reason": reason})
        return Command(goto=goto)

    return supervisor


async def recall(state: dict, runtime: Runtime[CopilotContext]) -> dict:
    similar = await recall_similar(runtime.store, state.get("tenant_id"), state["alert"]["service"])
    if similar:
        emit(runtime, {"type": "memory", "kind": "similar_incidents", "count": len(similar)})
    return {"similar_incidents": similar}


def final_status(state: dict) -> str:
    triage = state.get("triage") or {}
    diagnosis = state.get("diagnosis") or {}
    approval = state.get("approval") or {}
    execution = state.get("execution") or {}
    verification = state.get("verification") or {}
    if triage and not triage.get("actionable", True):
        return "no_action_needed"
    if diagnosis.get("category") == "no_issue":
        return "no_action_needed"
    if approval.get("decision") == "reject":
        return "action_rejected"
    if approval.get("decision") == "invalid":
        return "approval_invalid"
    if execution.get("status") in ("blocked", "failed"):
        return "action_failed"
    if verification:
        return "resolved" if verification.get("resolved") else "not_resolved"
    return "escalated"


REPORT_KEYS = (
    "alert",
    "triage",
    "hypotheses",
    "verdicts",
    "diagnosis",
    "proposal",
    "approval",
    "execution",
    "verification",
)


def make_finalize(models: Models):
    async def finalize(state: dict, config: RunnableConfig, runtime: Runtime[CopilotContext]) -> dict:
        status = final_status(state)
        facts = {k: state.get(k) for k in REPORT_KEYS if state.get(k) is not None}
        facts["status"] = status
        response = await models.structured.ainvoke(
            [
                SystemMessage(REPORT_PROMPT),
                HumanMessage("```json\n" + json.dumps(facts, indent=2, default=str) + "\n```"),
            ]
        )
        report = message_text(response)

        triage = state.get("triage") or {}
        diagnosis = state.get("diagnosis") or {}
        action = (state.get("proposal") or {}).get("action")
        thread_id = (config.get("configurable") or {}).get("thread_id")
        if thread_id and (triage or diagnosis):
            await remember_incident(
                runtime.store,
                state.get("tenant_id"),
                thread_id,
                {
                    "service": state["alert"]["service"],
                    "alert": state["alert"]["name"],
                    "root_cause": diagnosis.get("root_cause") or triage.get("reasoning"),
                    "category": diagnosis.get("category")
                    or ("no_issue" if not triage.get("actionable", True) else None),
                    "action": action,
                    "status": status,
                },
            )
        emit(runtime, {"type": "status", "status": status})
        return {"status": status, "report": report}

    return finalize


def summarize_for_api(values: dict[str, Any]) -> dict[str, Any]:
    """The subset of state the API and UI show."""
    keys = (
        "alert",
        "tenant_id",
        "requested_by",
        "similar_incidents",
        "triage",
        "hypotheses",
        "verdicts",
        "diagnosis",
        "diagnosis_attempts",
        "proposal",
        "approval",
        "execution",
        "verification",
        "status",
        "report",
    )
    return {k: values.get(k) for k in keys if k in values}
