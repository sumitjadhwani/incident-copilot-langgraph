"""Graph flow tests with a scripted model (no LLM calls)."""

from langgraph.store.memory import InMemoryStore

from copilot.approval.service import get_pending_approval
from copilot.graph.supervisor import next_step
from copilot.memory.store import incidents_ns, recall_similar, seed_runbooks
from copilot.tools.mock_backend import FlakyBackend, ScenarioBackend
from copilot.tools.read_tools import READ_TOOLS
from tests.fakes import oom_responder
from tests.helpers import Harness, make_settings


async def test_runs_until_approval_interrupt():
    h = Harness()
    await h.start()

    pending = await get_pending_approval(h.graph, h.thread_id)
    assert pending is not None
    assert pending.payload["action"]["tool"] == "patch_resources"
    assert pending.payload["risk"] == "high"
    assert len(pending.payload["action_hash"]) == 64

    state = await h.state()
    assert state["triage"]["actionable"] is True
    assert state["diagnosis"]["category"] == "oom"
    # Two hypotheses were investigated in parallel branches, each producing a verdict.
    assert {v["hypothesis_id"] for v in state["verdicts"]} == {"h1", "h2"}
    assert "execution" not in state or state["execution"] is None
    assert h.backend.applied == []


async def test_subagents_used_tools_through_recorder():
    h = Harness()
    await h.start()
    agents = {c["agent"] for c in h.recorder.calls}
    assert {"triage", "investigator", "remediation"} <= agents
    assert all(c["ok"] for c in h.recorder.calls)


async def test_not_actionable_finishes_without_diagnosis():
    h = Harness(responder=oom_responder(actionable=False))
    result = await h.start()
    assert result["status"] == "no_action_needed"
    assert result.get("diagnosis") is None
    assert ("hypotheses", None) not in [(r, None) for r, _ in h.model.calls]


async def test_low_confidence_retries_diagnosis_then_escalates():
    h = Harness(responder=oom_responder(confidence="low"))
    result = await h.start()
    assert result["diagnosis_attempts"] == 2
    assert result["status"] == "escalated"
    assert result.get("approval") is None


async def test_no_action_proposed_escalates():
    h = Harness(responder=oom_responder(action=None))
    result = await h.start()
    assert result["status"] == "escalated"
    assert await get_pending_approval(h.graph, h.thread_id) is None


async def test_transient_tool_failures_are_retried():
    flaky = FlakyBackend(ScenarioBackend(Harness().scenario), failures_per_method=1)
    h = Harness(backend=flaky)
    await h.start()
    assert flaky.failures["describe_pod"] == 1
    assert flaky.calls["describe_pod"] > 1
    assert any(not c["ok"] for c in h.recorder.calls)
    assert await get_pending_approval(h.graph, h.thread_id) is not None


async def test_memory_recall_is_tenant_scoped():
    store = InMemoryStore()
    await store.aput(incidents_ns("acme"), "old", {"service": "checkout-api", "category": "oom"})
    assert await recall_similar(store, "acme", "checkout-api") == [{"service": "checkout-api", "category": "oom"}]
    assert await recall_similar(store, "globex", "checkout-api") == []


async def test_finalize_remembers_incident_and_next_run_recalls_it():
    h = Harness(responder=oom_responder(actionable=False))
    await h.start()
    remembered = await recall_similar(h.store, "acme", "checkout-api")
    assert remembered and remembered[0]["status"] == "no_action_needed"

    h2 = Harness(responder=oom_responder(actionable=False), store=h.store)
    result = await h2.start()
    assert result["similar_incidents"][0]["category"] == "no_issue"
    assert result["similar_incidents"][0]["service"] == "checkout-api"

    other_tenant = await recall_similar(h.store, "globex", "checkout-api")
    assert other_tenant == []


async def test_runbook_is_loaded_for_remediation():
    h = Harness()
    await seed_runbooks(h.store, ["acme"])
    await h.start()
    state = await h.state()
    assert state.get("runbook") and "memory" in state["runbook"].lower()


def test_supervisor_routing_rules():
    s = make_settings()
    assert next_step({}, s)[0] == "triage"
    assert next_step({"triage": {"actionable": False}}, s)[0] == "finalize"
    assert next_step({"triage": {"actionable": True}}, s)[0] == "diagnosis"
    diag = {"category": "oom", "confidence": "high"}
    assert (
        next_step({"triage": {"actionable": True}, "diagnosis": diag, "diagnosis_attempts": 1}, s)[0] == "remediation"
    )
    done = {"triage": {"actionable": True}, "diagnosis": diag, "diagnosis_attempts": 1, "remediation_done": True}
    assert next_step({**done, "proposal": {"action": {"tool": "x"}}}, s)[0] == "approval_gate"
    assert next_step({**done, "proposal": {"action": None}}, s)[0] == "finalize"


def test_runtime_is_hidden_from_tool_schemas():
    for t in READ_TOOLS:
        assert "runtime" not in t.tool_call_schema.model_json_schema()["properties"], t.name
