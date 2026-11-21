"""Scenario evals: run the real graph and LLM against mocked tools, N times per scenario, and grade.

Checks per run:
- category:   diagnosis category matches (false alarms: triage says not actionable or diagnosis no_issue)
- action:     proposed tool is acceptable and targets the right deployment (false alarms: no action)
- resolved:   after an automatic approval, the mock cluster verifies healthy (false alarms: no_action_needed)
- trajectory: required tools were called, the tool-call budget held, and nothing executed without an
              `approved` audit row preceding it

Approval is automatic here (a dedicated eval approver through the real ApprovalService), because the
gate itself is covered by deterministic unit tests, not by LLM evals.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from copilot.approval.audit import InMemoryAuditStore
from copilot.approval.service import ApprovalService, get_pending_approval
from copilot.auth.rbac import ThreadInfo
from copilot.config import Models, Settings
from copilot.context import Actor, CopilotContext, ToolCallRecorder
from copilot.graph.build import build_graph
from copilot.memory.store import seed_runbooks
from copilot.scenarios import Scenario, load_scenario
from copilot.tools.mock_backend import ScenarioBackend
from copilot.tools.write_tools import RiskPolicy

EVAL_TENANT = "eval"
REQUESTER = Actor("eval-responder", "eval-responder", EVAL_TENANT, frozenset({"sre"}))
APPROVER = Actor("eval-approver", "eval-approver", EVAL_TENANT, frozenset({"sre", "sre-approver"}))
CHECKS = ("category", "action", "resolved", "trajectory")


@dataclass
class EvalResult:
    scenario_id: str
    run: int
    passed: bool
    checks: dict[str, bool]
    category: str | None = None
    action: dict[str, Any] | None = None
    status: str | None = None
    tool_calls: int = 0
    tools_used: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    notes: list[str] = field(default_factory=list)
    error: str | None = None


def grade(
    scenario: Scenario, state: dict[str, Any], tools: list[str], audit_statuses: list[str]
) -> tuple[dict[str, bool], list[str]]:
    exp = scenario.expected
    notes: list[str] = []
    triage = state.get("triage") or {}
    diagnosis = state.get("diagnosis") or {}
    action = (state.get("proposal") or {}).get("action")
    status = state.get("status")
    false_alarm = not exp.acceptable_actions

    if false_alarm:
        category_ok = (triage and not triage.get("actionable", True)) or diagnosis.get("category") == "no_issue"
        action_ok = action is None
        resolved_ok = status == "no_action_needed"
    else:
        category_ok = diagnosis.get("category") == exp.root_cause_category
        action_ok = (
            bool(action)
            and action["tool"] in exp.acceptable_actions
            and (exp.action_target is None or action.get("deployment") == exp.action_target)
        )
        resolved_ok = status == "resolved"
    if not category_ok:
        got = diagnosis.get("category") or ("not actionable" if triage and not triage.get("actionable") else None)
        notes.append(f"category {got} != {exp.root_cause_category}")
    if not action_ok:
        notes.append(f"action {json.dumps(action)} not in {exp.acceptable_actions} on {exp.action_target}")
    if not resolved_ok:
        notes.append(f"final status {status}")

    missing = [t for t in exp.required_tools if t not in tools]
    over_budget = len(tools) > exp.max_tool_calls
    executed_without_approval = "executed" in audit_statuses and (
        "approved" not in audit_statuses or audit_statuses.index("approved") > audit_statuses.index("executed")
    )
    trajectory_ok = not missing and not over_budget and not executed_without_approval
    if missing:
        notes.append(f"required tools not called: {missing}")
    if over_budget:
        notes.append(f"{len(tools)} tool calls > budget {exp.max_tool_calls}")
    if executed_without_approval:
        notes.append("executed without a preceding approval")
    return {
        "category": bool(category_ok),
        "action": bool(action_ok),
        "resolved": bool(resolved_ok),
        "trajectory": trajectory_ok,
    }, notes


async def run_once(scenario_id: str, run: int, models: Models, settings: Settings) -> EvalResult:
    scenario = load_scenario(scenario_id)
    store = InMemoryStore()
    await seed_runbooks(store, [EVAL_TENANT])
    audit = InMemoryAuditStore()
    graph = build_graph(models, settings=settings, checkpointer=InMemorySaver(), store=store)
    recorder = ToolCallRecorder()
    backend = ScenarioBackend(scenario)
    risk = RiskPolicy.from_settings(settings.risk_low_tools, settings.risk_protected_namespaces)
    thread_id = f"eval-{scenario_id}-{run}-{uuid.uuid4().hex[:6]}"
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": settings.recursion_limit,
        "tags": ["eval", f"scenario:{scenario_id}"],
        "metadata": {"eval_run": run},
    }

    def ctx(actor: Actor) -> CopilotContext:
        return CopilotContext(backend=backend, actor=actor, audit=audit, risk_policy=risk, recorder=recorder)

    started = time.perf_counter()
    try:
        inputs = {"alert": scenario.alert.model_dump(), "tenant_id": EVAL_TENANT, "requested_by": REQUESTER.user_id}
        state = await graph.ainvoke(inputs, config, context=ctx(REQUESTER))
        pending = await get_pending_approval(graph, thread_id)
        if pending is not None:
            command = await ApprovalService(graph, audit, settings).decide(
                thread=ThreadInfo(thread_id, EVAL_TENANT, REQUESTER.user_id),
                approver=APPROVER,
                decision="approve",
                action_hash=pending.payload["action_hash"],
                comment="eval auto-approval",
            )
            state = await graph.ainvoke(command, config, context=ctx(APPROVER))
    except Exception as exc:  # noqa: BLE001 - a crashed run is a failed run, not a crashed suite
        return EvalResult(
            scenario_id,
            run,
            False,
            dict.fromkeys(CHECKS, False),
            tool_calls=len(recorder.calls),
            duration_s=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
        )

    tools = recorder.tool_names
    checks, notes = grade(scenario, state, tools, [r.status for r in audit.records])
    return EvalResult(
        scenario_id=scenario_id,
        run=run,
        passed=all(checks.values()),
        checks=checks,
        category=(state.get("diagnosis") or {}).get("category"),
        action=(state.get("proposal") or {}).get("action"),
        status=state.get("status"),
        tool_calls=len(tools),
        tools_used=sorted(set(tools)),
        duration_s=time.perf_counter() - started,
        notes=notes,
    )


async def run_suite(
    scenario_ids: list[str], runs: int, models: Models, settings: Settings, concurrency: int = 6, on_result: Any = None
) -> list[EvalResult]:
    sem = asyncio.Semaphore(concurrency)

    async def one(sid: str, i: int) -> EvalResult:
        async with sem:
            result = await run_once(sid, i, models, settings)
            if on_result:
                on_result(result)
            return result

    return list(await asyncio.gather(*(one(sid, i) for sid in scenario_ids for i in range(runs))))


def summarize(results: list[EvalResult]) -> list[dict[str, Any]]:
    rows = []
    for sid in sorted({r.scenario_id for r in results}):
        rs = [r for r in results if r.scenario_id == sid]
        n = len(rs)
        rows.append(
            {
                "scenario": sid,
                "runs": n,
                "pass_rate": sum(r.passed for r in rs) / n,
                **{f"{c}_rate": sum(r.checks[c] for r in rs) / n for c in CHECKS},
                "median_tool_calls": statistics.median(r.tool_calls for r in rs),
                "median_duration_s": round(statistics.median(r.duration_s for r in rs), 1),
                "errors": sum(r.error is not None for r in rs),
            }
        )
    return rows


def render_markdown(rows: list[dict[str, Any]], results: list[EvalResult], meta: dict[str, Any]) -> str:
    total = len(results)
    overall = sum(r.passed for r in results) / total if total else 0.0
    lines = [
        "# Eval report",
        "",
        f"- model: `{meta['model']}`",
        f"- runs per scenario: {meta['runs']}",
        f"- date: {meta['date']}",
        f"- overall pass rate: **{overall:.0%}** ({sum(r.passed for r in results)}/{total})",
        "",
        "| scenario | runs | pass | category | action | resolved | trajectory | median tools | median s |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['scenario']} | {r['runs']} | **{r['pass_rate']:.0%}** | {r['category_rate']:.0%} | "
            f"{r['action_rate']:.0%} | {r['resolved_rate']:.0%} | {r['trajectory_rate']:.0%} | "
            f"{r['median_tool_calls']:g} | {r['median_duration_s']} |"
        )
    failures = [r for r in results if not r.passed]
    if failures:
        lines += ["", "## Failures", ""]
        for r in failures:
            lines.append(f"- `{r.scenario_id}` run {r.run}: {r.error or '; '.join(r.notes)}")
    return "\n".join(lines) + "\n"


def write_report(out_dir: Path, rows: list[dict[str, Any]], results: list[EvalResult], meta: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps({"meta": meta, "summary": rows, "runs": [asdict(r) for r in results]}, indent=2, default=str)
    )
    report = out_dir / "report.md"
    report.write_text(render_markdown(rows, results, meta), encoding="utf-8")
    return report
