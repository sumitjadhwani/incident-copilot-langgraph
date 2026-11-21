"""Deterministic tests for the approval gate, executor, approval service and audit chain."""

import dataclasses

import pytest
from langgraph.types import Command

from copilot.approval.audit import AuditEntry, DuplicateAuditEntry, InMemoryAuditStore
from copilot.approval.hashing import action_hash
from copilot.approval.service import get_pending_approval
from copilot.auth.rbac import ThreadInfo
from copilot.errors import Conflict, Forbidden, NotFound
from copilot.tools.write_tools import RiskPolicy
from tests.fakes import NS, oom_responder
from tests.helpers import ALICE, BOB, CAROL, Harness, make_settings


async def _pending_hash(h: Harness) -> str:
    pending = await get_pending_approval(h.graph, h.thread_id)
    assert pending is not None
    return pending.payload["action_hash"]


async def test_approve_executes_verifies_and_audits():
    h = Harness()
    await h.start()
    digest = await _pending_hash(h)

    command = await h.approvals.decide(thread=h.thread, approver=BOB, decision="approve", action_hash=digest)
    result = await h.resume(command, actor=BOB)

    assert result["execution"]["status"] == "executed"
    assert result["execution"]["executed_as"] == "bob"
    assert result["verification"]["resolved"] is True
    assert result["status"] == "resolved"
    assert len(h.backend.applied) == 1
    statuses = [r.status for r in h.audit.records]
    assert statuses == ["approved", "executed"]
    assert all(r.approver_id == BOB.user_id for r in h.audit.records)
    assert h.audit.records[0].checkpoint_id is not None
    assert (await h.audit.verify_chain()).ok


async def test_reject_never_executes():
    h = Harness()
    await h.start()
    digest = await _pending_hash(h)
    command = await h.approvals.decide(
        thread=h.thread, approver=BOB, decision="reject", action_hash=digest, comment="not during peak"
    )
    result = await h.resume(command)
    assert result["status"] == "action_rejected"
    assert h.backend.applied == []
    assert [r.status for r in h.audit.records] == ["rejected"]


async def test_service_rejects_stale_hash():
    h = Harness()
    await h.start()
    with pytest.raises(Conflict, match="does not match"):
        await h.approvals.decide(thread=h.thread, approver=BOB, decision="approve", action_hash="0" * 64)
    assert h.audit.records == []


async def test_node_rejects_resume_with_wrong_hash_even_if_service_bypassed():
    h = Harness()
    await h.start()
    result = await h.resume(Command(resume={"decision": "approve", "action_hash": "f" * 64, "approver_id": "x"}))
    assert result["approval"]["decision"] == "invalid"
    assert result["status"] == "approval_invalid"
    assert h.backend.applied == []


async def test_executor_blocks_when_no_approval_row_exists():
    """Defense in depth: a correct resume payload without the audited approval still cannot execute."""
    h = Harness()
    await h.start()
    digest = await _pending_hash(h)
    result = await h.resume(Command(resume={"decision": "approve", "action_hash": digest, "approver_id": "x"}))
    assert result["execution"]["status"] == "blocked"
    assert result["status"] == "action_failed"
    assert h.backend.applied == []
    assert [r.status for r in h.audit.records] == ["blocked"]


async def test_second_decision_is_a_conflict_and_does_not_resume_twice():
    h = Harness()
    await h.start()
    digest = await _pending_hash(h)
    command = await h.approvals.decide(thread=h.thread, approver=BOB, decision="approve", action_hash=digest)
    await h.resume(command)
    with pytest.raises(Conflict, match="already been decided"):
        await h.approvals.decide(thread=h.thread, approver=BOB, decision="approve", action_hash=digest)
    assert len(h.backend.applied) == 1


async def test_concurrent_double_click_records_one_decision():
    h = Harness()
    await h.start()
    digest = await _pending_hash(h)
    await h.approvals.decide(thread=h.thread, approver=BOB, decision="approve", action_hash=digest)
    # The interrupt is still pending (not resumed yet); the unique decision index stops the second one.
    with pytest.raises(Conflict):
        await h.approvals.decide(thread=h.thread, approver=BOB, decision="approve", action_hash=digest)
    assert [r.status for r in h.audit.records] == ["approved"]


@pytest.mark.parametrize(
    ("approver", "error"),
    [
        (ALICE, Forbidden),  # no sre-approver role (and she started it)
        (CAROL, NotFound),  # other tenant: indistinguishable from a missing thread
    ],
)
async def test_rbac_on_decisions(approver, error):
    h = Harness()
    await h.start()
    digest = await _pending_hash(h)
    with pytest.raises(error):
        await h.approvals.decide(thread=h.thread, approver=approver, decision="approve", action_hash=digest)
    assert h.audit.records == []


async def test_four_eyes_rule():
    h = Harness()
    await h.start()
    digest = await _pending_hash(h)
    own_thread = ThreadInfo(h.thread_id, BOB.tenant_id, BOB.user_id)
    with pytest.raises(Forbidden, match="four-eyes"):
        await h.approvals.decide(thread=own_thread, approver=BOB, decision="approve", action_hash=digest)

    h.approvals.settings = make_settings(require_distinct_approver=False)
    await h.approvals.decide(thread=own_thread, approver=BOB, decision="approve", action_hash=digest)


async def test_low_risk_action_is_auto_approved_and_audited():
    restart = {"tool": "restart_deployment", "namespace": NS, "deployment": "checkout-api"}
    h = Harness(responder=oom_responder(action=restart), risk_policy=RiskPolicy(protected_namespaces=()))
    result = await h.start()
    assert await get_pending_approval(h.graph, h.thread_id) is None
    assert result["approval"]["approver_id"] == "policy:auto-approve"
    assert result["execution"]["status"] == "executed"
    # Restart does not fix an OOM in the scenario, and verification says so.
    assert result["status"] == "not_resolved"
    assert [r.status for r in h.audit.records] == ["approved", "executed"]


async def test_replaying_from_before_execution_does_not_execute_twice():
    h = Harness()
    await h.start()
    digest = await _pending_hash(h)
    command = await h.approvals.decide(thread=h.thread, approver=BOB, decision="approve", action_hash=digest)
    await h.resume(command)
    assert len(h.backend.applied) == 1

    # Time travel: find the checkpoint whose next step is execute_action and replay from it.
    history = [s async for s in h.graph.aget_state_history(h.config)]
    before_exec = next(s for s in history if s.next == ("execute_action",))
    result = await h.graph.ainvoke(None, before_exec.config, context=h.context(BOB))

    assert result["execution"]["replayed"] is True
    assert len(h.backend.applied) == 1
    assert [r.status for r in h.audit.records] == ["approved", "executed"]


def test_action_hash_binds_args_thread_and_tenant():
    action = {"tool": "scale_deployment", "namespace": NS, "deployment": "x", "replicas": 3}
    base = action_hash(action, thread_id="t1", tenant_id="acme")
    assert base == action_hash(dict(reversed(list(action.items()))), thread_id="t1", tenant_id="acme")
    assert base != action_hash({**action, "replicas": 4}, thread_id="t1", tenant_id="acme")
    assert base != action_hash(action, thread_id="t2", tenant_id="acme")
    assert base != action_hash(action, thread_id="t1", tenant_id="globex")


async def test_audit_chain_detects_tampering():
    audit = InMemoryAuditStore()
    for i in range(3):
        await audit.append(
            AuditEntry(
                tenant_id="acme",
                thread_id=f"t{i}",
                approver_id="u",
                status="approved",
                action_tool="scale_deployment",
                action_payload={"i": i},
                action_hash=f"h{i}",
            )
        )
    assert (await audit.verify_chain()).ok

    audit.records[1] = dataclasses.replace(audit.records[1], approver_id="mallory")
    report = await audit.verify_chain()
    assert not report.ok and report.first_bad_id == 2

    audit.records[1] = dataclasses.replace(audit.records[1], approver_id="u")
    del audit.records[1]
    assert not (await audit.verify_chain()).ok


async def test_audit_unique_decision_per_action():
    audit = InMemoryAuditStore()
    entry = AuditEntry(
        tenant_id="acme",
        thread_id="t",
        approver_id="u",
        status="approved",
        action_tool="x",
        action_payload={},
        action_hash="h",
    )
    await audit.append(entry)
    with pytest.raises(DuplicateAuditEntry):
        await audit.append(dataclasses.replace(entry, status="rejected"))
    await audit.append(dataclasses.replace(entry, status="executed"))
    with pytest.raises(DuplicateAuditEntry):
        await audit.append(dataclasses.replace(entry, status="executed"))
