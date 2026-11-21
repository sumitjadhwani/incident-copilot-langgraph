"""Run the scenario eval suite against the configured LLM (tools are mocked).

    uv run python scripts/run_evals.py                       # all scenarios, 3 runs each
    uv run python scripts/run_evals.py --runs 5 --scenarios oomkilled_loop bad_env_var
    uv run python scripts/run_evals.py --threshold 0.66      # exit 1 if any scenario is below

Writes eval-results/report.md and eval-results/results.json.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

from copilot.config import PROJECT_ROOT, get_settings, make_models, run_async
from copilot.evals.harness import render_markdown, run_suite, summarize, write_report
from copilot.scenarios import list_scenarios


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not args.trace:
        os.environ["LANGSMITH_TRACING"] = "false"
    scenarios = args.scenarios or list_scenarios()
    print(
        f"model {settings.llm_provider}/{settings.resolved_model}: {len(scenarios)} scenarios x {args.runs} runs "
        f"(concurrency {args.concurrency})"
    )

    def progress(r) -> None:
        mark = "PASS" if r.passed else "FAIL"
        detail = r.error or "; ".join(r.notes)
        print(f"  {mark} {r.scenario_id} #{r.run} {r.duration_s:.0f}s tools={r.tool_calls} {detail}", flush=True)

    results = await run_suite(scenarios, args.runs, make_models(settings), settings, args.concurrency, progress)
    rows = summarize(results)
    meta = {
        "model": f"{settings.llm_provider}/{settings.resolved_model}",
        "runs": args.runs,
        "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }
    report = write_report(PROJECT_ROOT / "eval-results", rows, results, meta)
    print()
    print(render_markdown(rows, results, meta))
    print(f"report: {report}")
    below = [r["scenario"] for r in rows if r["pass_rate"] < args.threshold]
    if below:
        print(f"below threshold {args.threshold:.0%}: {below}")
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--scenarios", nargs="*", choices=list_scenarios())
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--threshold", type=float, default=0.0)
    parser.add_argument("--trace", action="store_true", help="send eval runs to LangSmith")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(run_async(main(parser.parse_args())))
