"""Turn raw Kubernetes objects into the compact shapes the agent's tools return.

The shapes match the scenario mock backend, so the agent sees the same structure in tests and live.
Secret-backed env vars are never revealed: only literal values are shown.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

REVISION = "deployment.kubernetes.io/revision"
CHANGE_CAUSE = "kubernetes.io/change-cause"


def _age(ts: str | None) -> str:
    if not ts:
        return ""
    delta = datetime.now(UTC) - datetime.fromisoformat(ts.replace("Z", "+00:00"))
    s = int(delta.total_seconds())
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def deployment_of(pod: dict[str, Any]) -> str:
    labels = pod["metadata"].get("labels") or {}
    if "app" in labels:
        return labels["app"]
    for ref in pod["metadata"].get("ownerReferences") or []:
        if ref.get("kind") == "ReplicaSet":
            return ref["name"].rsplit("-", 1)[0]
    return ""


def pod_status(pod: dict[str, Any]) -> str:
    """Like the STATUS column of kubectl get pods."""
    if pod["metadata"].get("deletionTimestamp"):
        return "Terminating"
    for cs in (pod.get("status") or {}).get("containerStatuses") or []:
        state = cs.get("state") or {}
        if "waiting" in state and state["waiting"].get("reason"):
            return state["waiting"]["reason"]
        if "terminated" in state and state["terminated"].get("reason"):
            return state["terminated"]["reason"]
    return (pod.get("status") or {}).get("phase", "Unknown")


def _state(cs: dict[str, Any]) -> str:
    state = cs.get("state") or {}
    if "running" in state:
        return "Running"
    if "waiting" in state:
        return f"Waiting ({state['waiting'].get('reason', '')})"
    if "terminated" in state:
        return f"Terminated ({state['terminated'].get('reason', '')})"
    return "Unknown"


def _last_state(cs: dict[str, Any]) -> dict[str, Any] | None:
    term = (cs.get("lastState") or {}).get("terminated")
    if not term:
        return None
    return {
        "state": "Terminated",
        "reason": term.get("reason"),
        "exit_code": term.get("exitCode"),
        "finished_at": term.get("finishedAt"),
    }


def summarize_pod(pod: dict[str, Any]) -> dict[str, Any]:
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    return {
        "name": pod["metadata"]["name"],
        "deployment": deployment_of(pod),
        "status": pod_status(pod),
        "ready": f"{sum(bool(c.get('ready')) for c in statuses)}/{len(pod['spec']['containers'])}",
        "restarts": sum(c.get("restartCount", 0) for c in statuses),
        "node": pod["spec"].get("nodeName", ""),
        "age": _age(pod["metadata"].get("creationTimestamp")),
    }


def _env(container: dict[str, Any]) -> dict[str, str]:
    env = {}
    for e in container.get("env") or []:
        env[e["name"]] = e.get("value", "<from secret/configmap>")
    return env


def describe_pod(pod: dict[str, Any]) -> dict[str, Any]:
    statuses = {c["name"]: c for c in (pod.get("status") or {}).get("containerStatuses") or []}
    containers = []
    for spec in pod["spec"]["containers"]:
        cs = statuses.get(spec["name"], {})
        containers.append(
            {
                "name": spec["name"],
                "image": spec.get("image"),
                "state": _state(cs) if cs else "Unknown",
                "ready": bool(cs.get("ready")),
                "last_state": _last_state(cs) if cs else None,
                "resources": {
                    "requests": (spec.get("resources") or {}).get("requests", {}),
                    "limits": (spec.get("resources") or {}).get("limits", {}),
                },
                "env": _env(spec),
            }
        )
    summary = summarize_pod(pod)
    return {
        **{k: summary[k] for k in ("name", "deployment", "status", "restarts", "node", "age")},
        "namespace": pod["metadata"]["namespace"],
        "phase": (pod.get("status") or {}).get("phase"),
        "containers": containers,
    }


def summarize_event(ev: dict[str, Any]) -> dict[str, Any]:
    obj = ev.get("involvedObject") or ev.get("regarding") or {}
    return {
        "type": ev.get("type"),
        "reason": ev.get("reason"),
        "object": f"{(obj.get('kind') or '').lower()}/{obj.get('name')}",
        "message": ev.get("message") or ev.get("note"),
        "count": ev.get("count") or (ev.get("series") or {}).get("count") or 1,
        "last_seen": ev.get("lastTimestamp")
        or ev.get("eventTime")
        or (ev.get("metadata") or {}).get("creationTimestamp"),
    }


def describe_deployment(dep: dict[str, Any], replicasets: list[dict[str, Any]]) -> dict[str, Any]:
    containers = [
        {
            "name": c["name"],
            "image": c.get("image"),
            "resources": {
                "requests": (c.get("resources") or {}).get("requests", {}),
                "limits": (c.get("resources") or {}).get("limits", {}),
            },
            "env": _env(c),
        }
        for c in dep["spec"]["template"]["spec"]["containers"]
    ]
    revisions = []
    for rs in replicasets:
        ann = rs["metadata"].get("annotations") or {}
        if REVISION not in ann:
            continue
        revisions.append(
            {
                "revision": int(ann[REVISION]),
                "image": rs["spec"]["template"]["spec"]["containers"][0].get("image"),
                "change_cause": ann.get(CHANGE_CAUSE, ""),
                "created_at": rs["metadata"].get("creationTimestamp"),
            }
        )
    revisions.sort(key=lambda r: r["revision"])
    status = dep.get("status") or {}
    return {
        "name": dep["metadata"]["name"],
        "replicas": dep["spec"].get("replicas", 1),
        "ready_replicas": status.get("readyReplicas", 0),
        "containers": containers,
        "revisions": revisions,
    }


def selector_string(dep: dict[str, Any]) -> str:
    labels = (dep["spec"].get("selector") or {}).get("matchLabels") or {}
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
