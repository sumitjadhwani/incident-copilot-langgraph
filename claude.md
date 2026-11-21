# Incident Copilot (LangGraph): Project Context

## Goal
A resume portfolio project for an AI Engineer (5+ yrs backend, 3 yrs GenAI: RAG, evals, LiteLLM, MCP, OTel).
It should show hands-on LangGraph depth **and** production capability: authenticated agents,
user-scoped tool actions, tracing, and audit. The focus is the agent control plane, NOT DevOps.

## What it does
An SRE incident-response copilot. A supervisor routes to triage, diagnosis and remediation subgraphs.
They investigate an alert through Prometheus, pod logs and K8s tools, then propose a fix.
Risky actions pause for approval by a human with the `sre-approver` role.
Inspired by Elastic's LangGraph SecOps use, Open Deep Research (supervisor and subgraphs),
Executive AI Assistant / Open SWE (approval flows), and Auth0 Token Vault (user-scoped tool credentials).

## LangGraph features to use
StateGraph, subgraphs, supervisor routing via `Command`, `Send` for parallel hypotheses,
`interrupt()` + `Command(resume=...)`, PostgresSaver checkpointer (durable resume after a crash),
time travel / state history for incident replay, `RetryPolicy` on flaky tools,
streaming (updates/messages/custom), `Store` for long-term memory (past incidents/runbooks).

## Architecture decisions (agreed)
- **Auth (user → agent):** Keycloak (dev mode, realm import JSON) issues JWTs; FastAPI verifies them.
- **RBAC lives in code, never in the LLM:** the resume endpoint checks the `sre-approver` clam, thread/tenant
  ownership and idempotency in deterministic Python *before* calling LangGraph.
- **Approval is tied to the exact action:** the interrupt payload includes a hash of the action; resume must match it.
  The tool-execution node checks again that a matching approval row exists (defense in depth).
- **Approval gate is structural:** every write tool goes through one gate node; risky classification is config/code-driven.
- **Auth (agent → K8s):** use K8s **impersonation headers** (Impersonate-User/Group) taken from verified JWT claims,
  NOT TokenRequest (that only mints ServiceAccount tokens). The agent's SA gets `impersonate` limited by `resourceNames`.
  K8s audit logs then record both the SA and the human.
- **MCP tool servers:** Prometheus, logs and K8s tools sit behind MCP servers (streamable HTTP). Forward the user's identity
  to the MCP server, which does the impersonation. Never let an MCP server act with its own admin creds.
  Read-only and write tools are separate.
- **Audit:** a separate append-only Postgres table (app role gets REVOKE UPDATE/DELETE), hash-chained rows
  containing approver_id, thread_id, checkpoint_id, action_payload + hash, trace_id, status
  (write `approved` before resume, then `executed`/`failed` after).
- **Tracing:** LangSmith + OpenTelemetry export (Jaeger/Tempo); tag spans with user/tenant; mask PII before export.
- **Infra:** docker-compose for Postgres, Keycloak and the agent API. A kind cluster (via a setup script/Makefile, not
  compose) runs one intentionally buggy microservice and a minimal Prometheus (plain manifests, no Helm).
  Logs come from the K8s API, no Loki. Host is Windows → Docker Desktop + WSL2.
- **Evals:** 5–6 synthetic incidents (OOMKilled loop, high 5xx latency, unreachable DB, bad env var, ...).
  CI runs them against recorded/mocked tool responses with pytest; each scenario runs N times and reports a pass rate;
  trajectory checks. The approval gate gets deterministic unit tests, not LLM evals. Live-kind runs are optional/nightly.

## Build order
1. Graph with **mocked tools** + Postgres checkpointer
2. `interrupt()` approval gate + audit table
3. Keycloak + JWT verification + role-gated resume endpoint (FastAPI)
4. Eval suite on mocked/recorded tool responses
5. kind cluster + buggy service + Prometheus, wired in through MCP servers with impersonation
6. Tracing (LangSmith + OTel), README with architecture diagram, demo recording

## Working agreements
- **Never commit.** The user commits after testing locally end to end. Make the changes, then stop.
- No made-up metrics in the README or resume bullets. Only measured numbers.
- Python, FastAPI, LangGraph (latest), LangChain MCP adapters.
