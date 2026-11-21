"""MCP server: read-only Kubernetes tools, executed as the calling human (impersonation)."""

from __future__ import annotations

from typing import Any

from mcp_servers.common import K8sClient, build_server, check_name, current_identity, fail
from mcp_servers.k8s_format import describe_deployment as fmt_deployment
from mcp_servers.k8s_format import describe_pod as fmt_pod
from mcp_servers.k8s_format import selector_string, summarize_event, summarize_pod
from mcp_servers.telemetry import tool_span

mcp = build_server(
    "k8s-read",
    "Read-only Kubernetes tools (pods, logs, events, deployments). Calls run as the authenticated user.",
)
k8s = K8sClient()


async def _deployment(namespace: str, deployment: str, who: Any) -> dict[str, Any]:
    return await k8s.request("GET", f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}", who)


@mcp.tool()
async def list_pods(namespace: str, deployment: str | None = None) -> list[dict[str, Any]]:
    """List pods in a namespace (optionally only one deployment's pods)."""
    check_name("namespace", namespace)
    check_name("deployment", deployment)
    who = current_identity(write=False)
    with tool_span("list_pods", who, namespace):
        params = {}
        if deployment:
            params["labelSelector"] = selector_string(await _deployment(namespace, deployment, who))
        pods = await k8s.request("GET", f"/api/v1/namespaces/{namespace}/pods", who, params=params)
        return [summarize_pod(p) for p in pods["items"] if not p["metadata"].get("deletionTimestamp")]


@mcp.tool()
async def describe_pod(namespace: str, pod: str) -> dict[str, Any]:
    """Describe a pod: container states, last termination, image, resources and env."""
    check_name("namespace", namespace)
    check_name("pod", pod)
    who = current_identity(write=False)
    with tool_span("describe_pod", who, namespace):
        return fmt_pod(await k8s.request("GET", f"/api/v1/namespaces/{namespace}/pods/{pod}", who))


@mcp.tool()
async def get_pod_logs(
    namespace: str, pod: str, container: str | None = None, previous: bool = False, tail: int = 50
) -> dict[str, Any]:
    """Recent log lines of a pod's container (previous=true for the crashed instance)."""
    check_name("namespace", namespace)
    check_name("pod", pod)
    check_name("container", container)
    tail = max(1, min(int(tail), 200))
    who = current_identity(write=False)
    with tool_span("get_pod_logs", who, namespace):
        params: dict[str, Any] = {"tailLines": tail + 1, "previous": str(previous).lower()}
        if container:
            params["container"] = container
        try:
            text = await k8s.request(
                "GET", f"/api/v1/namespaces/{namespace}/pods/{pod}/log", who, params=params, raw=True
            )
        except Exception as exc:  # noqa: BLE001
            if previous and "previous terminated container" in str(exc):
                return {
                    "pod": pod,
                    "container": container,
                    "previous": True,
                    "lines": [],
                    "note": "no previous instance",
                }
            raise
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return {
            "pod": pod,
            "container": container,
            "previous": previous,
            "lines": lines[-tail:],
            "truncated": len(lines) > tail,
        }


@mcp.tool()
async def get_events(namespace: str, involved_object: str | None = None) -> list[dict[str, Any]]:
    """Kubernetes events in a namespace, newest last (optionally filtered by object name prefix)."""
    check_name("namespace", namespace)
    who = current_identity(write=False)
    with tool_span("get_events", who, namespace):
        events = (await k8s.request("GET", f"/api/v1/namespaces/{namespace}/events", who))["items"]
        out = [summarize_event(e) for e in events]
        if involved_object:
            needle = involved_object.split("/")[-1]
            out = [e for e in out if (e["object"] or "").split("/")[-1].startswith(needle)]
        out.sort(key=lambda e: e["last_seen"] or "")
        return out[-50:]


@mcp.tool()
async def describe_deployment(namespace: str, deployment: str) -> dict[str, Any]:
    """Deployment spec (image, resources, env) and rollout history with change causes."""
    check_name("namespace", namespace)
    check_name("deployment", deployment)
    who = current_identity(write=False)
    with tool_span("describe_deployment", who, namespace):
        dep = await _deployment(namespace, deployment, who)
        rs = await k8s.request(
            "GET",
            f"/apis/apps/v1/namespaces/{namespace}/replicasets",
            who,
            params={"labelSelector": selector_string(dep)},
        )
        return fmt_deployment(dep, rs["items"])


def main() -> None:
    from mcp_servers.telemetry import setup

    setup("mcp-k8s-read")
    if not mcp.settings.port:
        fail("invalid", "PORT not set")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
