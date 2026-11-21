"""MCP server: Kubernetes write actions, executed as the approving human (impersonation).

This server does not decide whether an action is allowed. Kubernetes RBAC does: only members of
`copilot:approvers:<tenant>` may patch deployments in that tenant's namespaces, and only this server's
ServiceAccount may impersonate that group. The agent's approval gate runs before any call reaches here.
Every change is annotated with a change cause naming the human, so it shows up in rollout history.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from mcp_servers.common import Impersonation, K8sClient, build_server, check_name, current_identity, fail
from mcp_servers.k8s_format import CHANGE_CAUSE, REVISION, selector_string
from mcp_servers.telemetry import tool_span

mcp = build_server(
    "k8s-write",
    "Kubernetes write actions (restart, scale, resources, env, rollback). Calls run as the authenticated approver.",
)
k8s = K8sClient()

STRATEGIC = "application/strategic-merge-patch+json"
MERGE = "application/merge-patch+json"
JSON_PATCH = "application/json-patch+json"


def _path(namespace: str, deployment: str) -> str:
    return f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}"


def _cause(tool: str, detail: str, who: Impersonation) -> dict[str, Any]:
    return {"metadata": {"annotations": {CHANGE_CAUSE: f"copilot {tool}: {detail} (approved, as {who.user})"}}}


def _result(tool: str, namespace: str, deployment: str, who: Impersonation, summary: str) -> dict[str, Any]:
    return {"tool": tool, "target": f"{namespace}/deployment/{deployment}", "as_user": who.user, "summary": summary}


def _check(namespace: str, deployment: str, container: str | None = None) -> Impersonation:
    check_name("namespace", namespace)
    check_name("deployment", deployment)
    check_name("container", container)
    return current_identity(write=True)


@mcp.tool()
async def restart_deployment(namespace: str, deployment: str) -> dict[str, Any]:
    """Rolling restart (like kubectl rollout restart)."""
    who = _check(namespace, deployment)
    with tool_span("restart_deployment", who, namespace):
        now = datetime.now(UTC).isoformat()
        body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
        body.update(_cause("restart_deployment", "rolling restart", who))
        await k8s.request("PATCH", _path(namespace, deployment), who, body=body, content_type=STRATEGIC)
        return _result("restart_deployment", namespace, deployment, who, "rolling restart triggered")


@mcp.tool()
async def scale_deployment(namespace: str, deployment: str, replicas: int) -> dict[str, Any]:
    """Set the replica count."""
    who = _check(namespace, deployment)
    if not 0 <= int(replicas) <= 20:
        fail("invalid", "replicas must be between 0 and 20")
    with tool_span("scale_deployment", who, namespace):
        body = {"spec": {"replicas": int(replicas)}, **_cause("scale_deployment", f"replicas={replicas}", who)}
        await k8s.request("PATCH", _path(namespace, deployment), who, body=body, content_type=MERGE)
        return _result("scale_deployment", namespace, deployment, who, f"scaled to {replicas} replicas")


@mcp.tool()
async def patch_resources(
    namespace: str,
    deployment: str,
    container: str,
    memory_limit: str | None = None,
    memory_request: str | None = None,
    cpu_limit: str | None = None,
    cpu_request: str | None = None,
) -> dict[str, Any]:
    """Change CPU/memory requests or limits of one container."""
    who = _check(namespace, deployment, container)
    limits = {k: v for k, v in {"memory": memory_limit, "cpu": cpu_limit}.items() if v}
    requests = {k: v for k, v in {"memory": memory_request, "cpu": cpu_request}.items() if v}
    if not limits and not requests:
        fail("invalid", "nothing to change")
    with tool_span("patch_resources", who, namespace):
        resources: dict[str, Any] = {}
        if limits:
            resources["limits"] = limits
        if requests:
            resources["requests"] = requests
        detail = ", ".join(
            f"{k}={v}"
            for k, v in {
                **{f"limit.{k}": v for k, v in limits.items()},
                **{f"request.{k}": v for k, v in requests.items()},
            }.items()
        )
        body = {
            "spec": {"template": {"spec": {"containers": [{"name": container, "resources": resources}]}}},
            **_cause("patch_resources", f"{container} {detail}", who),
        }
        await k8s.request("PATCH", _path(namespace, deployment), who, body=body, content_type=STRATEGIC)
        return _result("patch_resources", namespace, deployment, who, f"{container}: {detail}")


@mcp.tool()
async def set_env(namespace: str, deployment: str, container: str, name: str, value: str) -> dict[str, Any]:
    """Set one environment variable on a container (triggers a rollout)."""
    who = _check(namespace, deployment, container)
    if not name.replace("_", "").isalnum() or not name[0].isalpha() or name.upper() != name:
        fail("invalid", f"bad env var name {name!r}")
    with tool_span("set_env", who, namespace):
        body = {
            "spec": {
                "template": {"spec": {"containers": [{"name": container, "env": [{"name": name, "value": value}]}]}}
            },
            **_cause("set_env", f"{container} {name}", who),
        }
        await k8s.request("PATCH", _path(namespace, deployment), who, body=body, content_type=STRATEGIC)
        return _result("set_env", namespace, deployment, who, f"{container}: set {name}")


@mcp.tool()
async def rollback_deployment(namespace: str, deployment: str, to_revision: int) -> dict[str, Any]:
    """Roll back to an earlier revision (like kubectl rollout undo --to-revision)."""
    who = _check(namespace, deployment)
    with tool_span("rollback_deployment", who, namespace):
        dep = await k8s.request("GET", _path(namespace, deployment), who)
        rs_list = await k8s.request(
            "GET",
            f"/apis/apps/v1/namespaces/{namespace}/replicasets",
            who,
            params={"labelSelector": selector_string(dep)},
        )
        target = next(
            (
                rs
                for rs in rs_list["items"]
                if (rs["metadata"].get("annotations") or {}).get(REVISION) == str(to_revision)
            ),
            None,
        )
        if target is None:
            fail("not_found", f"revision {to_revision} not found for deployment {deployment}")
        template = copy.deepcopy(target["spec"]["template"])
        template.setdefault("metadata", {}).setdefault("labels", {}).pop("pod-template-hash", None)
        patch = [
            {"op": "replace", "path": "/spec/template", "value": template},
            {
                "op": "add",
                "path": "/metadata/annotations/" + CHANGE_CAUSE.replace("/", "~1"),
                "value": f"copilot rollback_deployment: to revision {to_revision} (approved, as {who.user})",
            },
        ]
        await k8s.request("PATCH", _path(namespace, deployment), who, body=patch, content_type=JSON_PATCH)
        return _result("rollback_deployment", namespace, deployment, who, f"rolled back to revision {to_revision}")


def main() -> None:
    from mcp_servers.telemetry import setup

    setup("mcp-k8s-write")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
