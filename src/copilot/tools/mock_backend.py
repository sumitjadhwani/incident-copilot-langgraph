"""A `ToolBackend` that answers from a synthetic scenario snapshot.

Writes mutate a private copy of the scenario: the spec change is applied (limits, env, replicas,
image), and if the action matches one of the scenario's fix rules the affected deployments become
healthy. That lets the graph verify a fix after executing it, without a cluster.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import TYPE_CHECKING, Any

from copilot.scenarios import ContainerStatus, FixRule, Namespace, Pod, Scenario
from copilot.tools.backend import METRIC_NAMES, MetricName, NotFoundError, TransientBackendError
from copilot.tools.write_tools import (
    PatchResources,
    RestartDeployment,
    RollbackDeployment,
    ScaleDeployment,
    SetEnv,
)

if TYPE_CHECKING:
    from copilot.context import Actor

MAX_LOG_TAIL = 200


def parse_mib(value: str) -> int:
    if value.endswith("Gi"):
        return int(value[:-2]) * 1024
    if value.endswith("Mi"):
        return int(value[:-2])
    raise ValueError(f"unsupported memory quantity {value!r}")


def parse_millicores(value: str) -> int:
    return int(value[:-1]) if value.endswith("m") else int(float(value) * 1000)


def _matches_value(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        if actual is None:
            return False
        if "min_mib" in expected:
            return parse_mib(str(actual)) >= expected["min_mib"]
        if "min_millicores" in expected:
            return parse_millicores(str(actual)) >= expected["min_millicores"]
        if "min_int" in expected:
            return int(actual) >= expected["min_int"]
        if "max_int" in expected:
            return str(actual).isdigit() and int(actual) <= expected["max_int"]
        if "one_of" in expected:
            return actual in expected["one_of"]
        raise ValueError(f"unknown constraint {expected}")
    return actual == expected


def action_matches(rule: FixRule, action: Any) -> bool:
    if rule.tool != action.tool:
        return False
    return all(_matches_value(exp, getattr(action, key, None)) for key, exp in rule.args.items())


class ScenarioBackend:
    def __init__(self, scenario: Scenario) -> None:
        # Private copy: writes must not leak between runs that share a loaded scenario.
        self.scenario = scenario.model_copy(deep=True)
        self.applied: list[dict[str, Any]] = []

    # -- helpers -----------------------------------------------------------------------------

    def _namespace(self, namespace: str) -> Namespace:
        try:
            return self.scenario.namespaces[namespace]
        except KeyError:
            raise NotFoundError(
                f"namespace '{namespace}' not found; known namespaces: {sorted(self.scenario.namespaces)}"
            ) from None

    def _pod(self, namespace: str, pod: str) -> Pod:
        ns = self._namespace(namespace)
        for p in ns.pods:
            if p.name == pod:
                return p
        raise NotFoundError(f"pod '{pod}' not found in namespace '{namespace}'; use list_pods to see pods")

    # -- ToolBackend -------------------------------------------------------------------------

    async def list_pods(self, namespace: str, deployment: str | None = None) -> list[dict[str, Any]]:
        ns = self._namespace(namespace)
        pods = [p for p in ns.pods if deployment is None or p.deployment == deployment]
        return [
            {
                "name": p.name,
                "deployment": p.deployment,
                "status": p.status,
                "ready": f"{sum(c.ready for c in p.containers)}/{len(p.containers)}",
                "restarts": p.restarts,
                "node": p.node,
                "age": p.age,
            }
            for p in pods
        ]

    async def describe_pod(self, namespace: str, pod: str) -> dict[str, Any]:
        p = self._pod(namespace, pod)
        deployment = self._namespace(namespace).deployments.get(p.deployment)
        specs = {c.name: c for c in deployment.containers} if deployment else {}
        containers = []
        for status in p.containers:
            spec = specs.get(status.name)
            containers.append(
                {
                    "name": status.name,
                    "image": spec.image if spec else None,
                    "state": status.state,
                    "ready": status.ready,
                    "last_state": status.last_state,
                    "resources": spec.resources.model_dump() if spec else None,
                    "env": spec.env if spec else None,
                }
            )
        return {
            "name": p.name,
            "namespace": namespace,
            "deployment": p.deployment,
            "phase": p.phase,
            "status": p.status,
            "restarts": p.restarts,
            "node": p.node,
            "age": p.age,
            "containers": containers,
        }

    async def get_pod_logs(
        self, namespace: str, pod: str, container: str | None = None, previous: bool = False, tail: int = 50
    ) -> dict[str, Any]:
        p = self._pod(namespace, pod)
        name = container or (p.containers[0].name if p.containers else None)
        if name is None or name not in p.logs:
            raise NotFoundError(f"container '{container}' not found in pod '{pod}'; containers: {list(p.logs)}")
        logs = p.logs[name]
        lines = logs.previous if previous else logs.current
        tail = max(1, min(tail, MAX_LOG_TAIL))
        return {
            "pod": pod,
            "container": name,
            "previous": previous,
            "lines": lines[-tail:],
            "truncated": len(lines) > tail,
        }

    async def get_events(self, namespace: str, involved_object: str | None = None) -> list[dict[str, Any]]:
        ns = self._namespace(namespace)
        events = ns.events
        if involved_object:
            # Accept "pod/name", "deployment/name" or a bare name.
            needle = involved_object.split("/")[-1]
            events = [e for e in events if e.object.split("/")[-1].startswith(needle)]
        return [e.model_dump() for e in events]

    async def describe_deployment(self, namespace: str, deployment: str) -> dict[str, Any]:
        ns = self._namespace(namespace)
        d = ns.deployments.get(deployment)
        if d is None:
            raise NotFoundError(
                f"deployment '{deployment}' not found in '{namespace}'; deployments: {sorted(ns.deployments)}"
            )
        return d.model_dump()

    async def query_metrics(
        self, namespace: str, service: str, metric: MetricName, window_minutes: int = 60
    ) -> dict[str, Any]:
        self._namespace(namespace)
        if metric not in METRIC_NAMES:
            raise NotFoundError(f"unknown metric '{metric}'; supported: {list(METRIC_NAMES)}")
        series = self.scenario.metrics.get(service, {}).get(metric)
        if series is None:
            raise NotFoundError(f"no '{metric}' data for service '{service}'")
        points = [(t, v) for t, v in series.points if t >= -window_minutes]
        values = [v for _, v in points]
        return {
            "service": service,
            "metric": metric,
            "unit": series.unit,
            "description": series.description,
            "window_minutes": window_minutes,
            "summary": {"min": min(values), "max": max(values), "latest": values[-1]} if values else None,
            "points": [{"minutes_ago": -t, "value": v} for t, v in points],
        }

    # -- writes ------------------------------------------------------------------------------

    async def apply_action(self, action: Any, actor: Actor) -> dict[str, Any]:
        ns = self._namespace(action.namespace)
        dep = ns.deployments.get(action.deployment)
        if dep is None:
            raise NotFoundError(f"deployment '{action.deployment}' not found in '{action.namespace}'")

        if isinstance(action, PatchResources):
            container = next((c for c in dep.containers if c.name == action.container), None)
            if container is None:
                raise NotFoundError(f"container '{action.container}' not found in '{action.deployment}'")
            for field, bucket, key in (
                ("memory_limit", "limits", "memory"),
                ("memory_request", "requests", "memory"),
                ("cpu_limit", "limits", "cpu"),
                ("cpu_request", "requests", "cpu"),
            ):
                if (value := getattr(action, field)) is not None:
                    getattr(container.resources, bucket)[key] = value
        elif isinstance(action, SetEnv):
            container = next((c for c in dep.containers if c.name == action.container), None)
            if container is None:
                raise NotFoundError(f"container '{action.container}' not found in '{action.deployment}'")
            container.env[action.name] = action.value
        elif isinstance(action, ScaleDeployment):
            dep.replicas = action.replicas
        elif isinstance(action, RollbackDeployment):
            target = next((r for r in dep.revisions if r.revision == action.to_revision), None)
            if target is None:
                raise NotFoundError(f"revision {action.to_revision} not found for '{action.deployment}'")
            for c in dep.containers:
                c.image = target.image
        elif not isinstance(action, RestartDeployment):
            raise ValueError(f"unsupported action {action!r}")

        fixed = any(action_matches(rule, action) for rule in self.scenario.remediation.fixes)
        if fixed:
            for name in self.scenario.remediation.heals:
                self._heal(action.namespace, name)
        record = {
            "tool": action.tool,
            "target": action.target,
            "as_user": actor.username,
            "summary": f"{action.tool} applied to {action.target}",
        }
        self.applied.append(record)
        return record

    def _heal(self, namespace: str, deployment: str) -> None:
        ns = self._namespace(namespace)
        dep = ns.deployments.get(deployment)
        if dep is None:
            return
        dep.ready_replicas = dep.replicas
        pods = [p for p in ns.pods if p.deployment == deployment]
        for pod in pods:
            pod.status = "Running"
            pod.phase = "Running"
            pod.restarts = 0
            pod.age = "0m"
            for c in pod.containers:
                c.state = "Running"
                c.ready = True
        container_names = [c.name for c in dep.containers] or ["app"]
        for i in range(len(pods), dep.replicas):
            ns.pods.append(
                Pod(
                    name=f"{deployment}-healed-{i}",
                    deployment=deployment,
                    phase="Running",
                    status="Running",
                    restarts=0,
                    node="worker-1",
                    age="0m",
                    containers=[ContainerStatus(name=n, state="Running", ready=True) for n in container_names],
                )
            )
        # Scaling down removes surplus pods.
        surplus = len([p for p in ns.pods if p.deployment == deployment]) - dep.replicas
        for _ in range(max(0, surplus)):
            victim = next(p for p in reversed(ns.pods) if p.deployment == deployment)
            ns.pods.remove(victim)


class FlakyBackend:
    """Wraps a backend and fails the first N calls of each read method with a transient error.

    Used to demonstrate (and test) RetryPolicy on the tool nodes.
    """

    READ_METHODS = frozenset(
        {"list_pods", "describe_pod", "get_pod_logs", "get_events", "describe_deployment", "query_metrics"}
    )

    def __init__(self, inner: Any, failures_per_method: int = 1, delay_s: float = 0.0) -> None:
        self.inner = inner
        self.failures_per_method = failures_per_method
        self.delay_s = delay_s
        self.calls: Counter[str] = Counter()
        self.failures: Counter[str] = Counter()

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.inner, name)
        if name not in self.READ_METHODS:
            return target

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.calls[name] += 1
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            if self.failures[name] < self.failures_per_method:
                self.failures[name] += 1
                raise TransientBackendError(f"{name}: upstream timed out (injected fault)")
            return await target(*args, **kwargs)

        return wrapper
