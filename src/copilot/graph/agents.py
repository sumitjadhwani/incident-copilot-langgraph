"""The triage, diagnosis and remediation subgraphs."""

from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from copilot.config import Models, Settings
from copilot.context import CopilotContext
from copilot.graph.common import LLM_RETRY, build_react_agent, emit, structured
from copilot.graph.prompts import (
    DIAGNOSIS_PROMPT,
    HYPOTHESES_PROMPT,
    INVESTIGATOR_PROMPT,
    REMEDIATION_PROMPT,
    TRIAGE_PROMPT,
    render_alert,
    render_json,
    render_similar,
)
from copilot.graph.schemas import (
    Diagnosis,
    HypothesisList,
    HypothesisVerdict,
    RemediationProposal,
    TriageReport,
)
from copilot.graph.state import AgentState, DiagnosisInput, DiagnosisOutput, DiagnosisState
from copilot.memory.store import get_runbook
from copilot.tools.read_tools import READ_TOOLS

# --------------------------------------------------------------------------------------------
# Triage
# --------------------------------------------------------------------------------------------


class TriageInput(TypedDict, total=False):
    alert: dict[str, Any]
    similar_incidents: list[dict[str, Any]]


class TriageOutput(TypedDict, total=False):
    triage: dict[str, Any] | None


def build_triage(models: Models, settings: Settings):
    def task(state: dict) -> str:
        return "\n\n".join(
            [
                "A new alert fired. Triage it.",
                render_alert(state["alert"]),
                render_similar(state.get("similar_incidents")),
            ]
        )

    return build_react_agent(
        name="triage",
        models=models,
        tools=READ_TOOLS,
        system_prompt=TRIAGE_PROMPT,
        task=task,
        output_model=TriageReport,
        finish=lambda result, _state: {"triage": result.model_dump(mode="json")},
        max_tool_rounds=settings.triage_max_tool_rounds,
        state_schema=AgentState,
        input_schema=TriageInput,
        output_schema=TriageOutput,
    )


# --------------------------------------------------------------------------------------------
# Diagnosis: propose hypotheses -> investigate each in parallel (Send) -> conclude
# --------------------------------------------------------------------------------------------


class InvestigatorInput(TypedDict, total=False):
    alert: dict[str, Any]
    triage: dict[str, Any] | None
    hypothesis: dict[str, Any]


class InvestigatorOutput(TypedDict, total=False):
    verdicts: list[dict[str, Any]]


def build_investigator(models: Models, settings: Settings):
    def task(state: dict) -> str:
        h = state["hypothesis"]
        return "\n\n".join(
            [
                render_alert(state["alert"]),
                render_json("Triage findings", state.get("triage")),
                render_json("Your hypothesis", h),
                "Test it now.",
            ]
        )

    def finish(result: HypothesisVerdict, state: dict) -> dict:
        verdict = result.model_copy(
            update={
                "hypothesis_id": state["hypothesis"]["id"],
                "hypothesis_title": state["hypothesis"]["title"],
            }
        )
        return {"verdicts": [verdict.model_dump(mode="json")]}

    return build_react_agent(
        name="investigator",
        models=models,
        tools=READ_TOOLS,
        system_prompt=INVESTIGATOR_PROMPT,
        task=task,
        output_model=HypothesisVerdict,
        finish=finish,
        max_tool_rounds=settings.hypothesis_max_tool_rounds,
        state_schema=AgentState,
        input_schema=InvestigatorInput,
        output_schema=InvestigatorOutput,
        label=lambda s: (s.get("hypothesis") or {}).get("title"),
    )


def build_diagnosis(models: Models, settings: Settings):
    proposer = structured(models.structured, HypothesisList)
    concluder = structured(models.structured, Diagnosis)

    def previous_attempt(state: DiagnosisState) -> list[str]:
        if not state.get("diagnosis"):
            return []
        return [
            render_json("A previous diagnosis attempt was inconclusive", state["diagnosis"]),
            "Propose different hypotheses or sharper checks than last time.",
        ]

    async def propose(state: DiagnosisState, runtime: Runtime[CopilotContext]) -> dict:
        result: HypothesisList = await proposer.ainvoke(
            [
                SystemMessage(HYPOTHESES_PROMPT),
                HumanMessage(
                    "\n\n".join(
                        [
                            render_alert(state["alert"]),
                            render_json("Triage findings", state.get("triage")),
                            render_similar(state.get("similar_incidents")),
                            *previous_attempt(state),
                            f"Give at most {settings.max_hypotheses} hypotheses.",
                        ]
                    )
                ),
            ]
        )
        hypotheses = [h.model_dump(mode="json") for h in result.hypotheses[: settings.max_hypotheses]]
        for i, h in enumerate(hypotheses, 1):  # ids must be unique for branch bookkeeping
            h["id"] = f"h{i}"
        emit(runtime, {"type": "hypotheses", "hypotheses": hypotheses})
        return {"hypotheses": hypotheses}

    def fan_out(state: DiagnosisState) -> list[Send]:
        return [
            Send("investigate", {"alert": state["alert"], "triage": state.get("triage"), "hypothesis": h})
            for h in state["hypotheses"]
        ]

    async def conclude(state: DiagnosisState, runtime: Runtime[CopilotContext]) -> dict:
        result: Diagnosis = await concluder.ainvoke(
            [
                SystemMessage(DIAGNOSIS_PROMPT),
                HumanMessage(
                    "\n\n".join(
                        [
                            render_alert(state["alert"]),
                            render_json("Triage findings", state.get("triage")),
                            render_json("Hypotheses", state["hypotheses"]),
                            render_json("Investigator verdicts", state.get("verdicts", [])),
                        ]
                    )
                ),
            ]
        )
        diagnosis = result.model_dump(mode="json")
        emit(runtime, {"type": "agent_result", "agent": "diagnosis", "label": None, "result": diagnosis})
        return {"diagnosis": diagnosis, "diagnosis_attempts": state.get("diagnosis_attempts", 0) + 1}

    builder = StateGraph(
        DiagnosisState, context_schema=CopilotContext, input_schema=DiagnosisInput, output_schema=DiagnosisOutput
    )
    builder.add_node("propose", propose, retry_policy=LLM_RETRY)
    builder.add_node("investigate", build_investigator(models, settings))
    builder.add_node("conclude", conclude, retry_policy=LLM_RETRY)
    builder.add_edge(START, "propose")
    builder.add_conditional_edges("propose", fan_out, ["investigate"])
    builder.add_edge("investigate", "conclude")
    builder.add_edge("conclude", END)
    return builder.compile(name="diagnosis")


# --------------------------------------------------------------------------------------------
# Remediation
# --------------------------------------------------------------------------------------------


class RemediationInput(TypedDict, total=False):
    alert: dict[str, Any]
    tenant_id: str
    triage: dict[str, Any] | None
    diagnosis: dict[str, Any] | None


class RemediationOutput(TypedDict, total=False):
    runbook: str | None
    proposal: dict[str, Any] | None
    remediation_done: bool


def build_remediation(models: Models, settings: Settings):
    async def prepare(state: dict, runtime: Runtime[CopilotContext]) -> dict:
        category = (state.get("diagnosis") or {}).get("category")
        runbook = await get_runbook(runtime.store, state.get("tenant_id"), category)
        if runbook:
            emit(runtime, {"type": "memory", "kind": "runbook", "category": category})
        return {"runbook": runbook}

    def task(state: dict) -> str:
        parts = [
            render_alert(state["alert"]),
            render_json("Triage findings", state.get("triage")),
            render_json("Diagnosis", state.get("diagnosis")),
        ]
        if state.get("runbook"):
            parts.append("Team runbook for this category (from long-term memory):\n" + state["runbook"])
        parts.append("Propose the remediation.")
        return "\n\n".join(parts)

    return build_react_agent(
        name="remediation",
        models=models,
        tools=READ_TOOLS,
        system_prompt=REMEDIATION_PROMPT,
        task=task,
        output_model=RemediationProposal,
        finish=lambda result, _s: {"proposal": result.model_dump(mode="json"), "remediation_done": True},
        max_tool_rounds=2,
        state_schema=AgentState,
        input_schema=RemediationInput,
        output_schema=RemediationOutput,
        prepare=prepare,
    )
