"""FastAPI application: authenticated incident runs, role-gated approvals, audit, live events."""

# No `from __future__ import annotations`: FastAPI must resolve the local Annotated aliases (Svc, Me).

import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from copilot.api.runs import RunManager
from copilot.approval.audit import AuditStore
from copilot.approval.service import ApprovalService, get_pending_approval
from copilot.auth.jwt import JWTVerifier, actor_from_claims
from copilot.auth.rbac import ensure_can_operate, ensure_can_start, ensure_can_view
from copilot.config import Settings, get_settings
from copilot.context import Actor, CopilotContext
from copilot.errors import Conflict, CopilotError, NotFound, Unauthorized
from copilot.graph.supervisor import summarize_for_api
from copilot.observability.tracing import current_trace_id, setup_tracing
from copilot.scenarios import list_scenarios, load_scenario
from copilot.threads import ThreadRecord, ThreadRepository
from copilot.tools.mock_backend import ScenarioBackend
from copilot.tools.write_tools import RiskPolicy

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class Services:
    settings: Settings
    graph: Any
    audit: AuditStore
    threads: ThreadRepository
    verifier: JWTVerifier | None
    risk_policy: RiskPolicy
    backend_factory: Callable[[ThreadRecord], Any] = lambda t: ScenarioBackend(load_scenario(t.scenario_id))
    _backends: dict[str, Any] = field(default_factory=dict)
    runs: RunManager = field(init=False)
    approvals: ApprovalService = field(init=False)

    def __post_init__(self) -> None:
        self.approvals = ApprovalService(self.graph, self.audit, self.settings)
        self.runs = RunManager(self.graph, self.threads, self._context_for, self.settings.recursion_limit)
        self._records: dict[str, ThreadRecord] = {}

    def remember(self, record: ThreadRecord) -> None:
        self._records[record.thread_id] = record

    @property
    def scenario_mode(self) -> str:
        return "live" if self.settings.tool_backend == "mcp" else "mock"

    def _context_for(self, thread_id: str, actor: Actor) -> CopilotContext:
        record = self._records[thread_id]
        if self.settings.tool_backend == "mcp":
            # Per run and per human: the MCP servers act as whoever this run belongs to
            # (the requester while investigating, the approver when executing).
            from copilot.tools.mcp_backend import McpBackend

            backend = McpBackend(self.settings, actor)
        else:
            if thread_id not in self._backends:
                self._backends[thread_id] = self.backend_factory(record)
            backend = self._backends[thread_id]
        return CopilotContext(backend=backend, actor=actor, audit=self.audit, risk_policy=self.risk_policy)


ServicesFactory = Callable[[Settings], AbstractAsyncContextManager[Services]]


@asynccontextmanager
async def postgres_services(settings: Settings) -> AsyncIterator[Services]:
    from copilot.config import make_models
    from copilot.db import persistence
    from copilot.graph.build import build_graph

    async with persistence(settings) as p:
        graph = build_graph(make_models(settings), settings=settings, checkpointer=p.checkpointer, store=p.store)
        yield Services(
            settings=settings,
            graph=graph,
            audit=p.audit,
            threads=p.threads,
            verifier=JWTVerifier.from_settings(settings) if settings.auth_mode == "keycloak" else None,
            risk_policy=RiskPolicy.from_settings(settings.risk_low_tools, settings.risk_protected_namespaces),
        )


# --------------------------------------------------------------------------------------------
# Request / response models
# --------------------------------------------------------------------------------------------


class StartIncident(BaseModel):
    scenario_id: str


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]
    action_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    comment: str | None = Field(default=None, max_length=1000)


# --------------------------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------------------------


def create_app(settings: Settings | None = None, services_factory: ServicesFactory | None = None) -> FastAPI:
    settings = settings or get_settings()
    factory = services_factory or postgres_services

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with factory(settings) as services:
            app.state.services = services
            # Runs that were in flight when the API stopped are resumable, not lost.
            for stale in await services.threads.list_by_status("running"):
                await services.threads.set_status(stale.thread_id, "interrupted")
            try:
                yield
            finally:
                await services.runs.shutdown()

    app = FastAPI(title="Incident Copilot API", version="0.1.0", lifespan=lifespan)
    if setup_tracing(settings.otel_endpoint, settings.otel_service_name):
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        # No per-message ASGI receive/send spans: an SSE stream would otherwise produce one per event.
        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz", exclude_spans=["receive", "send"])

    @app.exception_handler(CopilotError)
    async def copilot_error(_request: Request, exc: CopilotError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status_code, headers=headers)

    def services(request: Request) -> Services:
        return request.app.state.services

    async def current_actor(request: Request, svc: Annotated[Services, Depends(services)]) -> Actor:
        if settings.auth_mode == "dev":
            return Actor(
                settings.dev_user_id,
                settings.dev_username,
                settings.dev_tenant,
                frozenset(r.strip() for r in settings.dev_roles.split(",") if r.strip()),
            )
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise Unauthorized("missing bearer token")
        assert svc.verifier is not None
        claims = await svc.verifier.verify(token)
        return actor_from_claims(claims, token)

    Svc = Annotated[Services, Depends(services)]
    Me = Annotated[Actor, Depends(current_actor)]

    async def load_thread(svc: Services, thread_id: str) -> ThreadRecord | None:
        record = await svc.threads.get(thread_id)
        if record is not None:
            svc.remember(record)
        return record

    # --- public ---------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    async def ui_config() -> dict[str, Any]:
        return {
            "auth_mode": settings.auth_mode,
            "issuer": settings.keycloak_issuer,
            "client_id": settings.keycloak_ui_client_id,
            "approver_role": settings.approver_role,
            "tool_backend": settings.tool_backend,
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # --- identity & catalog ---------------------------------------------------------------

    @app.get("/api/me")
    async def me(actor: Me) -> dict[str, Any]:
        return {
            "user_id": actor.user_id,
            "username": actor.username,
            "tenant_id": actor.tenant_id,
            "roles": sorted(actor.roles),
            "can_approve": actor.has_role(settings.approver_role),
        }

    @app.get("/api/scenarios")
    async def scenarios(actor: Me, svc: Svc) -> list[dict[str, Any]]:
        out = []
        for sid in list_scenarios(svc.scenario_mode):
            s = load_scenario(sid)
            out.append({"id": s.id, "title": s.title, "alert": s.alert.model_dump()})
        return out

    # --- incidents ------------------------------------------------------------------------

    @app.post("/api/incidents", status_code=201)
    async def start_incident(body: StartIncident, actor: Me, svc: Svc) -> dict[str, Any]:
        ensure_can_start(actor, settings)
        if body.scenario_id not in list_scenarios(svc.scenario_mode):
            raise NotFound(f"unknown scenario '{body.scenario_id}'")
        scenario = load_scenario(body.scenario_id)
        thread_id = f"inc-{uuid.uuid4().hex[:12]}"
        record = await svc.threads.create(
            ThreadRecord(
                thread_id=thread_id,
                tenant_id=actor.tenant_id,
                owner_id=actor.user_id,
                owner_username=actor.username,
                scenario_id=scenario.id,
                title=scenario.title,
                status="running",
            )
        )
        svc.remember(record)
        # Tenant and requester come from the verified token, never from the request body.
        alert = scenario.alert.model_dump()
        if alert.get("started_at") == "now":
            alert["started_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        inputs = {"alert": alert, "tenant_id": actor.tenant_id, "requested_by": actor.user_id}
        svc.runs.start(thread_id, inputs, actor, reason="incident started")
        return {"thread_id": thread_id, "thread": record.to_dict()}

    @app.get("/api/incidents")
    async def list_incidents(actor: Me, svc: Svc) -> list[dict[str, Any]]:
        return [r.to_dict() for r in await svc.threads.list_for_tenant(actor.tenant_id)]

    @app.get("/api/incidents/{thread_id}")
    async def get_incident(thread_id: str, actor: Me, svc: Svc) -> dict[str, Any]:
        record = await load_thread(svc, thread_id)
        ensure_can_view(actor, record.info if record else None)
        assert record is not None
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = await svc.graph.aget_state(config)
        pending = await get_pending_approval(svc.graph, thread_id)
        return {
            "thread": record.to_dict(),
            "running": svc.runs.is_running(thread_id),
            "state": summarize_for_api(snapshot.values),
            "next": list(snapshot.next),
            "pending_approval": pending.payload if pending else None,
        }

    @app.get("/api/incidents/{thread_id}/events")
    async def incident_events(thread_id: str, actor: Me, svc: Svc, after: int = 0) -> StreamingResponse:
        record = await load_thread(svc, thread_id)
        ensure_can_view(actor, record.info if record else None)

        async def sse() -> AsyncIterator[str]:
            async for event in svc.runs.subscribe(thread_id, after):
                yield f"id: {event['seq']}\ndata: {json.dumps(event, default=str)}\n\n"
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(
            sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    @app.get("/api/incidents/{thread_id}/history")
    async def incident_history(thread_id: str, actor: Me, svc: Svc) -> list[dict[str, Any]]:
        record = await load_thread(svc, thread_id)
        ensure_can_view(actor, record.info if record else None)
        history = []
        async for snap in svc.graph.aget_state_history({"configurable": {"thread_id": thread_id}}):
            history.append(
                {
                    "checkpoint_id": snap.config["configurable"]["checkpoint_id"],
                    "step": snap.metadata.get("step") if snap.metadata else None,
                    "source": snap.metadata.get("source") if snap.metadata else None,
                    "next": list(snap.next),
                    "created_at": snap.created_at,
                    "has_interrupt": bool(snap.interrupts),
                }
            )
        return history

    @app.post("/api/incidents/{thread_id}/approvals", status_code=202)
    async def decide(thread_id: str, body: ApprovalDecision, actor: Me, svc: Svc) -> dict[str, Any]:
        # All checks (tenant, role, four-eyes, hash, idempotency) happen in ApprovalService
        # before the graph is touched; only then is the resume started.
        record = await load_thread(svc, thread_id)
        ensure_can_view(actor, record.info if record else None)  # 404 before anything else leaks
        if svc.runs.is_running(thread_id):
            raise Conflict("the incident is still running")
        command = await svc.approvals.decide(
            thread=record.info if record else None,
            approver=actor,
            decision=body.decision,
            action_hash=body.action_hash,
            comment=body.comment,
            trace_id=current_trace_id(),
        )
        svc.runs.start(thread_id, command, actor, reason=f"{body.decision} by {actor.username}")
        return {"status": "accepted", "decision": body.decision}

    @app.post("/api/incidents/{thread_id}/resume", status_code=202)
    async def resume(thread_id: str, actor: Me, svc: Svc) -> dict[str, Any]:
        """Continue a run that stopped mid-way (API restart or crash) from its last checkpoint."""
        record = await load_thread(svc, thread_id)
        ensure_can_operate(actor, record.info if record else None, settings)
        snapshot = await svc.graph.aget_state({"configurable": {"thread_id": thread_id}})
        if snapshot.interrupts:
            raise Conflict("the incident is waiting for an approval decision")
        if not snapshot.next:
            raise Conflict("the incident has already finished")
        svc.runs.start(thread_id, None, actor, reason=f"resumed by {actor.username}")
        return {"status": "accepted", "next": list(snapshot.next)}

    @app.get("/api/audit")
    async def audit(actor: Me, svc: Svc, thread_id: str | None = None) -> dict[str, Any]:
        if thread_id:
            record = await load_thread(svc, thread_id)
            ensure_can_view(actor, record.info if record else None)
        rows = await svc.audit.list(actor.tenant_id, thread_id=thread_id)
        chain = await svc.audit.verify_chain()
        return {
            "rows": [r.to_dict() for r in rows],
            "chain": {"ok": chain.ok, "rows_checked": chain.rows_checked, "first_bad_id": chain.first_bad_id},
        }

    return app
