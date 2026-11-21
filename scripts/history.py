"""Time travel over a durable incident thread (Postgres checkpointer).

    uv run python scripts/history.py --thread-id <id>                          # list checkpoints
    uv run python scripts/history.py --thread-id <id> --show <checkpoint_id>   # state at a checkpoint
    uv run python scripts/history.py --thread-id <id> --replay <checkpoint_id> # re-run from there
    uv run python scripts/history.py --thread-id <id> --fork <checkpoint_id> \
        --update '{"diagnosis": null, "diagnosis_attempts": 0}' --as-node triage   # edit state, branch, re-run

Replays and forks write new checkpoints on the same thread; the original history is kept. The
executor's audit check still applies, so a replay can never execute an already-executed action twice,
and a replayed approval needs a new decision.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from copilot.config import get_settings, make_models, run_async
from copilot.console import print_event
from copilot.context import Actor, CopilotContext
from copilot.db import persistence
from copilot.graph.build import build_graph
from copilot.graph.common import preview
from copilot.runner import run_outcome, stream_events
from copilot.scenarios import load_scenario
from copilot.tools.mock_backend import ScenarioBackend
from copilot.tools.write_tools import RiskPolicy


def summarize(values: dict[str, Any]) -> str:
    parts = []
    if t := values.get("triage"):
        parts.append(f"triage(actionable={t.get('actionable')})")
    if d := values.get("diagnosis"):
        parts.append(f"diagnosis({d.get('category')}/{d.get('confidence')})")
    if p := values.get("proposal"):
        parts.append(f"proposal({(p.get('action') or {}).get('tool')})")
    if a := values.get("approval"):
        parts.append(f"approval({a.get('decision')})")
    if e := values.get("execution"):
        parts.append(f"execution({e.get('status')})")
    if s := values.get("status"):
        parts.append(f"status={s}")
    return " ".join(parts) or "-"


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    async with persistence(settings) as p:
        needs_llm = bool(args.replay or args.fork)
        graph = build_graph(
            make_models(settings) if needs_llm else _no_models(),
            settings=settings,
            checkpointer=p.checkpointer,
            store=p.store,
        )
        base = {"configurable": {"thread_id": args.thread_id}, "recursion_limit": settings.recursion_limit}

        if not (args.show or args.replay or args.fork):
            print(f"{'step':>4}  {'source':<7} {'next':<28} checkpoint_id                          summary")
            async for snap in graph.aget_state_history(base):
                meta = snap.metadata or {}
                nxt = ",".join(snap.next) + (" (paused)" if snap.interrupts else "")
                print(
                    f"{meta.get('step', ''):>4}  {meta.get('source', ''):<7} {nxt:<28} "
                    f"{snap.config['configurable']['checkpoint_id']}  {summarize(snap.values)}"
                )
            return 0

        target = args.show or args.replay or args.fork
        config = {**base, "configurable": {**base["configurable"], "checkpoint_id": target}}
        snapshot = await graph.aget_state(config)
        if not snapshot.values:
            print(f"checkpoint {target} not found on thread {args.thread_id}")
            return 1
        if args.show:
            print(json.dumps({k: v for k, v in snapshot.values.items()}, indent=2, default=str))
            print(f"\nnext: {list(snapshot.next)}")
            return 0

        record = await p.threads.get(args.thread_id)
        if record is None or not record.scenario_id:
            print("thread has no scenario on record; cannot rebuild the tool backend")
            return 1
        actor = Actor("cli-responder", "cli-responder", record.tenant_id, frozenset({settings.responder_role}))
        context = CopilotContext(
            backend=ScenarioBackend(load_scenario(record.scenario_id)),
            actor=actor,
            audit=p.audit,
            risk_policy=RiskPolicy.from_settings(settings.risk_low_tools, settings.risk_protected_namespaces),
        )
        if args.fork:
            update = json.loads(args.update or "{}")
            config = await graph.aupdate_state(config, update, as_node=args.as_node)
            print(f"forked: new checkpoint {config['configurable']['checkpoint_id']} with {preview(update, 200)}")
        else:
            print(f"replaying from {target} (next: {list(snapshot.next)})")

        async for event in stream_events(graph, None, config, context):
            print_event(event)
        outcome = await run_outcome(graph, base)
        print(f"\noutcome: {json.dumps(outcome, default=str)[:400]}")
        if outcome["status"] == "awaiting_approval":
            print("decide it in the UI or with scripts/run_incident.py --durable --thread-id", args.thread_id)
    return 0


def _no_models():
    """Listing and showing history never calls a model."""
    from copilot.config import Models

    class _Unused:
        def bind_tools(self, *_a: Any, **_k: Any) -> _Unused:
            return self

        def with_structured_output(self, *_a: Any, **_k: Any) -> _Unused:
            return self

    return Models(agent=_Unused(), structured=_Unused())  # type: ignore[arg-type]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--thread-id", required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--show", metavar="CHECKPOINT_ID")
    group.add_argument("--replay", metavar="CHECKPOINT_ID")
    group.add_argument("--fork", metavar="CHECKPOINT_ID")
    parser.add_argument("--update", help="JSON object of state keys to overwrite when forking")
    parser.add_argument("--as-node", help="node the update is attributed to (decides what runs next)")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(run_async(main(parser.parse_args())))
