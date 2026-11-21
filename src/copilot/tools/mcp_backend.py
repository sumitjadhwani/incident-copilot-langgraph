"""A `ToolBackend` that calls the MCP tool servers on the kind cluster.

The acting human's access token is forwarded as `Authorization: Bearer ...` to every server. The servers
verify it and impersonate that human in Kubernetes, so a run started by alice reads as `kc:alice`, and the
write after bob's approval executes as `kc:bob`. This backend never holds cluster credentials of its own.

Read and write tools live on separate servers (separate ServiceAccounts with different impersonation
rights); the backend only talks to the write server from `apply_action`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from copilot.config import Settings
from copilot.context import Actor
from copilot.observability.tracing import UNTRACED_TAG, current_run_span, mcp_client_span
from copilot.tools.backend import BackendError, ForbiddenError, MetricName, NotFoundError, TransientBackendError

READ_SERVER = "k8s_read"
WRITE_SERVER = "k8s_write"
METRICS_SERVER = "observability"


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_error_text(e) for e in exc.exceptions)
    return str(exc)


def map_error(exc: BaseException) -> BackendError:
    """Translate MCP/transport failures into the backend error contract."""
    text = _error_text(exc)
    lowered = text.lower()
    for code, cls in (
        ("forbidden:", ForbiddenError),
        ("not_found:", NotFoundError),
        ("unavailable:", TransientBackendError),
    ):
        idx = lowered.find(code)
        if idx >= 0:
            return cls(text[idx + len(code) :].strip())
    if "invalid:" in lowered:
        return BackendError(text[lowered.find("invalid:") + 8 :].strip())
    if "401" in text or "unauthorized" in lowered:
        return ForbiddenError(f"tool server rejected the user's token: {text[:200]}")
    # Connection refused, timeouts, 5xx from the server: worth a retry.
    return TransientBackendError(f"tool server call failed: {text[:300]}")


def _parse(result: Any) -> Any:
    if isinstance(result, tuple):  # (content, artifact)
        result = result[0]
    if isinstance(result, list):
        result = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in result)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return result
    return result


class McpBackend:
    def __init__(self, settings: Settings, actor: Actor) -> None:
        if not actor.access_token:
            raise ForbiddenError("the MCP backend needs the acting user's access token")
        headers = {"Authorization": f"Bearer {actor.access_token}"}
        timeout = settings.mcp_timeout_s
        self.actor = actor
        self._client = MultiServerMCPClient(
            {
                METRICS_SERVER: {
                    "transport": "streamable_http",
                    "url": settings.mcp_observability_url,
                    "headers": headers,
                    "timeout": timeout,
                },
                READ_SERVER: {
                    "transport": "streamable_http",
                    "url": settings.mcp_k8s_read_url,
                    "headers": headers,
                    "timeout": timeout,
                },
                WRITE_SERVER: {
                    "transport": "streamable_http",
                    "url": settings.mcp_k8s_write_url,
                    "headers": headers,
                    "timeout": timeout,
                },
            },
            handle_tool_errors=False,
        )
        self._tools: dict[str, dict[str, BaseTool]] = {}

    async def _tool(self, server: str, name: str) -> BaseTool:
        if server not in self._tools:
            try:
                tools = await self._client.get_tools(server_name=server)
            except BaseException as exc:  # noqa: BLE001 - includes anyio ExceptionGroups
                if isinstance(exc, KeyboardInterrupt | SystemExit):
                    raise
                raise map_error(exc) from exc
            self._tools[server] = {t.name: t for t in tools}
        try:
            return self._tools[server][name]
        except KeyError:
            raise BackendError(f"tool '{name}' not offered by MCP server '{server}'") from None

    async def _call(self, server: str, name: str, args: dict[str, Any]) -> Any:
        # One client span per MCP call, under the tool or node span that made it. The HTTP requests (session
        # setup, tools/list, tools/call) nest inside it, and the MCP server continues the trace from there.
        with current_run_span(), mcp_client_span(server, name):
            return await self._invoke(server, name, args)

    async def _invoke(self, server: str, name: str, args: dict[str, Any]) -> Any:
        tool = await self._tool(server, name)
        clean = {k: v for k, v in args.items() if v is not None}
        try:
            # Invoke as a tool call to get the artifact, which holds MCP `structuredContent`
            # (a list result is otherwise split into one text block per item). The adapter's own LangChain tool
            # run is tagged so GraphTracer does not add a second span for it.
            msg = await tool.ainvoke(
                {"type": "tool_call", "id": f"mcp-{uuid.uuid4().hex[:8]}", "name": name, "args": clean},
                config={"tags": [UNTRACED_TAG]},
            )
            artifact = getattr(msg, "artifact", None)
            structured = (
                artifact.get("structured_content")
                if isinstance(artifact, dict)
                else getattr(artifact, "structured_content", None)
            )
            if structured is not None:
                return structured["result"] if set(structured) == {"result"} else structured
            return _parse(getattr(msg, "content", msg))
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                raise
            raise map_error(exc) from exc

    # -- reads ------------------------------------------------------------------------------

    async def list_pods(self, namespace: str, deployment: str | None = None) -> list[dict[str, Any]]:
        return await self._call(READ_SERVER, "list_pods", {"namespace": namespace, "deployment": deployment})

    async def describe_pod(self, namespace: str, pod: str) -> dict[str, Any]:
        return await self._call(READ_SERVER, "describe_pod", {"namespace": namespace, "pod": pod})

    async def get_pod_logs(
        self, namespace: str, pod: str, container: str | None = None, previous: bool = False, tail: int = 50
    ) -> dict[str, Any]:
        return await self._call(
            READ_SERVER,
            "get_pod_logs",
            {"namespace": namespace, "pod": pod, "container": container, "previous": previous, "tail": tail},
        )

    async def get_events(self, namespace: str, involved_object: str | None = None) -> list[dict[str, Any]]:
        return await self._call(READ_SERVER, "get_events", {"namespace": namespace, "involved_object": involved_object})

    async def describe_deployment(self, namespace: str, deployment: str) -> dict[str, Any]:
        return await self._call(READ_SERVER, "describe_deployment", {"namespace": namespace, "deployment": deployment})

    async def query_metrics(
        self, namespace: str, service: str, metric: MetricName, window_minutes: int = 60
    ) -> dict[str, Any]:
        return await self._call(
            METRICS_SERVER,
            "query_metrics",
            {"namespace": namespace, "service": service, "metric": metric, "window_minutes": window_minutes},
        )

    # -- writes -----------------------------------------------------------------------------

    async def apply_action(self, action: Any, actor: Actor) -> dict[str, Any]:
        if actor.user_id != self.actor.user_id:
            raise ForbiddenError("write must run with the approver's own credentials")
        args = action.model_dump(exclude={"tool"})
        return await self._call(WRITE_SERVER, action.tool, args)
