r"""The approval gate, the executor and post-action verification.

Every write goes through these nodes, in this order:

    approval_gate --(approved)--> execute_action --(executed)--> verify --> finalize
         \--(rejected / invalid)--> finalize       \--(blocked / failed)--> finalize

- approval_gate: validates the proposed action, classifies risk in code, hashes the action, and for
  risky actions pauses with `interrupt()`. On resume, the decision must carry the same hash.
- execute_action: checks again that an `approved` audit row exists for exactly this hash
  (defense in depth), skips if it already executed (safe re-runs), executes as the acting human,
  and records `executed`/`failed`.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import TypeAdapter, ValidationError

from copilot.approval.audit import DECISION_STATUSES, AuditEntry, DuplicateAuditEntry
from copilot.approval.hashing import action_hash
from copilot.config import Settings
from copilot.context import CopilotContext
from copilot.graph.common import emit
from copilot.observability.tracing import current_trace_id
from copilot.tools.backend import TransientBackendError
from copilot.tools.write_tools import ActionSpec

AUTO_APPROVER = "policy:auto-approve"

_action_adapter: TypeAdapter[Any] = TypeAdapter(ActionSpec)


def parse_action(payload: dict[str, Any]) -> Any:
    return _action_adapter.validate_python(payload)


def thread_id_of(config: RunnableConfig) -> str:
    thread_id = (config.get("configurable") or {}).get("thread_id")
    if not thread_id:
        raise RuntimeError("the approval gate requires a checkpointer and a thread_id")
    return thread_id


async def approval_gate(
    state: dict, config: RunnableConfig, runtime: Runtime[CopilotContext]
) -> Command[Literal["execute_action", "finalize"]]:
    ctx = runtime.context
    proposal = state.get("proposal") or {}
    try:
        action = parse_action(proposal.get("action") or {})
    except ValidationError as exc:
        return Command(goto="finalize", update={"approval": {"decision": "invalid", "error": str(exc)}})

    thread_id = thread_id_of(config)
    tenant_id = state["tenant_id"]
    payload = action.model_dump(mode="json")
    digest = action_hash(payload, thread_id=thread_id, tenant_id=tenant_id)
    assessment = ctx.risk_policy.assess(action)
    base = {"action_hash": digest, "risk": assessment.risk, "reasons": list(assessment.reasons)}

    if not assessment.requires_approval:
        if await ctx.audit.find(thread_id, digest, DECISION_STATUSES) is None:
            with contextlib.suppress(DuplicateAuditEntry):  # a re-run already recorded it
                await ctx.audit.append(
                    AuditEntry(
                        tenant_id=tenant_id,
                        thread_id=thread_id,
                        approver_id=AUTO_APPROVER,
                        status="approved",
                        action_tool=action.tool,
                        action_payload=payload,
                        action_hash=digest,
                        trace_id=current_trace_id(),
                        detail={"reasons": list(assessment.reasons)},
                    )
                )
        emit(runtime, {"type": "approval", "decision": "approve", "approver_id": AUTO_APPROVER, **base})
        return Command(
            goto="execute_action", update={"approval": {"decision": "approve", "approver_id": AUTO_APPROVER, **base}}
        )

    # Risky: pause for a human. Everything the approver needs to judge is in the payload.
    decision = interrupt(
        {
            "type": "approval_request",
            "action": payload,
            "action_hash": digest,
            "risk": assessment.risk,
            "reasons": list(assessment.reasons),
            "rationale": proposal.get("rationale"),
            "expected_outcome": proposal.get("expected_outcome"),
            "diagnosis": state.get("diagnosis"),
            "requested_by": state.get("requested_by"),
        }
    )

    # Resume value comes from the approval service, which already checked RBAC and the hash.
    # Check the hash again here: a resume for any other action must never get through.
    if not isinstance(decision, dict) or decision.get("action_hash") != digest:
        return Command(
            goto="finalize",
            update={
                "approval": {"decision": "invalid", "error": "resume payload does not match the pending action", **base}
            },
        )
    approval = {
        **base,
        "decision": decision.get("decision"),
        "approver_id": decision.get("approver_id"),
        "comment": decision.get("comment"),
    }
    if decision.get("decision") != "approve":
        return Command(goto="finalize", update={"approval": {**approval, "decision": "reject"}})
    return Command(goto="execute_action", update={"approval": approval})


async def execute_action(state: dict, config: RunnableConfig, runtime: Runtime[CopilotContext]) -> dict:
    ctx = runtime.context
    thread_id = thread_id_of(config)
    tenant_id = state["tenant_id"]
    action = parse_action(state["proposal"]["action"])
    payload = action.model_dump(mode="json")
    digest = action_hash(payload, thread_id=thread_id, tenant_id=tenant_id)

    def entry(status: str, approver_id: str, detail: dict) -> AuditEntry:
        return AuditEntry(
            tenant_id=tenant_id,
            thread_id=thread_id,
            approver_id=approver_id,
            status=status,  # type: ignore[arg-type]
            action_tool=action.tool,
            action_payload=payload,
            action_hash=digest,
            trace_id=current_trace_id(),
            detail=detail,
        )

    approved = await ctx.audit.find(thread_id, digest, ("approved",))
    if approved is None or approved.tenant_id != tenant_id:
        await ctx.audit.append(entry("blocked", ctx.actor.user_id, {"reason": "no matching approval row"}))
        emit(runtime, {"type": "execution", "status": "blocked"})
        return {"execution": {"status": "blocked", "error": "no matching approval for this exact action"}}

    already = await ctx.audit.find(thread_id, digest, ("executed",))
    if already is not None:
        # Node re-ran after a crash or replay: never execute twice.
        return {"execution": {"status": "executed", "result": already.detail.get("result"), "replayed": True}}

    try:
        result = await ctx.backend.apply_action(action, ctx.actor)
    except TransientBackendError:
        raise  # RetryPolicy
    except Exception as exc:  # noqa: BLE001 - any backend failure is recorded, then reported
        await ctx.audit.append(
            entry("failed", approved.approver_id, {"error": str(exc), "executed_as": ctx.actor.username})
        )
        emit(runtime, {"type": "execution", "status": "failed", "error": str(exc)})
        return {"execution": {"status": "failed", "error": str(exc)}}

    with contextlib.suppress(DuplicateAuditEntry):  # a re-run already recorded it
        await ctx.audit.append(
            entry("executed", approved.approver_id, {"result": result, "executed_as": ctx.actor.username})
        )
    emit(runtime, {"type": "execution", "status": "executed", "result": result})
    return {"execution": {"status": "executed", "result": result, "executed_as": ctx.actor.username}}


def after_execute(state: dict) -> str:
    return "verify" if (state.get("execution") or {}).get("status") == "executed" else "finalize"


async def check_health(backend: Any, namespace: str, deployments: list[str]) -> tuple[bool, list[str], int]:
    """One health snapshot: (all pods Running and ready, human-readable checks, total restarts)."""
    checks: list[str] = []
    healthy = True
    restarts = 0
    for deployment in deployments:
        pods = await backend.list_pods(namespace, deployment)
        restarts += sum(int(p.get("restarts") or 0) for p in pods)
        unhealthy = [
            p["name"] for p in pods if p["status"] != "Running" or p["ready"].split("/")[0] != p["ready"].split("/")[1]
        ]
        if not pods:
            healthy = False
            checks.append(f"{deployment}: no pods found")
        elif unhealthy:
            healthy = False
            checks.append(f"{deployment}: {len(unhealthy)}/{len(pods)} pods unhealthy ({', '.join(unhealthy)})")
        else:
            checks.append(f"{deployment}: all {len(pods)} pods Running and ready")
    return healthy, checks, restarts


def make_verify(settings: Settings):
    timeout_s, stable_s = settings.verify_window
    interval_s = settings.verify_interval_s

    async def verify(state: dict, runtime: Runtime[CopilotContext]) -> dict:
        """Deterministic post-action check: do the alerting and target deployments become and stay healthy?

        On a real cluster a rollout takes time, and a wrong fix (say, a restart for an OOM) can look healthy
        for a few seconds before crashing again. So: poll until every pod is Running and ready with no new
        restarts for `stable_s`, or give up after `timeout_s`.
        """
        backend = runtime.context.backend
        action = parse_action(state["proposal"]["action"])
        deployments = list(dict.fromkeys([state["alert"]["service"], action.deployment]))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        stable_since: float | None = None
        baseline_restarts: int | None = None
        while True:
            healthy, checks, restarts = await check_health(backend, action.namespace, deployments)
            now = loop.time()
            if healthy and (baseline_restarts is None or restarts <= baseline_restarts):
                if stable_since is None:
                    stable_since, baseline_restarts = now, restarts
                if now - stable_since >= stable_s:
                    resolved = True
                    break
            else:
                stable_since = baseline_restarts = None
            if now >= deadline:
                resolved = False
                break
            emit(runtime, {"type": "verification_progress", "healthy": healthy, "checks": checks})
            await asyncio.sleep(interval_s)
        if stable_s:
            checks.append(
                f"stable for {stable_s:.0f}s with no new restarts"
                if resolved
                else f"not stable within {timeout_s:.0f}s"
            )
        verification = {"resolved": resolved, "checks": checks}
        emit(runtime, {"type": "verification", **verification})
        return {"verification": verification}

    return verify
