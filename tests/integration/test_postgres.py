"""Postgres-backed tests: audit immutability, hash chain, durable resume, tenancy.

They run against a separate `copilot_test` database on the docker-compose Postgres and are skipped
when it is not reachable.
"""

from __future__ import annotations

import dataclasses
import os
import uuid

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from copilot.approval.audit import AuditEntry, DuplicateAuditEntry
from copilot.approval.service import ApprovalService, get_pending_approval
from copilot.config import Models, run_async
from copilot.db import ensure_database, migrate, persistence
from copilot.graph.build import build_graph
from copilot.memory.store import incidents_ns, recall_similar
from copilot.threads import ThreadRecord
from copilot.tools.mock_backend import FlakyBackend, ScenarioBackend
from tests.fakes import FakeModel, oom_responder
from tests.helpers import ALICE, BOB, Harness, make_settings

pytestmark = pytest.mark.db

TEST_DB = "copilot_test"
ADMIN_URL = os.environ.get("COPILOT_TEST_ADMIN_DATABASE_URL", "postgresql://copilot:copilot@localhost:5432/copilot")
APP_URL_BASE = os.environ.get(
    "COPILOT_TEST_DATABASE_URL", "postgresql://copilot_app:copilot_app@localhost:5432/copilot"
)


def _with_db(url: str, dbname: str) -> str:
    params = conninfo_to_dict(url)
    params["dbname"] = dbname
    return make_conninfo(**params)


ADMIN_TEST_URL = _with_db(ADMIN_URL, TEST_DB)
APP_TEST_URL = _with_db(APP_URL_BASE, TEST_DB)


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    try:
        run_async(ensure_database(ADMIN_URL, TEST_DB))
        run_async(migrate(ADMIN_TEST_URL, APP_TEST_URL))
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable: {exc}")


@pytest.fixture
def settings():
    return make_settings(database_url=APP_TEST_URL, admin_database_url=ADMIN_TEST_URL)


def _entry(**overrides) -> AuditEntry:
    base = dict(
        tenant_id="acme",
        thread_id=f"t-{uuid.uuid4().hex[:8]}",
        approver_id="u-bob",
        status="approved",
        action_tool="scale_deployment",
        action_payload={"replicas": 3},
        action_hash=uuid.uuid4().hex,
    )
    return AuditEntry(**{**base, **overrides})


async def test_audit_append_and_chain(settings):
    async with persistence(settings) as p:
        first = await p.audit.append(_entry())
        second = await p.audit.append(_entry())
        assert second.prev_hash == first.row_hash
        assert (await p.audit.verify_chain()).ok
        rows = await p.audit.list("acme", thread_id=first.thread_id)
        assert [r.row_hash for r in rows] == [first.row_hash]


async def test_audit_unique_decision(settings):
    async with persistence(settings) as p:
        entry = _entry()
        await p.audit.append(entry)
        with pytest.raises(DuplicateAuditEntry):
            await p.audit.append(dataclasses.replace(entry, status="rejected"))


async def test_app_role_cannot_update_or_delete_audit_rows(settings):
    async with persistence(settings) as p:
        record = await p.audit.append(_entry())
    async with await psycopg.AsyncConnection.connect(APP_TEST_URL, autocommit=True) as conn:
        for stmt in (
            "UPDATE audit_log SET approver_id = 'mallory' WHERE id = %s",
            "DELETE FROM audit_log WHERE id = %s",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute(stmt, (record.id,))


async def test_owner_is_stopped_by_trigger(settings):
    async with persistence(settings) as p:
        record = await p.audit.append(_entry())
    async with await psycopg.AsyncConnection.connect(ADMIN_TEST_URL, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            await conn.execute("UPDATE audit_log SET approver_id = 'mallory' WHERE id = %s", (record.id,))
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            await conn.execute("DELETE FROM audit_log WHERE id = %s", (record.id,))


async def test_thread_repository(settings):
    async with persistence(settings) as p:
        tid = f"t-{uuid.uuid4().hex[:8]}"
        await p.threads.create(ThreadRecord(tid, "acme", "u-alice", "alice", "oomkilled_loop", "title", "running"))
        await p.threads.set_status(tid, "awaiting_approval")
        row = await p.threads.get(tid)
        assert row and row.status == "awaiting_approval" and row.tenant_id == "acme"
        assert tid in [r.thread_id for r in await p.threads.list_for_tenant("acme")]
        assert tid not in [r.thread_id for r in await p.threads.list_for_tenant("globex")]


async def test_store_is_tenant_scoped(settings):
    async with persistence(settings) as p:
        service = f"svc-{uuid.uuid4().hex[:6]}"
        await p.store.aput(incidents_ns("acme"), uuid.uuid4().hex, {"service": service, "category": "oom"})
        assert len(await recall_similar(p.store, "acme", service)) == 1
        assert await recall_similar(p.store, "globex", service) == []


async def test_approval_survives_process_restart(settings):
    """Pause at the approval interrupt, throw the graph and pool away, resume from Postgres."""
    h = Harness()  # only for scenario, context and inputs
    thread = dataclasses.replace(h.thread)

    async with persistence(settings) as p:
        model = FakeModel(oom_responder())
        graph = build_graph(Models(model, model), settings=settings, checkpointer=p.checkpointer, store=p.store)
        ctx = dataclasses.replace(h.context(ALICE), audit=p.audit)
        await graph.ainvoke(h.inputs(), h.config, context=ctx, durability="sync")
        pending = await get_pending_approval(graph, h.thread_id)
        assert pending is not None
        digest = pending.payload["action_hash"]

    # "New process": fresh pool, fresh graph object, nothing in memory.
    async with persistence(settings) as p:
        model = FakeModel(oom_responder())
        graph = build_graph(Models(model, model), settings=settings, checkpointer=p.checkpointer, store=p.store)
        service = ApprovalService(graph, p.audit, settings)
        command = await service.decide(thread=thread, approver=BOB, decision="approve", action_hash=digest)
        ctx = dataclasses.replace(h.context(BOB), audit=p.audit)
        result = await graph.ainvoke(command, h.config, context=ctx, durability="sync")

        assert result["status"] == "resolved"
        rows = await p.audit.list("acme", thread_id=h.thread_id)
        assert [r.status for r in rows] == ["approved", "executed"]
        # Only the resume ran models: triage/diagnosis/remediation were not repeated.
        assert {role for role, _ in model.calls} == {"report"}


async def test_resume_after_crash_continues_from_last_checkpoint(settings):
    """A run dies mid-investigation; re-invoking the thread continues instead of starting over."""
    h = Harness()
    backend = FlakyBackend(ScenarioBackend(h.scenario), failures_per_method=99)  # beyond RetryPolicy attempts

    async with persistence(settings) as p:
        model = FakeModel(oom_responder())
        graph = build_graph(Models(model, model), settings=settings, checkpointer=p.checkpointer, store=p.store)
        ctx = dataclasses.replace(h.context(ALICE), backend=backend, audit=p.audit)
        with pytest.raises(Exception, match="injected fault"):
            await graph.ainvoke(h.inputs(), h.config, context=ctx, durability="sync")
        calls_before = len(model.calls)
        assert ("triage", None) in model.calls

        backend.failures_per_method = 0  # the dependency recovered
        await graph.ainvoke(None, h.config, context=ctx, durability="sync")

        assert await get_pending_approval(graph, h.thread_id) is not None
        # Triage's tool-calling turns: one before the crash, one after the (retried) tool results.
        # Restarting from scratch would have repeated the first turn, giving three.
        assert calls_before == 1
        assert [c for c in model.calls if c == ("triage", None)] == [("triage", None)] * 2
