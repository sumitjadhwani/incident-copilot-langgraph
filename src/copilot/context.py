"""Per-run context injected into the graph (LangGraph `context_schema`).

Context is set by trusted code when a run starts and is never produced by the model. It holds what
the LLM must not control: which backend tools use, who the acting human is, and where audit rows go.

A resume after approval is a new run whose context carries the *approver's* identity, so writes
execute as the person who authorized them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from copilot.tools.write_tools import RiskPolicy

if TYPE_CHECKING:
    from copilot.approval.audit import AuditStore
    from copilot.tools.backend import ToolBackend


@dataclass(frozen=True)
class Actor:
    """A verified human identity (from the JWT) or a named system principal."""

    user_id: str
    username: str
    tenant_id: str
    roles: frozenset[str] = frozenset()
    groups: tuple[str, ...] = ()
    # Forwarded to MCP tool servers (milestone 9). Never logged or stored in state.
    access_token: str | None = field(default=None, repr=False, compare=False)

    def has_role(self, role: str) -> bool:
        return role in self.roles


@dataclass
class ToolCallRecorder:
    """Collects every tool call of a run (trajectory checks in evals, timeline in the UI)."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(self, **call: Any) -> None:
        self.calls.append(call)

    @property
    def tool_names(self) -> list[str]:
        return [c["tool"] for c in self.calls]


@dataclass(frozen=True)
class CopilotContext:
    backend: ToolBackend
    actor: Actor
    audit: AuditStore
    risk_policy: RiskPolicy = field(default_factory=RiskPolicy)
    recorder: ToolCallRecorder | None = None
