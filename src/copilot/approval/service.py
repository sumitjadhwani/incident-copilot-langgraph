"""Approval decisions: the only code path that resumes a paused incident.

Order of operations (all deterministic, before LangGraph runs anything):
1. RBAC: same tenant, approver role, four-eyes rule.
2. There must be a pending approval interrupt, and its action hash must equal the one the approver saw.
3. Record the decision in the audit log. A unique index makes this the idempotency point: a second
   decision for the same action fails here, so the graph is never resumed twice.
4. Return the `Command(resume=...)` for the caller to run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langgraph.types import Command

from copilot.approval.audit import DECISION_STATUSES, AuditEntry, AuditStore, DuplicateAuditEntry
from copilot.auth.rbac import ThreadInfo, ensure_can_decide
from copilot.config import Settings
from copilot.context import Actor
from copilot.errors import Conflict

Decision = Literal["approve", "reject"]


@dataclass(frozen=True)
class PendingApproval:
    thread_id: str
    checkpoint_id: str | None
    interrupt_id: str
    payload: dict[str, Any]


async def get_pending_approval(graph: Any, thread_id: str) -> PendingApproval | None:
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    for intr in snapshot.interrupts:
        value = intr.value
        if isinstance(value, dict) and value.get("type") == "approval_request":
            return PendingApproval(
                thread_id=thread_id,
                checkpoint_id=(snapshot.config.get("configurable") or {}).get("checkpoint_id"),
                interrupt_id=intr.id,
                payload=value,
            )
    return None


class ApprovalService:
    def __init__(self, graph: Any, audit: AuditStore, settings: Settings) -> None:
        self.graph = graph
        self.audit = audit
        self.settings = settings

    async def decide(
        self,
        *,
        thread: ThreadInfo | None,
        approver: Actor,
        decision: Decision,
        action_hash: str,
        comment: str | None = None,
        trace_id: str | None = None,
    ) -> Command:
        thread = ensure_can_decide(approver, thread, self.settings)

        pending = await get_pending_approval(self.graph, thread.thread_id)
        if pending is None:
            if await self.audit.find(thread.thread_id, action_hash, DECISION_STATUSES):
                raise Conflict("this action has already been decided")
            raise Conflict("no approval is pending for this incident")
        if pending.payload["action_hash"] != action_hash:
            raise Conflict("action hash does not match the pending action (stale or tampered request)")

        payload = pending.payload
        try:
            await self.audit.append(
                AuditEntry(
                    tenant_id=thread.tenant_id,
                    thread_id=thread.thread_id,
                    checkpoint_id=pending.checkpoint_id,
                    approver_id=approver.user_id,
                    status="approved" if decision == "approve" else "rejected",
                    action_tool=payload["action"]["tool"],
                    action_payload=payload["action"],
                    action_hash=action_hash,
                    trace_id=trace_id,
                    detail={
                        "approver_username": approver.username,
                        "comment": comment,
                        "risk": payload.get("risk"),
                        "reasons": payload.get("reasons"),
                    },
                )
            )
        except DuplicateAuditEntry as exc:
            raise Conflict("this action has already been decided") from exc

        return Command(
            resume={
                "decision": decision,
                "action_hash": action_hash,
                "approver_id": approver.user_id,
                "comment": comment,
            }
        )
