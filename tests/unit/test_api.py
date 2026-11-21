"""API tests: JWT verification, RBAC, tenancy, approval flow and SSE, over real HTTP (in-process)."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from copilot.api.app import Services, create_app
from copilot.approval.audit import InMemoryAuditStore
from copilot.auth.jwt import JWTVerifier
from copilot.config import Models
from copilot.graph.build import build_graph
from copilot.threads import InMemoryThreadRepository
from copilot.tools.write_tools import RiskPolicy
from tests.fakes import FakeModel, oom_responder
from tests.helpers import make_settings

ISSUER = "http://keycloak.test/realms/incident-copilot"
AUDIENCE = "copilot-api"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(sub: str, tenant: str | None, roles: list[str], *, key=KEY, **overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": sub,
        "preferred_username": sub.removeprefix("u-"),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "realm_access": {"roles": roles},
    }
    if tenant:
        claims["tenant"] = tenant
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256")


ALICE = token("u-alice", "acme", ["sre"])
BOB = token("u-bob", "acme", ["sre", "sre-approver"])
CAROL = token("u-carol", "globex", ["sre", "sre-approver"])


def auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
async def client():
    settings = make_settings(auth_mode="keycloak", keycloak_issuer=ISSUER, jwt_audience=AUDIENCE)

    @asynccontextmanager
    async def memory_services(s):
        model = FakeModel(oom_responder())
        graph = build_graph(Models(model, model), settings=s, checkpointer=InMemorySaver(), store=InMemoryStore())
        yield Services(
            settings=s,
            graph=graph,
            audit=InMemoryAuditStore(),
            threads=InMemoryThreadRepository(),
            verifier=JWTVerifier(issuer=ISSUER, audience=AUDIENCE, key=KEY.public_key()),
            risk_policy=RiskPolicy(),
        )

    app = create_app(settings, memory_services)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.app = app  # type: ignore[attr-defined]
            yield c


async def wait_idle(client, thread_id: str, limit_s: float = 10) -> None:
    runs = client.app.state.services.runs
    deadline = asyncio.get_running_loop().time() + limit_s
    while runs.is_running(thread_id):
        assert asyncio.get_running_loop().time() < deadline, "run did not finish"
        await asyncio.sleep(0.02)


async def start(client) -> str:
    r = await client.post("/api/incidents", json={"scenario_id": "oomkilled_loop"}, headers=auth(ALICE))
    assert r.status_code == 201, r.text
    thread_id = r.json()["thread_id"]
    await wait_idle(client, thread_id)
    return thread_id


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "not-a-jwt",
        token("u-x", "acme", ["sre"], exp=int(time.time()) - 3600),  # expired
        token("u-x", "acme", ["sre"], aud="another-api"),  # wrong audience
        token("u-x", "acme", ["sre"], iss="http://evil/realms/x"),  # wrong issuer
        token("u-x", "acme", ["sre"], key=OTHER_KEY),  # wrong signature
        token("u-x", None, ["sre"]),  # no tenant
    ],
)
async def test_rejects_bad_tokens(client, bad):
    headers = auth(bad) if bad else {}
    r = await client.get("/api/me", headers=headers)
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


async def test_me_reflects_verified_claims(client):
    r = await client.get("/api/me", headers=auth(BOB))
    assert r.json() == {
        "user_id": "u-bob",
        "username": "bob",
        "tenant_id": "acme",
        "roles": ["sre", "sre-approver"],
        "can_approve": True,
    }


async def test_tenant_from_group_path_fallback(client):
    tok = token("u-dave", None, ["sre"], groups=["/tenants/globex"])
    assert (await client.get("/api/me", headers=auth(tok))).json()["tenant_id"] == "globex"


async def test_user_without_role_cannot_start(client):
    tok = token("u-viewer", "acme", [])
    r = await client.post("/api/incidents", json={"scenario_id": "oomkilled_loop"}, headers=auth(tok))
    assert r.status_code == 403


async def test_full_approval_flow_with_rbac(client):
    thread_id = await start(client)

    inc = (await client.get(f"/api/incidents/{thread_id}", headers=auth(ALICE))).json()
    assert inc["thread"]["status"] == "awaiting_approval"
    pending = inc["pending_approval"]
    digest = pending["action_hash"]
    body = {"decision": "approve", "action_hash": digest}

    # Other tenant: indistinguishable from a missing incident, everywhere.
    for method, path in [
        ("GET", f"/api/incidents/{thread_id}"),
        ("GET", f"/api/incidents/{thread_id}/history"),
        ("GET", f"/api/audit?thread_id={thread_id}"),
    ]:
        assert (await client.request(method, path, headers=auth(CAROL))).status_code == 404
    assert (
        await client.post(f"/api/incidents/{thread_id}/approvals", json=body, headers=auth(CAROL))
    ).status_code == 404
    assert thread_id not in [t["thread_id"] for t in (await client.get("/api/incidents", headers=auth(CAROL))).json()]

    # Same tenant, no approver role (and owner).
    r = await client.post(f"/api/incidents/{thread_id}/approvals", json=body, headers=auth(ALICE))
    assert r.status_code == 403

    # Approver with a stale/tampered hash.
    r = await client.post(
        f"/api/incidents/{thread_id}/approvals",
        headers=auth(BOB),
        json={"decision": "approve", "action_hash": "a" * 64},
    )
    assert r.status_code == 409

    # Approver, correct hash.
    r = await client.post(f"/api/incidents/{thread_id}/approvals", json=body, headers=auth(BOB))
    assert r.status_code == 202, r.text
    await wait_idle(client, thread_id)

    inc = (await client.get(f"/api/incidents/{thread_id}", headers=auth(ALICE))).json()
    assert inc["thread"]["status"] == "completed"
    assert inc["state"]["status"] == "resolved"
    assert inc["state"]["execution"]["executed_as"] == "bob"
    assert inc["pending_approval"] is None

    # Double submit after the fact.
    r = await client.post(f"/api/incidents/{thread_id}/approvals", json=body, headers=auth(BOB))
    assert r.status_code == 409

    audit = (await client.get(f"/api/audit?thread_id={thread_id}", headers=auth(ALICE))).json()
    assert [row["status"] for row in audit["rows"]] == ["approved", "executed"]
    assert audit["rows"][0]["approver_id"] == "u-bob"
    assert audit["chain"]["ok"] is True
    assert (await client.get("/api/audit", headers=auth(CAROL))).json()["rows"] == []


async def test_sse_stream_replays_run_events(client):
    thread_id = await start(client)
    events = []
    async with client.stream("GET", f"/api/incidents/{thread_id}/events?after=0", headers=auth(ALICE)) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        async for line in r.aiter_lines():
            if line.startswith("data: ") and line != "data: {}":
                events.append(json.loads(line[6:]))
    types = [e["type"] for e in events]
    assert types[0] == "run_started"
    assert "route" in types and "tool_result" in types and "hypotheses" in types
    assert "approval_required" in types
    assert types[-1] == "run_finished"
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)

    later = []
    async with client.stream(
        "GET", f"/api/incidents/{thread_id}/events?after={events[-2]['seq']}", headers=auth(ALICE)
    ) as r:
        async for line in r.aiter_lines():
            if line.startswith("data: ") and line != "data: {}":
                later.append(json.loads(line[6:]))
    assert [e["type"] for e in later] == ["run_finished"]


async def test_history_lists_checkpoints(client):
    thread_id = await start(client)
    history = (await client.get(f"/api/incidents/{thread_id}/history", headers=auth(ALICE))).json()
    assert len(history) > 5
    assert history[0]["has_interrupt"] is True


async def test_dev_mode_identity():
    settings = make_settings(auth_mode="dev", dev_user_id="u-dev", dev_tenant="acme", dev_roles="sre")

    @asynccontextmanager
    async def svc(s):
        model = FakeModel(oom_responder())
        graph = build_graph(Models(model, model), settings=s, checkpointer=InMemorySaver())
        yield Services(
            settings=s,
            graph=graph,
            audit=InMemoryAuditStore(),
            threads=InMemoryThreadRepository(),
            verifier=None,
            risk_policy=RiskPolicy(),
        )

    app = create_app(settings, svc)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        me = (await c.get("/api/me")).json()
        assert me["user_id"] == "u-dev" and me["can_approve"] is False
        assert (await c.get("/api/config")).json()["auth_mode"] == "dev"
        assert (await c.get("/")).status_code == 200
