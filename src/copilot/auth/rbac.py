"""Authorization rules, in plain deterministic Python.

None of these decisions involve the LLM. They run before any LangGraph call that reads or resumes
a thread. Cross-tenant access returns "not found" rather than "forbidden", so thread ids from other
tenants cannot be probed.
"""

from __future__ import annotations

from dataclasses import dataclass

from copilot.config import Settings
from copilot.context import Actor
from copilot.errors import Forbidden, NotFound


@dataclass(frozen=True)
class ThreadInfo:
    thread_id: str
    tenant_id: str
    owner_id: str


def ensure_can_start(actor: Actor, settings: Settings) -> None:
    if not (actor.has_role(settings.responder_role) or actor.has_role(settings.approver_role)):
        raise Forbidden(f"starting an incident requires the '{settings.responder_role}' role")


def ensure_can_view(actor: Actor, thread: ThreadInfo | None) -> ThreadInfo:
    if thread is None or thread.tenant_id != actor.tenant_id:
        raise NotFound("incident not found")
    return thread


def ensure_can_operate(actor: Actor, thread: ThreadInfo | None, settings: Settings) -> ThreadInfo:
    """Resume or replay an incident run (not approve)."""
    thread = ensure_can_view(actor, thread)
    ensure_can_start(actor, settings)
    return thread


def ensure_can_decide(actor: Actor, thread: ThreadInfo | None, settings: Settings) -> ThreadInfo:
    """Approve or reject a pending action."""
    thread = ensure_can_view(actor, thread)
    if not actor.has_role(settings.approver_role):
        raise Forbidden(f"approving actions requires the '{settings.approver_role}' role")
    if settings.require_distinct_approver and actor.user_id == thread.owner_id:
        raise Forbidden("four-eyes rule: you cannot approve an action on an incident you started")
    return thread
