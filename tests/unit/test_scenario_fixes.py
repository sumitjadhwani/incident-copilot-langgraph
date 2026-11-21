"""Each scenario's mock cluster heals for a correct fix and stays broken for a wrong one."""

import pytest

from copilot.context import Actor
from copilot.graph.gate import parse_action
from copilot.scenarios import list_scenarios, load_scenario
from copilot.tools.mock_backend import ScenarioBackend

ACTOR = Actor("u", "tester", "acme")

GOOD = {
    "oomkilled_loop": {
        "tool": "patch_resources",
        "deployment": "checkout-api",
        "container": "app",
        "memory_limit": "512Mi",
    },
    "high_5xx_latency": {"tool": "scale_deployment", "deployment": "inventory-api", "replicas": 4},
    "db_unreachable": {"tool": "scale_deployment", "deployment": "catalog-db", "replicas": 1},
    "bad_env_var": {
        "tool": "set_env",
        "deployment": "notification-svc",
        "container": "app",
        "name": "SMTP_PORT",
        "value": "587",
    },
    "bad_image_tag": {"tool": "rollback_deployment", "deployment": "search-api", "to_revision": 22},
}
WRONG = {
    "oomkilled_loop": {"tool": "restart_deployment", "deployment": "checkout-api"},
    "high_5xx_latency": {"tool": "restart_deployment", "deployment": "orders-api"},
    "db_unreachable": {"tool": "restart_deployment", "deployment": "catalog-api"},
    "bad_env_var": {
        "tool": "patch_resources",
        "deployment": "notification-svc",
        "container": "app",
        "memory_limit": "512Mi",
    },
    "bad_image_tag": {"tool": "restart_deployment", "deployment": "search-api"},
}


async def unhealthy(backend: ScenarioBackend, namespace: str, deployments: list[str]) -> list[str]:
    bad = []
    for d in deployments:
        pods = await backend.list_pods(namespace, d)
        if not pods:
            bad.append(f"{d}: no pods")
        for p in pods:
            ready, total = p["ready"].split("/")
            if p["status"] != "Running" or ready != total:
                bad.append(p["name"])
    return bad


def test_every_actionable_scenario_has_a_known_fix():
    for sid in list_scenarios():
        s = load_scenario(sid)
        assert bool(s.expected.acceptable_actions) == (sid in GOOD), sid


@pytest.mark.parametrize("sid", sorted(GOOD))
async def test_good_fix_heals(sid):
    s = load_scenario(sid)
    backend = ScenarioBackend(s)
    ns = s.alert.namespace
    targets = sorted({s.alert.service, s.expected.action_target})
    assert await unhealthy(backend, ns, targets), "scenario should start broken"

    action = parse_action({"namespace": ns, **GOOD[sid]})
    assert action.tool in s.expected.acceptable_actions
    await backend.apply_action(action, ACTOR)
    assert await unhealthy(backend, ns, targets) == []


@pytest.mark.parametrize("sid", sorted(WRONG))
async def test_wrong_fix_does_not_heal(sid):
    s = load_scenario(sid)
    backend = ScenarioBackend(s)
    ns = s.alert.namespace
    await backend.apply_action(parse_action({"namespace": ns, **WRONG[sid]}), ACTOR)
    assert await unhealthy(backend, ns, sorted({s.alert.service, s.expected.action_target}))


async def test_writes_do_not_leak_between_backends():
    s = load_scenario("bad_env_var")
    first = ScenarioBackend(s)
    await first.apply_action(parse_action({"namespace": "acme-prod", **GOOD["bad_env_var"]}), ACTOR)
    second = ScenarioBackend(s)
    assert await unhealthy(second, "acme-prod", ["notification-svc"])


def test_false_alarm_starts_healthy():
    s = load_scenario("false_alarm_memory")
    assert s.expected.acceptable_actions == []
    pods = s.namespaces["acme-prod"].pods
    assert all(p.status == "Running" and p.restarts == 0 for p in pods)
