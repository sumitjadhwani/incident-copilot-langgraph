"""Run the copilot against a synthetic scenario from the terminal.

    uv run python scripts/run_incident.py --scenario oomkilled_loop
    uv run python scripts/run_incident.py --durable                      # Postgres checkpointer/store/audit
    uv run python scripts/run_incident.py --durable --crash-after triage # simulate a crash
    uv run python scripts/run_incident.py --durable --thread-id <id>     # resume that thread
    uv run python scripts/run_incident.py --flaky 1                      # inject transient tool failures
    uv run python scripts/run_incident.py --approve auto                 # approve without prompting
    uv run python scripts/run_incident.py --scenario live_oom_checkout   # kind cluster via MCP, as alice/bob

The CLI acts as two dev identities in the configured tenant: a responder who starts the incident and
an approver (the four-eyes rule forbids approving your own incident).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from copilot.approval.audit import InMemoryAuditStore
from copilot.approval.service import ApprovalService
from copilot.auth.rbac import ThreadInfo
from copilot.config import Settings, get_settings, make_models, run_async
from copilot.console import print_event
from copilot.context import Actor, CopilotContext, ToolCallRecorder
from copilot.graph.build import build_graph
from copilot.memory.store import seed_runbooks
from copilot.observability.tracing import setup_tracing
from copilot.runner import run_outcome, stream_events
from copilot.scenarios import list_scenarios, load_scenario
from copilot.threads import ThreadRecord
from copilot.tools.mcp_backend import McpBackend
from copilot.tools.mock_backend import FlakyBackend, ScenarioBackend
from copilot.tools.write_tools import RiskPolicy


def keycloak_actor(settings: Settings, username: str) -> Actor:
    """Log in through the dev-only `copilot-cli` client (password = username in the demo realm)."""
    import httpx
    import jwt

    from copilot.auth.jwt import actor_from_claims

    r = httpx.post(
        f"{settings.keycloak_issuer}/protocol/openid-connect/token",
        timeout=10,
        data={"grant_type": "password", "client_id": "copilot-cli", "username": username, "password": username},
    )
    r.raise_for_status()
    token = r.json()["access_token"]
    # The MCP servers verify the signature; here we only read the claims to label the run.
    return actor_from_claims(jwt.decode(token, options={"verify_signature": False}), token)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    overrides = {k: v for k, v in {"llm_provider": args.provider, "llm_model": args.model}.items() if v}
    if overrides:
        settings = Settings(**{**settings.model_dump(), **overrides})
    if args.no_trace:
        os.environ["LANGSMITH_TRACING"] = "false"
    setup_tracing(settings.otel_endpoint, "incident-copilot-cli")  # set COPILOT_OTEL_ENDPOINT to export

    scenario = load_scenario(args.scenario)
    live = scenario.mode == "live"
    if live:
        # Real humans from Keycloak (dev-only password grant): the MCP servers impersonate them in Kubernetes.
        settings = Settings(**{**settings.model_dump(), "tool_backend": "mcp"})
        requester = keycloak_actor(settings, args.user)
        approver = keycloak_actor(settings, args.approver_user)
        tenant = requester.tenant_id
    else:
        tenant = settings.dev_tenant
        requester = Actor("cli-responder", "cli-responder", tenant, frozenset({settings.responder_role}))
        approver = Actor(
            "cli-approver", "cli-approver", tenant, frozenset({settings.responder_role, settings.approver_role})
        )
    mock_backend: Any = ScenarioBackend(scenario) if not live else None
    if args.flaky and mock_backend is not None:
        mock_backend = FlakyBackend(mock_backend, failures_per_method=args.flaky)
    risk = RiskPolicy.from_settings(settings.risk_low_tools, settings.risk_protected_namespaces)

    async with AsyncExitStack() as stack:
        threads = None
        if args.durable:
            from copilot.db import persistence

            p = await stack.enter_async_context(persistence(settings))
            checkpointer, store, audit, threads = p.checkpointer, p.store, p.audit, p.threads
        else:
            checkpointer, store, audit = InMemorySaver(), InMemoryStore(), InMemoryAuditStore()
            await seed_runbooks(store, [tenant])

        graph = build_graph(make_models(settings), settings=settings, checkpointer=checkpointer, store=store)
        thread_id = args.thread_id or f"cli-{uuid.uuid4().hex[:10]}"
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": settings.recursion_limit,
            "tags": [f"scenario:{scenario.id}"],
            "metadata": {"tenant_id": tenant},
        }
        recorder = ToolCallRecorder()

        def ctx(actor: Actor) -> CopilotContext:
            backend = McpBackend(settings, actor) if live else mock_backend
            return CopilotContext(backend=backend, actor=actor, audit=audit, risk_policy=risk, recorder=recorder)

        snapshot = await graph.aget_state(config)
        if args.thread_id and snapshot.values:
            print(f"Resuming thread {thread_id} (next: {list(snapshot.next) or 'nothing'})")
            inputs: Any = None
        else:
            alert = scenario.alert.model_dump()
            if alert.get("started_at") == "now":
                alert["started_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            inputs = {"alert": alert, "tenant_id": tenant, "requested_by": requester.user_id}
            if threads is not None:
                await threads.create(
                    ThreadRecord(
                        thread_id, tenant, requester.user_id, requester.username, scenario.id, scenario.title, "running"
                    )
                )
        print(
            f"Scenario: {scenario.title}\nModel:    {settings.llm_provider}/{settings.resolved_model}\n"
            f"Thread:   {thread_id}"
        )

        actor = requester
        while True:
            async for event in stream_events(graph, inputs, config, ctx(actor)):
                print_event(event)
                if args.crash_after and event.get("type") == "node_done" and event["node"] == args.crash_after:
                    print(
                        f"\n!! simulated crash after '{args.crash_after}'. Resume with: "
                        f"--durable --thread-id {thread_id}",
                        flush=True,
                    )
                    os._exit(3)  # no cleanup, like a killed process
            outcome = await run_outcome(graph, config)
            if threads is not None:
                await threads.set_status(thread_id, outcome["status"])
            if outcome["status"] != "awaiting_approval":
                break

            req = outcome["approval_request"]
            print("\n== APPROVAL REQUIRED ==========================================")
            print(f"action:   {json.dumps(req['action'])}")
            print(f"risk:     {req['risk']} ({'; '.join(req['reasons'])})")
            print(f"why:      {req['rationale']}")
            print(f"hash:     {req['action_hash']}")
            if args.approve == "prompt":
                answer = (await asyncio.to_thread(input, "approve as cli-approver? [y/N] ")).strip().lower()
                decision = "approve" if answer in ("y", "yes") else "reject"
            else:
                decision = "approve" if args.approve == "auto" else "reject"
            service = ApprovalService(graph, audit, settings)
            inputs = await service.decide(
                thread=ThreadInfo(thread_id, tenant, requester.user_id),
                approver=approver,
                decision=decision,
                action_hash=req["action_hash"],
                comment=f"decided from CLI ({args.approve})",
            )
            actor = approver
            print(f"== {decision} recorded in the audit log; resuming as {approver.username}\n")

        rows = await audit.list(tenant, thread_id=thread_id)
        if rows:
            print("\nAudit trail:")
            for r in rows:
                print(f"  #{r.id} {r.status:<9} {r.action_tool:<20} by {r.approver_id:<20} hash={r.row_hash[:12]}...")
            print(f"  chain verified: {(await audit.verify_chain()).ok}")
        print(f"\nTools called: {len(recorder.calls)} ({', '.join(sorted(set(recorder.tool_names)))})")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="oomkilled_loop", choices=list_scenarios(None))
    parser.add_argument("--user", default="alice", help="live scenarios: Keycloak user who starts the incident")
    parser.add_argument("--approver-user", default="bob", help="live scenarios: Keycloak user who approves")
    parser.add_argument("--provider", choices=["deepseek", "google_genai"])
    parser.add_argument("--model")
    parser.add_argument("--no-trace", action="store_true", help="disable LangSmith tracing for this run")
    parser.add_argument("--durable", action="store_true", help="use Postgres (run scripts/db_migrate.py first)")
    parser.add_argument("--thread-id", help="resume an existing thread (with --durable)")
    parser.add_argument("--approve", choices=["prompt", "auto", "reject"], default="prompt")
    parser.add_argument("--flaky", type=int, default=0, help="fail the first N calls of each read tool")
    parser.add_argument("--crash-after", help="hard-exit after this top-level node finishes (with --durable)")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(run_async(run(args)))


if __name__ == "__main__":
    main()
