import pytest

from copilot.scenarios import load_scenario
from copilot.tools.backend import NotFoundError
from copilot.tools.mock_backend import ScenarioBackend

NS = "acme-prod"
POD = "checkout-api-7d9f8b6c5-x2k4p"


@pytest.fixture
def backend():
    return ScenarioBackend(load_scenario("oomkilled_loop"))


async def test_list_pods_filters_by_deployment(backend):
    pods = await backend.list_pods(NS, deployment="checkout-api")
    assert {p["deployment"] for p in pods} == {"checkout-api"}
    assert len(pods) == 3
    crashing = next(p for p in pods if p["name"] == POD)
    assert crashing["status"] == "CrashLoopBackOff"
    assert crashing["ready"] == "0/1"


async def test_describe_pod_merges_container_spec(backend):
    pod = await backend.describe_pod(NS, POD)
    app = pod["containers"][0]
    assert app["last_state"]["reason"] == "OOMKilled"
    assert app["last_state"]["exit_code"] == 137
    assert app["resources"]["limits"]["memory"] == "256Mi"
    assert app["image"].endswith(":2.14.0")


async def test_logs_previous_and_tail(backend):
    current = await backend.get_pod_logs(NS, POD)
    previous = await backend.get_pod_logs(NS, POD, previous=True, tail=2)
    assert "starting checkout-api" in current["lines"][0]
    assert len(previous["lines"]) == 2
    assert previous["truncated"] is True
    assert "gc pause" in previous["lines"][-1]


async def test_events_filter_by_object(backend):
    events = await backend.get_events(NS, involved_object=f"pod/{POD}")
    assert events and all(POD in e["object"] for e in events)


async def test_query_metrics_window_and_summary(backend):
    result = await backend.query_metrics(NS, "checkout-api", "memory_usage", window_minutes=15)
    assert all(p["minutes_ago"] <= 15 for p in result["points"])
    assert result["summary"]["max"] == 254
    assert result["unit"] == "MiB"


@pytest.mark.parametrize(
    "call",
    [
        lambda b: b.list_pods("nope"),
        lambda b: b.describe_pod(NS, "nope"),
        lambda b: b.get_pod_logs(NS, POD, container="sidecar"),
        lambda b: b.describe_deployment(NS, "nope"),
        lambda b: b.query_metrics(NS, "nope", "memory_usage"),
        lambda b: b.query_metrics("nope", "checkout-api", "memory_usage"),
        lambda b: b.query_metrics(NS, "checkout-api", "disk_usage"),
    ],
)
async def test_not_found_errors(backend, call):
    with pytest.raises(NotFoundError):
        await call(backend)
