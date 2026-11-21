"""Read-only investigation tools exposed to the model.

Each tool resolves its backend from the run context (`ToolRuntime.context`), never from model
arguments. Results are compact JSON so the model sees structured data rather than Python reprs.
"""

import json
from collections.abc import Awaitable
from typing import Any

from langchain.tools import ToolRuntime, tool

from copilot.context import CopilotContext
from copilot.tools.backend import ForbiddenError, MetricName, NotFoundError


def _ctx(runtime: ToolRuntime[Any]) -> CopilotContext:
    # Tools are annotated ToolRuntime[Any]: ToolRuntime[CopilotContext] would make pydantic build a
    # validator for the context dataclass (and its protocol-typed fields) when creating tool schemas.
    return runtime.context


def _dump(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), default=str)


async def _call(coro: Awaitable[Any]) -> str:
    """Return the result as JSON; turn "not found" / "forbidden" into an error the model can act on.

    Transient errors are deliberately not caught: they propagate so the node's RetryPolicy retries.
    """
    try:
        return _dump(await coro)
    except (NotFoundError, ForbiddenError) as exc:
        return _dump({"error": str(exc)})


@tool
async def list_pods(namespace: str, runtime: ToolRuntime[Any], deployment: str | None = None) -> str:
    """List pods in a namespace with status, readiness, restart count and node.

    Args:
        namespace: Kubernetes namespace, e.g. "acme-prod".
        deployment: Optional deployment name to filter by.
    """
    return await _call(_ctx(runtime).backend.list_pods(namespace, deployment))


@tool
async def describe_pod(namespace: str, pod: str, runtime: ToolRuntime[Any]) -> str:
    """Describe a pod: container states, last termination reason and exit code, image,
    resource requests/limits and environment variables.

    Args:
        namespace: Kubernetes namespace.
        pod: Full pod name (use list_pods to find it).
    """
    return await _call(_ctx(runtime).backend.describe_pod(namespace, pod))


@tool
async def get_pod_logs(
    namespace: str,
    pod: str,
    runtime: ToolRuntime[Any],
    container: str | None = None,
    previous: bool = False,
    tail: int = 50,
) -> str:
    """Fetch recent log lines from a pod's container.

    Args:
        namespace: Kubernetes namespace.
        pod: Full pod name.
        container: Container name; defaults to the first container.
        previous: If true, return logs from the previous (crashed) container instance.
            Use this for pods that are restarting.
        tail: Number of most recent lines to return (max 200).
    """
    return await _call(_ctx(runtime).backend.get_pod_logs(namespace, pod, container, previous, tail))


@tool
async def get_events(namespace: str, runtime: ToolRuntime[Any], involved_object: str | None = None) -> str:
    """List Kubernetes events in a namespace (BackOff, FailedScheduling, Pulled, ...).

    Args:
        namespace: Kubernetes namespace.
        involved_object: Optional filter such as "pod/<name>", "deployment/<name>" or a name prefix.
    """
    return await _call(_ctx(runtime).backend.get_events(namespace, involved_object))


@tool
async def describe_deployment(namespace: str, deployment: str, runtime: ToolRuntime[Any]) -> str:
    """Describe a deployment: replicas, container spec (image, resources, env) and rollout history.
    Useful for spotting a recent release that lines up with the incident start.

    Args:
        namespace: Kubernetes namespace.
        deployment: Deployment name (usually the service name).
    """
    return await _call(_ctx(runtime).backend.describe_deployment(namespace, deployment))


@tool
async def query_metrics(
    namespace: str, service: str, metric: MetricName, runtime: ToolRuntime[Any], window_minutes: int = 60
) -> str:
    """Query a time series for a service from Prometheus. Returns min/max/latest and the points.

    Args:
        namespace: Kubernetes namespace of the service.
        service: Service (deployment) name, e.g. "checkout-api".
        metric: One of memory_usage (MiB, max per pod), cpu_usage (cores), restart_count,
            http_5xx_rate (percent), http_p99_latency (ms).
        window_minutes: How far back to look.
    """
    return await _call(_ctx(runtime).backend.query_metrics(namespace, service, metric, window_minutes))


READ_TOOLS = [list_pods, describe_pod, get_pod_logs, get_events, describe_deployment, query_metrics]
