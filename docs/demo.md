# Demo script (about 6 minutes)

A walkthrough for recording. It shows a real incident on a kind cluster: investigated by an agent acting as one
human, fixed after a second human approves, and traceable in Jaeger and the Kubernetes audit log.

## Before recording

```powershell
docker compose -f infra/docker-compose.yml up -d --wait postgres keycloak jaeger
.\scripts\kind_up.ps1                         # first time; later: .\scripts\kind_up.ps1 -Reset for fresh bugs
$env:COPILOT_TOOL_BACKEND = "mcp"
docker compose -f infra/docker-compose.yml up -d --build --wait api
kubectl --context kind-incident-copilot -n acme-prod get pods   # checkout-api should be OOMKilled / CrashLoopBackOff
```

Windows to have open: two browser windows (one private) on http://localhost:8000, Jaeger at
http://localhost:16686, and a terminal.

## 1. The incident is real (30 s)

Terminal:

```powershell
kubectl --context kind-incident-copilot -n acme-prod get pods
kubectl --context kind-incident-copilot -n acme-prod rollout history deployment/checkout-api
```

Say: release 2.14.0 added a cache that outgrows the 128Mi limit, so pods are OOMKilled in a loop.

## 2. alice investigates (2 min)

Window 1: log in as **alice / alice** (tenant acme, role `sre`). Pick **[live] checkout-api restarting**, start it.

Point at the live timeline:
- supervisor routing (triage, then diagnosis)
- three hypotheses investigated **in parallel** (`Send`), each with its own tool calls
- tool calls hit the cluster through MCP servers **as alice** (impersonation), reading pods, previous logs,
  rollout history and Prometheus memory metrics
- diagnosis: OOM, high confidence; remediation proposes a rollback to revision 1 (or a memory patch)

The run pauses: **Approval required**, showing the action, risk reasons (protected namespace) and action hash.
alice's Approve button is disabled. Say that the server enforces this too (403), and that she couldn't approve
her own incident even with the role (four-eyes rule).

## 3. bob approves (1 min)

Window 2 (private): log in as **bob / bob** (`sre-approver`). Open the same incident and approve with a comment.

Timeline: executor runs the action **as bob** through the write MCP server, then verification polls the cluster
until both pods are Running and ready with no new restarts for 45 s. The report streams in. Status: resolved.

Terminal:

```powershell
kubectl --context kind-incident-copilot -n acme-prod get pods
kubectl --context kind-incident-copilot -n acme-prod rollout history deployment/checkout-api   # change cause names kc:bob
```

## 4. Who did what (1.5 min)

UI, **Audit log** tab: `approved` then `executed`, both by bob, hash chain verified.

Terminal: Kubernetes' own audit log records the service account *and* the human:

```powershell
uv run python scripts/k8s_audit.py --writes -n 3
```

Jaeger: copy the `trace_id` idea from the audit row, or search service `incident-copilot-api`, operation
`incident.run`. Expand one trace: approval request → resume run → `graph.node execute_action` →
`tools/call rollback_deployment` → (MCP HTTP requests) → `mcp.tool rollback_deployment` on service `mcp-k8s-write`
(tagged `k8s.impersonate.user=kc:bob`) → `PATCH` to the Kubernetes API.
The investigation trace shows `llm deepseek-flash` spans with token counts.

## 5. Tenancy (30 s)

Log out, log in as **carol / carol** (tenant globex, approver). The acme incident is not in her list, and opening its
URL returns 404. Terminal:

```powershell
uv run python scripts/k8s_audit.py --denied -n 3   # e.g. kc:carol refused on acme-prod (if probed)
```

Close: the LLM chose the fix; deterministic code decided who could approve it, and Kubernetes enforced who could
apply it.

## Reset for another take

```powershell
.\scripts\kind_up.ps1 -Reset
```
