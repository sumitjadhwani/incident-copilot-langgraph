"""Write actions (the only way the copilot can change the cluster) and the risk policy.

The model never calls these as free-form tools. The remediation agent fills in one `ActionSpec`
(a discriminated union, so arguments are schema-validated), and every action then goes through the
single approval gate node. Whether an action needs a human is decided here, in code.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

K8sName = Annotated[str, Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)]
Memory = Annotated[str, Field(pattern=r"^[1-9][0-9]*(Mi|Gi)$", description="e.g. '512Mi' or '1Gi'")]
Cpu = Annotated[str, Field(pattern=r"^([1-9][0-9]*m|[0-9]+(\.[0-9]+)?)$", description="e.g. '500m' or '2'")]


class _Action(BaseModel):
    namespace: K8sName
    deployment: K8sName

    @property
    def target(self) -> str:
        return f"{self.namespace}/deployment/{self.deployment}"


class RestartDeployment(_Action):
    """Rolling restart of all pods in a deployment (no spec change)."""

    tool: Literal["restart_deployment"] = "restart_deployment"


class ScaleDeployment(_Action):
    """Set the replica count."""

    tool: Literal["scale_deployment"] = "scale_deployment"
    replicas: int = Field(ge=0, le=20)


class PatchResources(_Action):
    """Change CPU/memory requests or limits of one container."""

    tool: Literal["patch_resources"] = "patch_resources"
    container: K8sName
    memory_limit: Memory | None = None
    memory_request: Memory | None = None
    cpu_limit: Cpu | None = None
    cpu_request: Cpu | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> PatchResources:
        if not any((self.memory_limit, self.memory_request, self.cpu_limit, self.cpu_request)):
            raise ValueError("patch_resources needs at least one of memory/cpu limit/request")
        return self


class SetEnv(_Action):
    """Set (add or replace) one environment variable on a container; triggers a rollout."""

    tool: Literal["set_env"] = "set_env"
    container: K8sName
    name: Annotated[str, Field(pattern=r"^[A-Z_][A-Z0-9_]*$", max_length=128)]
    value: Annotated[str, Field(max_length=1024)]


class RollbackDeployment(_Action):
    """Roll the deployment back to an earlier revision from its rollout history."""

    tool: Literal["rollback_deployment"] = "rollback_deployment"
    to_revision: int = Field(ge=1)


ActionSpec = Annotated[
    RestartDeployment | ScaleDeployment | PatchResources | SetEnv | RollbackDeployment,
    Field(discriminator="tool"),
]

WRITE_TOOL_NAMES: tuple[str, ...] = (
    "restart_deployment",
    "scale_deployment",
    "patch_resources",
    "set_env",
    "rollback_deployment",
)

Risk = Literal["low", "high"]


@dataclass(frozen=True)
class RiskAssessment:
    risk: Risk
    reasons: tuple[str, ...]

    @property
    def requires_approval(self) -> bool:
        return self.risk == "high"


@dataclass(frozen=True)
class RiskPolicy:
    """Code-driven risk classification. The LLM has no say in whether approval is needed."""

    low_risk_tools: frozenset[str] = frozenset({"restart_deployment"})
    protected_namespaces: tuple[str, ...] = ("*-prod", "kube-system")

    @classmethod
    def from_settings(cls, low_risk_tools: str, protected_namespaces: str) -> RiskPolicy:
        return cls(
            low_risk_tools=frozenset(t.strip() for t in low_risk_tools.split(",") if t.strip()),
            protected_namespaces=tuple(p.strip() for p in protected_namespaces.split(",") if p.strip()),
        )

    def assess(self, action: BaseModel) -> RiskAssessment:
        reasons: list[str] = []
        tool = action.tool  # type: ignore[attr-defined]
        namespace = action.namespace  # type: ignore[attr-defined]
        if tool not in self.low_risk_tools:
            reasons.append(f"'{tool}' is classified high-risk")
        if any(fnmatch.fnmatch(namespace, p) for p in self.protected_namespaces):
            reasons.append(f"namespace '{namespace}' is protected")
        if isinstance(action, ScaleDeployment) and action.replicas == 0:
            reasons.append("scaling to zero causes an outage")
        return RiskAssessment("high" if reasons else "low", tuple(reasons) or ("low-risk tool, unprotected namespace",))
