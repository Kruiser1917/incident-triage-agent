"""Class invariants: every oom_kill case must actually show an OOM story."""

from datetime import timedelta

import pytest

from tests.simulator.conftest import REPO_ROOT
from triage.simulator.clock import epoch_s
from triage.simulator.scenarios.oom_kill import VARIANTS
from triage.simulator.show import Case, load_case

CASES = [spec.case_id for spec in VARIANTS]


@pytest.fixture(scope="module", params=CASES)
def case(request: pytest.FixtureRequest) -> Case:
    return load_case(REPO_ROOT, request.param)


def _root(case: Case) -> str:
    assert case.golden.expected.root_service is not None
    return case.golden.expected.root_service


def test_root_service_is_oomkilled(case: Case) -> None:
    root = _root(case)
    events = [
        line
        for line in case.logs
        if line.service == root and line.source == "k8s" and "OOMKilled (exit code 137)" in line.msg
    ]
    assert events, "no OOMKilled events for the root service in the log window"


def test_memory_reaches_limit_and_resets(case: Case) -> None:
    root = _root(case)
    series = case.metrics.series.get(root, {}).get("memory")
    if series is None:
        pytest.skip("memory series intentionally missing in this case")
    limit = case.topology.services[root].memory_limit_mib
    assert limit is not None
    values = [v for v in series.values if v is not None]
    peak_index = values.index(max(values))
    assert max(values) >= 0.95 * limit
    assert min(values[peak_index:] or [limit]) < 0.8 * limit or peak_index == len(values) - 1


def test_leak_cases_have_the_release_and_cache_cases_have_none(case: Case) -> None:
    root = _root(case)
    alert_ts = epoch_s(case.alert.alerts[0].startsAt)
    code_deploys = [d for d in case.deploys if d.service == root and d.change_type == "code"]
    if "no_recent_deploy" in case.golden.tags:
        recent = [d for d in code_deploys if epoch_s(d.ts) > alert_ts - 5 * 24 * 3600]
        assert not recent
        assert any("evictions=0" in line.msg for line in case.logs)
    else:
        version = case.golden.expected.root_cause_keywords[1]
        release = [d for d in code_deploys if d.version == version]
        assert release and release[0].ts < case.alert.alerts[0].startsAt - timedelta(minutes=30)


def test_alert_condition_holds_at_fire_time(case: Case) -> None:
    alert = case.alert.alerts[0]
    service = alert.labels["service"]
    index = (epoch_s(alert.startsAt) - epoch_s(case.metrics.start)) // case.metrics.step_s
    metrics = case.metrics.series[service]
    if alert.labels["alertname"] == "HighErrorRate" and "error_rate" in metrics:
        value = metrics["error_rate"].values[index]
        assert value is not None and value > 5.0
    if alert.labels["alertname"] == "HighLatencyP95" and "latency_p95" in metrics:
        value = metrics["latency_p95"].values[index]
        assert value is not None and value > 1000.0


def test_red_herring_tag_matches_notes(case: Case) -> None:
    has_tag = "red_herring" in case.golden.tags
    assert has_tag == bool(case.golden.expected.red_herrings)
