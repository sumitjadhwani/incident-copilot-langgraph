"""The interface between agent tools and the systems they read and change.

Tools never talk to Kubernetes or Prometheus directly; they call a `ToolBackend`. Milestone 1 uses
`ScenarioBackend` (canned data). Milestone 9 adds an MCP-backed implementation that forwards the
user's identity. Swapping backends does not touch the graph.

Error contract:
- `NotFoundError`: the request was valid but the object does not exist. Tools turn this into an
  error message for the model so it can correct itself.
- `TransientBackendError`: a flaky dependency (timeout, 503). Tools let it propagate so the node's
  `RetryPolicy` can retry it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from copilot.context import Actor

MetricName = Literal["memory_usage", "cpu_usage", "restart_count", "http_5xx_rate", "http_p99_latency"]

METRIC_NAMES: tuple[str, ...] = MetricName.__args__  # type: ignore[attr-defined]


class BackendError(Exception):
    """Base class for tool backend failures."""


class NotFoundError(BackendError):
    """The requested namespace, pod, deployment or metric does not exist."""


class TransientBackendError(BackendError):
    """A temporary failure worth retrying."""


class ForbiddenError(BackendError):
    """The acting human is not allowed to do this (enforced by Kubernetes RBAC or the tool server)."""


@runtime_checkable
class ToolBackend(Protocol):
    # -- reads ------------------------------------------------------------------------------
    async def list_pods(self, namespace: str, deployment: str | None = None) -> list[dict[str, Any]]: ...

    async def describe_pod(self, namespace: str, pod: str) -> dict[str, Any]: ...

    async def get_pod_logs(
        self, namespace: str, pod: str, container: str | None = None, previous: bool = False, tail: int = 50
    ) -> dict[str, Any]: ...

    async def get_events(self, namespace: str, involved_object: str | None = None) -> list[dict[str, Any]]: ...

    async def describe_deployment(self, namespace: str, deployment: str) -> dict[str, Any]: ...

    async def query_metrics(
        self, namespace: str, service: str, metric: MetricName, window_minutes: int = 60
    ) -> dict[str, Any]: ...

    # -- writes -----------------------------------------------------------------------------
    async def apply_action(self, action: Any, actor: Actor) -> dict[str, Any]:
        """Apply one validated `ActionSpec` on behalf of `actor` (the human who approved it)."""
        ...
