"""Structured outputs produced by the agents and stored in graph state.

These models are the contract between subgraphs: the supervisor routes on them, the approval gate
hashes the proposed action, the eval suite grades them, and the UI renders them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from copilot.tools.write_tools import ActionSpec

RootCauseCategory = Literal[
    "oom",  # memory limit exceeded / OOMKilled
    "cpu_saturation",  # CPU throttling or capacity shortfall
    "dependency_down",  # a database or downstream service is unavailable
    "bad_config",  # wrong environment variable / config value
    "bad_image",  # image tag missing, pull failure, broken build
    "no_issue",  # false alarm: behaviour is normal
    "unknown",  # not enough evidence
]

Confidence = Literal["high", "medium", "low"]


class TriageReport(BaseModel):
    """Output of the triage agent."""

    summary: str = Field(description="Two or three sentences: what is happening and the user impact.")
    affected_service: str = Field(description="The service most directly affected.")
    namespace: str
    symptoms: list[str] = Field(description="Observed symptoms, each grounded in tool output.")
    severity: Literal["low", "medium", "high", "critical"]
    actionable: bool = Field(
        description="False only if the evidence shows normal behaviour (false alarm) and nothing needs doing."
    )
    reasoning: str = Field(description="Why the incident is or is not actionable.")


class Hypothesis(BaseModel):
    id: str = Field(description="Short slug, e.g. 'h1'.")
    title: str = Field(description="One-line candidate root cause.")
    category: RootCauseCategory
    rationale: str = Field(description="Why this is plausible given the triage findings.")
    checks: list[str] = Field(description="Concrete checks to confirm or refute it (tools and what to look for).")


class HypothesisList(BaseModel):
    hypotheses: list[Hypothesis] = Field(description="Distinct, competing hypotheses, most likely first.")


class HypothesisVerdict(BaseModel):
    """Output of one parallel hypothesis investigation."""

    hypothesis_id: str
    hypothesis_title: str = ""
    verdict: Literal["supported", "refuted", "inconclusive"]
    evidence: list[str] = Field(description="Facts from tool output that support the verdict.")
    notes: str = Field(default="", description="Anything else the diagnosis step should know.")


class Diagnosis(BaseModel):
    root_cause: str = Field(description="The root cause in one or two sentences.")
    category: RootCauseCategory
    confidence: Confidence
    culprit_deployment: str = Field(
        description="The deployment whose change or state causes the incident (may differ from the alerting one)."
    )
    evidence: list[str] = Field(description="Key evidence, citing tool outputs.")
    supported_hypothesis_id: str | None = None


class RemediationProposal(BaseModel):
    """Output of the remediation agent: at most one action, chosen from the write-tool catalog."""

    action: ActionSpec | None = Field(
        description="The single action that best fixes the root cause, or null if no automated action is safe."
    )
    rationale: str = Field(description="Why this action fixes the root cause.")
    expected_outcome: str = Field(description="What should be observable after the action succeeds.")
    alternatives: list[str] = Field(default_factory=list, description="Other reasonable options, briefly.")


class Verification(BaseModel):
    resolved: bool
    checks: list[str]
