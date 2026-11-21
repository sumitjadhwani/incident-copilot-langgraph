"""Graph state.

Structured results are stored as plain dicts (pydantic `model_dump`), not model instances. That
keeps checkpoints JSON-friendly and safe to deserialize, and the API can return them as-is.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class IncidentState(TypedDict, total=False):
    # --- set by trusted code when the run starts (never by the model) ---
    alert: dict[str, Any]
    tenant_id: str
    requested_by: str

    # --- memory ---
    similar_incidents: list[dict[str, Any]]

    # --- triage subgraph ---
    triage: dict[str, Any] | None

    # --- diagnosis subgraph ---
    hypotheses: list[dict[str, Any]]
    verdicts: list[dict[str, Any]]
    diagnosis: dict[str, Any] | None
    diagnosis_attempts: int

    # --- remediation subgraph ---
    runbook: str | None
    proposal: dict[str, Any] | None
    remediation_done: bool

    # --- approval gate / executor / verification ---
    approval: dict[str, Any] | None
    execution: dict[str, Any] | None
    verification: dict[str, Any] | None

    # --- finalize ---
    status: str
    report: str


class AgentState(TypedDict, total=False):
    """Private state of a ReAct-style agent subgraph (triage, investigator, remediation)."""

    messages: Annotated[list[AnyMessage], add_messages]
    tool_rounds: int
    alert: dict[str, Any]
    tenant_id: str
    similar_incidents: list[dict[str, Any]]
    triage: dict[str, Any] | None
    diagnosis: dict[str, Any] | None
    hypothesis: dict[str, Any]
    runbook: str | None
    proposal: dict[str, Any] | None
    remediation_done: bool
    verdicts: Annotated[list[dict[str, Any]], operator.add]


class DiagnosisState(TypedDict, total=False):
    alert: dict[str, Any]
    triage: dict[str, Any] | None
    similar_incidents: list[dict[str, Any]]
    diagnosis: dict[str, Any] | None
    diagnosis_attempts: int
    hypotheses: list[dict[str, Any]]
    # Parallel hypothesis branches each append one verdict.
    verdicts: Annotated[list[dict[str, Any]], operator.add]


class DiagnosisInput(TypedDict, total=False):
    alert: dict[str, Any]
    triage: dict[str, Any] | None
    similar_incidents: list[dict[str, Any]]
    diagnosis: dict[str, Any] | None
    diagnosis_attempts: int


class DiagnosisOutput(TypedDict, total=False):
    hypotheses: list[dict[str, Any]]
    verdicts: list[dict[str, Any]]
    diagnosis: dict[str, Any] | None
    diagnosis_attempts: int
