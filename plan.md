# Incident Copilot (LangGraph): Implementation Plan

## Context

The repo is empty apart from `claude.md` and `.env` (DeepSeek, Google and LangSmith keys). The goal is the
portfolio project described in `claude.md`: an SRE incident copilot that shows LangGraph depth plus a
production-grade control plane (auth, user-scoped tool actions, approval, audit, tracing).

It is built **gradually**: 11 milestones, each adding one capability on top of a working system and ending in
something you can run and test yourself. I stop after each milestone; you test and commit (I never commit).

Decisions from your answers:
- **LLM:** chosen by env var through `init_chat_model`; DeepSeek default, Gemini switchable.
- **Client:** minimal one-page web UI (static HTML + vanilla JS served by FastAPI, no build step).
- **Tenancy:** lightweight: `tenant` JWT claim, two demo tenants, thread ownership table, one K8s namespace per tenant.
- **Tooling:** uv, `pyproject.toml` + `uv.lock`, Python pinned to 3.13 (same as the Docker image).

## Target layout

```
src/copilot/
  config.py            settings (pydantic-settings), model factory
  graph/               state.py, supervisor.py, triage.py, diagnosis.py, remediation.py, gate.py, build.py
  tools/               backend.py (ToolBackend protocol), mock_backend.py, mcp_backend.py, registry.py (read/write + risk)
  approval/            hashing.py, audit.py, service.py
  auth/                jwt.py, deps.py (get_current_user), rbac.py
  api/                 app.py, routes_incidents.py, routes_approvals.py, sse.py, static/index.html
  memory/              store.py (past incidents, runbooks)
  observability/       tracing.py, masking.py
scenarios/             one JSON fixture per synthetic incident (also used by evals)
mcp_servers/           observability/, k8s_read/, k8s_write/
infra/                 docker-compose.yml, keycloak/realm.json, postgres/init.sql, kind/ (cluster config, manifests)
buggy_service/         the intentionally broken microservice
tests/unit, tests/evals
scripts/               PowerShell: dev-up.ps1, kind-up.ps1, run-evals.ps1
```

Two seams keep later milestones from rewriting earlier ones:
- **`ToolBackend`**: the graph only talks to this interface. Mock backend first (M1), MCP backend later (M9).
- **`get_current_user`**: a FastAPI dependency. Returns a fixed dev user in M5, a verified JWT identity in M6.
  Identity reaches tools through LangGraph runtime context, never through LLM-controlled arguments.

## Milestones

**Progress (2026-11-21):** M0–M8 done and verified (85 deterministic tests; live runs against DeepSeek,
Keycloak and Postgres; eval suite 18/18). Changes from the original plan:
- Model: `deepseek-flash` via `init_chat_model`. Tool loops run with thinking on; structured outputs run
  with thinking off, because DeepSeek's thinking mode rejects a forced `tool_choice`.
- Write actions are a typed `ActionSpec` union filled in by the remediation agent, not free-form tools.
- The supervisor routes in code over the agents' structured outputs (LLMs decide facts, code decides flow).
- Added a four-eyes rule (no approving your own incident) and `POST /incidents/{id}/resume` for runs
  stopped by a crash or restart.
- Tenant comes from a Keycloak group attribute (`tenant` claim), with the group path as fallback.

**Progress (2026-11-21, later):** M9 and M10 done and verified live on kind (two incidents resolved end to end,
K8s audit log shows SA + impersonated human, one Jaeger trace from approval to the Kubernetes PATCH). Changes:
- MCP servers verify the user's token themselves (FastMCP `token_verifier`, audience `copilot-mcp`) and
  impersonate `kc:<user>` with `copilot:tenant:<t>` / `copilot:approvers:<t>` groups. Separate SAs: the read server
  cannot impersonate approver groups.
- The observability server builds PromQL from a fixed catalog and enforces tenant → namespace itself, because
  Prometheus has no per-tenant authorization. kube-state-metrics added for restart counts.
- `query_metrics` takes a namespace. Verification polls until healthy and stable (45 s, no new restarts) on the
  live cluster, so a restart that only hides an OOM for a few seconds does not count as a fix.
- Tracing: own LangChain callback handler for node/LLM/tool spans, redacting exporter, LangSmith anonymizer;
  audit rows carry trace ids. The demo recording itself is left to you ([docs/demo.md](docs/demo.md)).

### M0. Scaffold
`pyproject.toml`, `.gitignore` (**must include `.env` before your first commit**), `.env.example`, settings,
compose file with Postgres only, ruff + pytest.
**Test:** `uv sync`, `uv run pytest`, `docker compose up -d postgres`.

### M1. Basic agent with mocked tools
One ReAct-style `StateGraph` (model node + `ToolNode`) with read-only mock tools: `query_metrics`,
`get_pod_logs`, `list_pods`, `describe_pod`, `get_events`. Responses come from a scenario fixture
(start with `oomkilled_loop.json`). In-memory, run from `scripts/run_incident.py`, console streaming.
**Test:** run the OOM scenario; the agent names the root cause.

### M2. Supervisor and subgraphs
Supervisor routes with `Command(goto=...)` to triage, diagnosis and remediation subgraphs. Typed state
(alert, findings, hypotheses, proposed_action). Diagnosis fans out hypotheses in parallel with `Send` and
merges through a reducer. Remediation only *proposes* an action (structured output); nothing is executed yet.
**Test:** stream with `subgraphs=True`, see routing and parallel hypothesis branches.

### M3. Durability
`AsyncPostgresSaver` checkpointer, `RetryPolicy` on tool nodes, fault injection in the mock backend
(a tool fails N times then succeeds). State history / time travel script: list checkpoints, replay or fork from one.
**Test:** kill the process mid-run, restart with the same `thread_id`, it continues; replay an incident from an earlier checkpoint.

### M4. Approval gate and audit
- Write tools (`restart_deployment`, `scale_deployment`, `patch_resources`, `set_env`, `rollback`) in a separate
  registry with a code-driven risk classification.
- **One gate node** in front of all write tools. Risky action: `interrupt()` with payload + `action_hash`
  (SHA-256 of canonical JSON of tool, args, thread, tenant). Resume must carry the same hash.
- Separate executor node: re-checks that a matching `approved` audit row exists, and skips if an `executed`
  row already exists (safe when a node re-runs after resume).
- `audit_log` table: append-only (app role has UPDATE/DELETE revoked), hash-chained, inserts serialized with an
  advisory lock. Rows: `approved` before resume, then `executed`/`failed`. A `verify_chain` script.
- Mock write tools change the scenario state so the agent can verify the fix afterwards.
**Test:** deterministic unit tests: hash mismatch rejected, no approval row means no execution, replayed resume
does not execute twice, tampered chain detected, UPDATE on audit denied by Postgres.

### M5. FastAPI and web UI (dev identity)
Endpoints: `POST /incidents`, `GET /incidents/{id}/stream` (SSE: updates, messages, custom),
`GET /incidents/{id}`, `GET /incidents/{id}/history`, `POST /incidents/{id}/approvals`, `GET /audit`.
`threads` table (thread_id, owner, tenant). One-page UI: start a scenario, live timeline, approval card with
Approve/Reject, audit view. The API joins docker-compose.
**Test:** full flow in the browser with the fixed dev user.

### M6. Keycloak, JWT, RBAC, tenancy
- Keycloak in compose with realm import: public PKCE client for the UI, `sre-approver` role, `tenant` claim
  mapper, users: alice (acme, no approver role), bob (acme, approver), carol (globex, approver).
- `get_current_user` now verifies the JWT (JWKS, issuer, audience, expiry).
- Resume endpoint checks, in plain Python before touching LangGraph: role, thread tenant/ownership,
  action hash, idempotency (unique constraint). Then writes the `approved` row, then resumes.
- UI logs in with keycloak-js; SSE read through `fetch` streaming (EventSource cannot send an Authorization header).
**Test:** alice gets 403 on approve; carol gets 404/403 on acme's thread; bob approves; double-click approve executes once. All covered by unit tests too.

### M7. Long-term memory
`AsyncPostgresStore`: resolved incidents and runbooks, namespaced by tenant. Triage looks up similar past
incidents; a final node writes the resolution back.
**Test:** run the same scenario twice; the second run cites the first. Tenant B cannot see tenant A's memory.

### M8. Eval suite
6 scenarios: OOMKilled loop, high 5xx/latency, unreachable DB, bad env var, bad image tag, plus one
"no action needed" false alarm. Real LLM, mocked tools. pytest runs each scenario N times and reports a pass
rate against a threshold. Checks: root-cause category, proposed action, trajectory (required tools called,
no write tool before the gate, step budget). GitHub Actions workflow: unit tests on every push, evals with an
API key secret. Can run against both DeepSeek and Gemini for a measured comparison.
**Test:** `scripts/run-evals.ps1` prints the pass-rate table.

### M9. kind, buggy service, Prometheus, MCP with impersonation
- `scripts/kind-up.ps1`: kind cluster (with API-server audit policy enabled), namespaces per tenant, buggy
  FastAPI service with env-toggled faults, minimal Prometheus from plain manifests.
- Three MCP servers (FastMCP, streamable HTTP), running in the cluster and exposed by NodePort:
  `observability` (read), `k8s-read`, `k8s-write`. The agent forwards the user's JWT; the MCP server
  verifies it itself and sets `Impersonate-User`/`Impersonate-Group`. Its ServiceAccount has only `impersonate`,
  limited by `resourceNames`. RBAC bindings give approver groups write access in their tenant namespace only.
- `mcp_backend.py` implements `ToolBackend` with `langchain-mcp-adapters`; a setting switches mock/MCP.
**Test:** trigger a real OOM loop, approve the fix in the UI, the pod recovers; the K8s audit log shows both the
SA and the human; a user from the other tenant is denied by K8s itself.

### M10. Tracing, README, demo
OpenTelemetry (FastAPI + graph spans) exported to Jaeger in compose, spans tagged with user/tenant, `trace_id`
stored in audit rows. LangSmith already enabled through `.env`; add input/output masking and an OTel
redaction processor for PII. README with architecture diagram and only measured numbers; demo script.
**Test:** one incident visible end to end in Jaeger and LangSmith, with emails/tokens masked.

## Known risks to handle while building
- **psycopg async on Windows** needs a selector event loop: local scripts use
  `asyncio.run(..., loop_factory=asyncio.SelectorEventLoop)`; the API itself runs in Docker (Linux).
- **Keycloak issuer mismatch** between `localhost` (browser) and the compose hostname (API): fetch JWKS from the
  internal URL, validate the issuer against the public URL.
- **Per-request headers in langchain-mcp-adapters**: check the current API when M9 starts.
- LangGraph and MCP APIs move fast: I check current docs at the start of each milestone rather than trust memory.
- Prerequisites for you to install: uv now, kind before M9.

## Verification
Each milestone has its own test above. Overall: `uv run pytest tests/unit` (deterministic, includes all gate,
RBAC and audit tests), `scripts/run-evals.ps1` (pass rates), then the manual end-to-end demo from M9.
