"""Synthetic incident scenarios.

A scenario is a frozen snapshot of a small cluster (deployments, pods, logs, events) plus metric
series and the alert that fired. The mock tool backend serves tool calls from it, and the eval suite
uses the `expected` block to grade the agent. One JSON file per scenario lives in `scenarios/`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from copilot.config import SCENARIOS_DIR


class Alert(BaseModel):
    name: str
    severity: Literal["info", "warning", "critical"]
    namespace: str
    service: str
    summary: str
    started_at: str
    labels: dict[str, str] = Field(default_factory=dict)


class Resources(BaseModel):
    requests: dict[str, str] = Field(default_factory=dict)
    limits: dict[str, str] = Field(default_factory=dict)


class ContainerSpec(BaseModel):
    name: str
    image: str
    resources: Resources = Field(default_factory=Resources)
    env: dict[str, str] = Field(default_factory=dict)


class Revision(BaseModel):
    revision: int
    image: str
    change_cause: str
    created_at: str


class Deployment(BaseModel):
    name: str
    replicas: int
    ready_replicas: int
    containers: list[ContainerSpec]
    revisions: list[Revision] = Field(default_factory=list)


class ContainerStatus(BaseModel):
    name: str
    state: str
    ready: bool
    last_state: dict[str, str | int] | None = None


class PodLogs(BaseModel):
    current: list[str] = Field(default_factory=list)
    previous: list[str] = Field(default_factory=list)


class Pod(BaseModel):
    name: str
    deployment: str
    phase: str
    status: str
    restarts: int
    node: str
    age: str
    containers: list[ContainerStatus]
    logs: dict[str, PodLogs] = Field(default_factory=dict, description="Keyed by container name.")


class Event(BaseModel):
    type: Literal["Normal", "Warning"]
    reason: str
    object: str
    message: str
    count: int = 1
    last_seen: str


class Namespace(BaseModel):
    deployments: dict[str, Deployment] = Field(default_factory=dict)
    pods: list[Pod] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)


class MetricSeries(BaseModel):
    unit: str
    description: str
    # (minutes relative to now, value); offsets are <= 0
    points: list[tuple[int, float]]


class Expected(BaseModel):
    """Grading rubric used by the eval suite."""

    root_cause_category: str
    root_cause_keywords: list[str] = Field(default_factory=list)
    # Any of these write tools counts as a correct remediation (an incident often has several valid fixes).
    # Empty means no action should be proposed (false alarm).
    acceptable_actions: list[str] = Field(default_factory=list)
    # Deployment the action must target (may differ from the alerting service).
    action_target: str | None = None
    required_tools: list[str] = Field(default_factory=list)
    max_tool_calls: int = 60


class FixRule(BaseModel):
    """An action that resolves the scenario.

    `args` values are either exact values or one constraint dict:
    {"min_mib": n}, {"max_int": n}, {"min_int": n}, {"one_of": [...]}.
    """

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ScenarioRemediation(BaseModel):
    fixes: list[FixRule] = Field(default_factory=list)
    # Deployments that become healthy once a fix is applied.
    heals: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    id: str
    title: str
    description: str
    # mock: tools answer from the snapshot below. live: tools hit the kind cluster through MCP.
    mode: Literal["mock", "live"] = "mock"
    alert: Alert
    expected: Expected
    namespaces: dict[str, Namespace] = Field(default_factory=dict)
    # metrics[service][metric_name]
    metrics: dict[str, dict[str, MetricSeries]] = Field(default_factory=dict)
    remediation: ScenarioRemediation = Field(default_factory=ScenarioRemediation)


def scenario_path(scenario_id: str) -> Path:
    return SCENARIOS_DIR / f"{scenario_id}.json"


def load_scenario(scenario_id: str) -> Scenario:
    path = scenario_path(scenario_id)
    if not path.exists():
        available = ", ".join(list_scenarios(None)) or "none"
        raise FileNotFoundError(f"Unknown scenario '{scenario_id}'. Available: {available}")
    return Scenario.model_validate(json.loads(path.read_text(encoding="utf-8")))


def list_scenarios(mode: str | None = "mock") -> list[str]:
    """Scenario ids, filtered by mode (None = all)."""
    ids = sorted(p.stem for p in SCENARIOS_DIR.glob("*.json"))
    if mode is None:
        return ids
    return [i for i in ids if json.loads(scenario_path(i).read_text(encoding="utf-8")).get("mode", "mock") == mode]
