"""The eval harness grades correctly (scripted model, no LLM)."""

from copilot.config import Models
from copilot.evals.harness import grade, run_once, summarize
from copilot.scenarios import load_scenario
from tests.fakes import FakeModel, oom_responder
from tests.helpers import make_settings


async def test_run_once_passes_for_a_competent_copilot():
    model = FakeModel(oom_responder())
    result = await run_once("oomkilled_loop", 0, Models(model, model), make_settings())
    # The scripted model never reads logs, so only the trajectory check fails.
    assert result.checks == {"category": True, "action": True, "resolved": True, "trajectory": False}
    assert "get_pod_logs" in " ".join(result.notes)
    assert result.status == "resolved"


async def test_wrong_action_fails_action_and_resolved():
    restart = {"tool": "restart_deployment", "namespace": "acme-prod", "deployment": "checkout-api"}
    model = FakeModel(oom_responder(action=restart))
    result = await run_once("oomkilled_loop", 0, Models(model, model), make_settings())
    assert not result.checks["action"] and not result.checks["resolved"]


def test_grade_false_alarm():
    s = load_scenario("false_alarm_memory")
    ok_state = {"triage": {"actionable": False}, "status": "no_action_needed"}
    checks, _ = grade(s, ok_state, ["query_metrics"], [])
    assert all(checks.values())

    bad_state = {
        "triage": {"actionable": True},
        "diagnosis": {"category": "oom"},
        "proposal": {"action": {"tool": "patch_resources", "deployment": "reports-worker"}},
        "status": "resolved",
    }
    checks, notes = grade(s, bad_state, ["query_metrics"], ["approved", "executed"])
    assert checks == {"category": False, "action": False, "resolved": False, "trajectory": True}
    assert notes


def test_grade_flags_execution_without_approval_and_budget():
    s = load_scenario("bad_env_var")
    state = {
        "diagnosis": {"category": "bad_config"},
        "status": "resolved",
        "proposal": {"action": {"tool": "set_env", "deployment": "notification-svc"}},
    }
    checks, notes = grade(s, state, ["get_pod_logs"] * 61, ["executed"])
    assert not checks["trajectory"]
    assert any("budget" in n for n in notes) and any("without" in n for n in notes)


def test_summarize_rates():
    from copilot.evals.harness import EvalResult

    rs = [
        EvalResult(
            "a",
            i,
            i < 2,
            {"category": True, "action": i < 2, "resolved": i < 2, "trajectory": True},
            tool_calls=10 + i,
            duration_s=5.0,
        )
        for i in range(3)
    ]
    (row,) = summarize(rs)
    assert row["pass_rate"] == 2 / 3 and row["category_rate"] == 1.0 and row["median_tool_calls"] == 11
