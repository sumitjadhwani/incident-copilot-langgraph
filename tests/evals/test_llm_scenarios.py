"""LLM-backed scenario evals (marked `eval`, excluded from the default test run).

    uv run pytest -m eval                                   # 3 runs per scenario
    EVAL_RUNS=5 EVAL_PASS_THRESHOLD=0.8 uv run pytest -m eval

The whole suite runs once (concurrently) in a session fixture; each scenario then asserts its pass
rate against the threshold. A report is written to eval-results/.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from copilot.config import PROJECT_ROOT, get_settings, make_models, run_async
from copilot.evals.harness import run_suite, summarize, write_report
from copilot.scenarios import list_scenarios

pytestmark = pytest.mark.eval

RUNS = int(os.environ.get("EVAL_RUNS", "3"))
THRESHOLD = float(os.environ.get("EVAL_PASS_THRESHOLD", "0.66"))


@pytest.fixture(scope="session")
def suite():
    settings = get_settings()
    key = {"deepseek": "DEEPSEEK_API_KEY", "google_genai": "GOOGLE_API_KEY"}[settings.llm_provider]
    if not os.environ.get(key):
        pytest.skip(f"{key} not set")
    results = run_async(run_suite(list_scenarios(), RUNS, make_models(settings), settings))
    rows = summarize(results)
    meta = {
        "model": f"{settings.llm_provider}/{settings.resolved_model}",
        "runs": RUNS,
        "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }
    write_report(PROJECT_ROOT / "eval-results", rows, results, meta)
    return {r["scenario"]: r for r in rows}, results


@pytest.mark.parametrize("scenario_id", list_scenarios())
def test_scenario_pass_rate(suite, scenario_id):
    rows, results = suite
    row = rows[scenario_id]
    failures = [r for r in results if r.scenario_id == scenario_id and not r.passed]
    detail = "; ".join(f"run {r.run}: {r.error or ', '.join(r.notes)}" for r in failures)
    assert row["pass_rate"] >= THRESHOLD, f"{scenario_id} pass rate {row['pass_rate']:.0%} < {THRESHOLD:.0%}. {detail}"
