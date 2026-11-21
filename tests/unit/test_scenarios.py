import pytest

from copilot.scenarios import list_scenarios, load_scenario
from copilot.tools.backend import METRIC_NAMES


def test_at_least_one_scenario_exists():
    assert "oomkilled_loop" in list_scenarios()


@pytest.mark.parametrize("scenario_id", list_scenarios())
def test_scenario_is_consistent(scenario_id):
    s = load_scenario(scenario_id)
    assert s.id == scenario_id
    assert s.alert.namespace in s.namespaces, "alert namespace must exist in the snapshot"
    ns = s.namespaces[s.alert.namespace]
    assert s.alert.service in ns.deployments, "alerting service must be a deployment"
    for pod in ns.pods:
        assert pod.deployment in ns.deployments, f"pod {pod.name} references unknown deployment"
    for service, metrics in s.metrics.items():
        for name, series in metrics.items():
            assert name in METRIC_NAMES, f"{service}: unsupported metric {name}"
            offsets = [t for t, _ in series.points]
            assert offsets == sorted(offsets) and all(t <= 0 for t in offsets)


def test_unknown_scenario_lists_available():
    with pytest.raises(FileNotFoundError, match="oomkilled_loop"):
        load_scenario("does_not_exist")
