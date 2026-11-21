"""Test doubles: a scripted chat model that plays every agent role deterministically."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from copilot.graph.schemas import (
    Diagnosis,
    HypothesisList,
    HypothesisVerdict,
    RemediationProposal,
    TriageReport,
)


def tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def role_of(messages: list[BaseMessage]) -> str:
    system = next((m.content for m in messages if isinstance(m, SystemMessage)), "")
    if "TRIAGE agent" in system:
        return "triage"
    if "list distinct, competing hypotheses" in system:
        return "hypotheses"
    if "Weigh their verdicts" in system:
        return "diagnosis"
    if "INVESTIGATOR" in system:
        return "investigator"
    if "REMEDIATION planner" in system:
        return "remediation"
    if "incident report" in system:
        return "report"
    raise AssertionError(f"unknown role for system prompt: {system[:80]!r}")


def first_turn(messages: list[BaseMessage]) -> bool:
    """True when the agent has not acted yet (only system + task message)."""
    return isinstance(messages[-1], HumanMessage) and not any(isinstance(m, AIMessage) for m in messages)


Responder = Callable[[str, list[BaseMessage], type[BaseModel] | None], Any]


class _Bound:
    def __init__(self, model: FakeModel, schema: type[BaseModel] | None) -> None:
        self.model = model
        self.schema = schema

    async def ainvoke(self, messages: list[BaseMessage], config: Any = None, **_: Any) -> Any:
        return self.model.respond(messages, self.schema)


class FakeModel:
    """Duck-typed chat model. `responder(role, messages, schema)` decides every answer."""

    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.calls: list[tuple[str, type[BaseModel] | None]] = []

    def respond(self, messages: list[BaseMessage], schema: type[BaseModel] | None) -> Any:
        role = role_of(messages)
        self.calls.append((role, schema))
        return self.responder(role, messages, schema)

    def bind_tools(self, tools: Any, **_: Any) -> _Bound:
        return _Bound(self, None)

    def with_structured_output(self, schema: type[BaseModel], **_: Any) -> _Bound:
        return _Bound(self, schema)

    async def ainvoke(self, messages: list[BaseMessage], config: Any = None, **_: Any) -> Any:
        return self.respond(messages, None)


NS = "acme-prod"
POD = "checkout-api-7d9f8b6c5-x2k4p"

DEFAULT_ACTION = {
    "tool": "patch_resources",
    "namespace": NS,
    "deployment": "checkout-api",
    "container": "app",
    "memory_limit": "512Mi",
}


def oom_responder(
    *,
    action: dict[str, Any] | None = DEFAULT_ACTION,
    actionable: bool = True,
    confidence: str = "high",
    category: str = "oom",
) -> Responder:
    """Plays a competent copilot on the oomkilled_loop scenario."""

    def respond(role: str, messages: list[BaseMessage], schema: type[BaseModel] | None) -> Any:
        if schema is None and role in ("triage", "investigator", "remediation"):
            if first_turn(messages):
                return AIMessage(
                    content="checking",
                    tool_calls=[
                        tool_call("describe_pod", {"namespace": NS, "pod": POD}, f"{role}-1"),
                    ],
                )
            return AIMessage(content="I have enough evidence.")
        if schema is TriageReport:
            return TriageReport(
                summary="checkout-api pods are crash looping.",
                affected_service="checkout-api",
                namespace=NS,
                symptoms=["OOMKilled"],
                severity="critical",
                actionable=actionable,
                reasoning="restarts",
            )
        if schema is HypothesisList:
            return HypothesisList.model_validate(
                {
                    "hypotheses": [
                        {
                            "id": "x",
                            "title": "Memory limit too low",
                            "category": "oom",
                            "rationale": "exit 137",
                            "checks": ["describe_pod"],
                        },
                        {
                            "id": "y",
                            "title": "Bad config",
                            "category": "bad_config",
                            "rationale": "new env",
                            "checks": ["describe_deployment"],
                        },
                    ]
                }
            )
        if schema is HypothesisVerdict:
            return HypothesisVerdict(hypothesis_id="?", verdict="supported", evidence=["OOMKilled exit 137"])
        if schema is Diagnosis:
            return Diagnosis(
                root_cause="cache growth exceeds the 256Mi limit",
                category=category,
                confidence=confidence,
                culprit_deployment="checkout-api",
                evidence=["OOMKilled"],
                supported_hypothesis_id="h1",
            )
        if schema is RemediationProposal:
            return RemediationProposal.model_validate(
                {
                    "action": action,
                    "rationale": "more headroom",
                    "expected_outcome": "pods stay up",
                }
            )
        if role == "report":
            return AIMessage(content="## Summary\nreport")
        raise AssertionError(f"unexpected call: {role} {schema}")

    return respond
