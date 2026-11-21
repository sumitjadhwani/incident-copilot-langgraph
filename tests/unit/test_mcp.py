"""MCP servers and the MCP backend, without a cluster: identity mapping, token checks, formatting,
tenant guard on metrics, write patches, error mapping, and the stability check in verify."""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from langchain_core.tools import ToolException
from mcp.server.fastmcp.exceptions import ToolError

from copilot.tools.backend import ForbiddenError, NotFoundError, TransientBackendError
from copilot.tools.mcp_backend import map_error
from mcp_servers import k8s_format, k8s_read, k8s_write, observability
from mcp_servers.common import Impersonation, KeycloakTokenVerifier, impersonation_for

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISS = "http://localhost:8080/realms/incident-copilot"


def claims(user: str, tenant: str | None, roles: list[str]) -> dict[str, Any]:
    c = {"sub": f"u-{user}", "preferred_username": user, "realm_access": {"roles": roles}}
    if tenant:
        c["tenant"] = tenant
    return c


# ------------------------------------------------------------------------------------ identity


def test_reader_never_gets_approver_group():
    who = impersonation_for(claims("bob", "acme", ["sre", "sre-approver"]), write=False)
    assert who == Impersonation("kc:bob", ("copilot:tenant:acme",), "acme", "bob")


def test_writer_gets_tenant_scoped_approver_group_only_with_role():
    bob = impersonation_for(claims("bob", "acme", ["sre", "sre-approver"]), write=True)
    alice = impersonation_for(claims("alice", "acme", ["sre"]), write=True)
    carol = impersonation_for(claims("carol", "globex", ["sre-approver"]), write=True)
    assert bob.groups == ("copilot:tenant:acme", "copilot:approvers:acme")
    assert alice.groups == ("copilot:tenant:acme",)
    assert carol.groups == ("copilot:tenant:globex", "copilot:approvers:globex")


def test_tenant_from_group_path_and_missing_tenant():
    c = claims("dave", None, ["sre"]) | {"groups": ["/tenants/globex"]}
    assert impersonation_for(c, write=False).tenant == "globex"
    with pytest.raises(ToolError, match="forbidden"):
        impersonation_for(claims("eve", None, ["sre"]), write=False)


async def test_token_verifier_checks_audience_and_expiry():
    verifier = KeycloakTokenVerifier(issuer=ISS, audience="copilot-mcp", key=KEY.public_key())
    now = int(time.time())
    base = claims("bob", "acme", ["sre"]) | {"iss": ISS, "iat": now, "exp": now + 300}
    good = jwt.encode(base | {"aud": ["copilot-api", "copilot-mcp"]}, KEY, algorithm="RS256")
    api_only = jwt.encode(base | {"aud": "copilot-api"}, KEY, algorithm="RS256")
    expired = jwt.encode(base | {"aud": "copilot-mcp", "exp": now - 3600}, KEY, algorithm="RS256")
    token = await verifier.verify_token(good)
    assert token is not None and token.claims["preferred_username"] == "bob" and token.subject == "u-bob"
    assert await verifier.verify_token(api_only) is None
    assert await verifier.verify_token(expired) is None


# ------------------------------------------------------------------------------------ formatting

POD = {
    "metadata": {
        "name": "checkout-api-7d9f-x2k4p",
        "namespace": "acme-prod",
        "labels": {"app": "checkout-api"},
        "creationTimestamp": "2026-11-21T10:00:00Z",
    },
    "spec": {
        "nodeName": "kind-control-plane",
        "containers": [
            {
                "name": "app",
                "image": "buggy-service:dev",
                "resources": {"limits": {"memory": "128Mi"}},
                "env": [
                    {"name": "CACHE_MB", "value": "200"},
                    {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db", "key": "pw"}}},
                ],
            }
        ],
    },
    "status": {
        "phase": "Running",
        "containerStatuses": [
            {
                "name": "app",
                "ready": False,
                "restartCount": 7,
                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                "lastState": {
                    "terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": "2026-11-21T10:05:00Z"}
                },
            }
        ],
    },
}


def test_pod_formatting_matches_mock_shape_and_hides_secrets():
    s = k8s_format.summarize_pod(POD)
    assert s["status"] == "CrashLoopBackOff" and s["ready"] == "0/1" and s["restarts"] == 7
    d = k8s_format.describe_pod(POD)
    app = d["containers"][0]
    assert app["last_state"] == {
        "state": "Terminated",
        "reason": "OOMKilled",
        "exit_code": 137,
        "finished_at": "2026-11-21T10:05:00Z",
    }
    assert app["env"] == {"CACHE_MB": "200", "DB_PASSWORD": "<from secret/configmap>"}
    assert app["resources"]["limits"] == {"memory": "128Mi"}


def test_deployment_revisions():
    dep = {
        "metadata": {"name": "checkout-api"},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "x"}},
            "template": {"spec": {"containers": [{"name": "app", "image": "img:2"}]}},
        },
        "status": {"readyReplicas": 1},
    }
    rs = [
        {
            "metadata": {
                "annotations": {k8s_format.REVISION: "2", k8s_format.CHANGE_CAUSE: "add cache"},
                "creationTimestamp": "t2",
            },
            "spec": {"template": {"spec": {"containers": [{"image": "img:2"}]}}},
        },
        {
            "metadata": {
                "annotations": {k8s_format.REVISION: "1", k8s_format.CHANGE_CAUSE: "initial"},
                "creationTimestamp": "t1",
            },
            "spec": {"template": {"spec": {"containers": [{"image": "img:1"}]}}},
        },
    ]
    out = k8s_format.describe_deployment(dep, rs)
    assert [r["revision"] for r in out["revisions"]] == [1, 2]
    assert out["revisions"][1]["change_cause"] == "add cache"
    assert k8s_format.selector_string(dep) == "app=x"


# ------------------------------------------------------------------------------------ servers


class FakeK8s:
    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def request(self, method: str, path: str, who: Impersonation, **kw: Any) -> Any:
        self.calls.append({"method": method, "path": path, "who": who, **kw})
        return self.responses.get((method, path), {"items": []})


BOB_W = Impersonation("kc:bob", ("copilot:tenant:acme", "copilot:approvers:acme"), "acme", "bob")


async def test_list_pods_uses_deployment_selector_and_identity(monkeypatch):
    fake = FakeK8s(
        {
            ("GET", "/apis/apps/v1/namespaces/acme-prod/deployments/checkout-api"): {
                "spec": {"selector": {"matchLabels": {"app": "checkout-api"}}}
            },
            ("GET", "/api/v1/namespaces/acme-prod/pods"): {"items": [POD]},
        }
    )
    reader = Impersonation("kc:alice", ("copilot:tenant:acme",), "acme", "alice")
    monkeypatch.setattr(k8s_read, "k8s", fake)
    monkeypatch.setattr(k8s_read, "current_identity", lambda write: reader)
    pods = await k8s_read.list_pods("acme-prod", "checkout-api")
    assert pods[0]["status"] == "CrashLoopBackOff"
    assert fake.calls[-1]["params"] == {"labelSelector": "app=checkout-api"}
    assert all(c["who"] == reader for c in fake.calls)


async def test_read_tools_reject_bad_names():
    with pytest.raises(ToolError, match="invalid"):
        await k8s_read.describe_pod("acme-prod", "../../secrets")


async def test_rollback_builds_json_patch_with_change_cause(monkeypatch):
    dep_path = "/apis/apps/v1/namespaces/acme-prod/deployments/checkout-api"
    old_template = {
        "metadata": {"labels": {"app": "checkout-api", "pod-template-hash": "abc"}},
        "spec": {"containers": [{"name": "app", "env": [{"name": "CACHE_MB", "value": "0"}]}]},
    }
    fake = FakeK8s(
        {
            ("GET", dep_path): {"spec": {"selector": {"matchLabels": {"app": "checkout-api"}}}},
            ("GET", "/apis/apps/v1/namespaces/acme-prod/replicasets"): {
                "items": [
                    {"metadata": {"annotations": {k8s_format.REVISION: "1"}}, "spec": {"template": old_template}},
                    {"metadata": {"annotations": {k8s_format.REVISION: "2"}}, "spec": {"template": {}}},
                ]
            },
        }
    )
    monkeypatch.setattr(k8s_write, "k8s", fake)
    monkeypatch.setattr(k8s_write, "current_identity", lambda write: BOB_W)
    result = await k8s_write.rollback_deployment("acme-prod", "checkout-api", 1)
    patch = fake.calls[-1]
    assert patch["method"] == "PATCH" and patch["content_type"] == k8s_write.JSON_PATCH
    template = patch["body"][0]["value"]
    assert "pod-template-hash" not in template["metadata"]["labels"]
    assert template["spec"]["containers"][0]["env"] == [{"name": "CACHE_MB", "value": "0"}]
    assert "as kc:bob" in patch["body"][1]["value"]
    assert result["as_user"] == "kc:bob"


async def test_set_env_is_a_strategic_merge_by_name(monkeypatch):
    fake = FakeK8s({})
    monkeypatch.setattr(k8s_write, "k8s", fake)
    monkeypatch.setattr(k8s_write, "current_identity", lambda write: BOB_W)
    await k8s_write.set_env("acme-prod", "notification-svc", "app", "SMTP_PORT", "587")
    call = fake.calls[-1]
    assert call["content_type"] == k8s_write.STRATEGIC
    assert call["body"]["spec"]["template"]["spec"]["containers"] == [
        {"name": "app", "env": [{"name": "SMTP_PORT", "value": "587"}]}
    ]
    with pytest.raises(ToolError, match="invalid"):
        await k8s_write.set_env("acme-prod", "notification-svc", "app", "bad-name", "x")


async def test_metrics_are_tenant_scoped_and_from_catalog(monkeypatch):
    globex = Impersonation("kc:carol", ("copilot:tenant:globex",), "globex", "carol")
    monkeypatch.setattr(observability, "current_identity", lambda write: globex)
    with pytest.raises(ToolError, match="forbidden"):
        await observability.query_metrics("acme-prod", "checkout-api", "memory_usage")
    query, unit, _ = observability.build_query("acme-prod", "checkout-api", "memory_usage")
    assert 'namespace="acme-prod"' in query and "checkout-api-[a-z0-9]+-[a-z0-9]+" in query and unit == "MiB"
    with pytest.raises(ToolError, match="not_found"):
        observability.build_query("acme-prod", "checkout-api", "rate(up[5m])")


# ------------------------------------------------------------------------------------ agent side


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (ToolException('forbidden: pods is forbidden: User "kc:carol" cannot list'), ForbiddenError),
        (ToolException('not_found: deployments.apps "x" not found'), NotFoundError),
        (ToolException("unavailable: kubernetes API 503"), TransientBackendError),
        (ConnectionError("All connection attempts failed"), TransientBackendError),
        (BaseExceptionGroup("tg", [ToolException("forbidden: nope")]), ForbiddenError),
    ],
)
def test_backend_error_mapping(raised, expected):
    assert isinstance(map_error(raised), expected)


async def test_verify_requires_stability():
    """A pod that looks healthy and then restarts must not count as resolved."""
    from copilot.graph.gate import make_verify
    from tests.helpers import make_settings

    class Flapping:
        def __init__(self) -> None:
            self.n = 0

        async def list_pods(self, namespace: str, deployment: str) -> list[dict[str, Any]]:
            self.n += 1
            return [{"name": "p", "status": "Running", "ready": "1/1", "restarts": self.n}]  # restarts keep growing

    class Steady:
        async def list_pods(self, namespace: str, deployment: str) -> list[dict[str, Any]]:
            return [{"name": "p", "status": "Running", "ready": "1/1", "restarts": 3}]

    class Runtime:
        def __init__(self, backend: Any) -> None:
            self.context = type("C", (), {"backend": backend})()
            self.stream_writer = None

    settings = make_settings(verify_timeout_s=0.4, verify_stable_s=0.15, verify_interval_s=0.05)
    verify = make_verify(settings)
    state = {
        "alert": {"service": "checkout-api"},
        "proposal": {"action": {"tool": "restart_deployment", "namespace": "acme-prod", "deployment": "checkout-api"}},
    }
    assert (await verify(state, Runtime(Flapping())))["verification"]["resolved"] is False
    assert (await verify(state, Runtime(Steady())))["verification"]["resolved"] is True
