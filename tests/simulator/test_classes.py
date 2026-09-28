"""Invariants for every case: the story the answer key tells must be visible in the fixtures."""

import pytest

from tests.simulator.conftest import REPO_ROOT
from triage.simulator.alerts import FOR_SECONDS
from triage.simulator.clock import epoch_s
from triage.simulator.generate import all_specs
from triage.simulator.schemas import MetricName, Topology
from triage.simulator.show import Case, load_case

CASE_IDS = [spec.case_id for spec in all_specs()]

# Text that must appear in some log line of each class (the class "signature").
SIGNATURES = {
    "oom_kill": ("OOMKilled (exit code 137)",),
    "db_connection_leak": ("QueuePool limit", "failed to acquire connection from pool"),
    "upstream_throttling": ("429 Too Many Requests",),
    "cert_expired": ("expired",),
    "dependency_cascade": (
        "OOM command not allowed",
        "still waiting for ShareLock",
        "slow request",
    ),
}


@pytest.fixture(scope="module", params=CASE_IDS)
def case(request: pytest.FixtureRequest) -> Case:
    return load_case(REPO_ROOT, request.param)


def _downstream(topology: Topology, service: str) -> set[str]:
    """All services reachable from ``service`` by following depends_on."""
    seen: set[str] = set()
    stack = [service]
    while stack:
        for dependency in topology.services[stack.pop()].depends_on:
            if dependency not in seen:
                seen.add(dependency)
                stack.append(dependency)
    return seen


ALERT_CONDITIONS: dict[str, tuple[MetricName, float]] = {
    "HighErrorRate": ("error_rate", 5.0),
    "HighLatencyP95": ("latency_p95", 1000.0),
    "DiskUsageHigh": ("disk_usage", 90.0),
}


def test_alert_matches_metrics_at_fire_time(case: Case) -> None:
    alert = case.alert.alerts[0]
    condition = ALERT_CONDITIONS.get(alert.labels["alertname"])
    series = case.metrics.series.get(alert.labels["service"], {})
    if condition is None or condition[0] not in series:
        return  # restart-count alerts, or the metric is missing on purpose
    metric, threshold = condition
    index = (epoch_s(alert.startsAt) - epoch_s(case.metrics.start)) // case.metrics.step_s
    value = series[metric].values[index]
    assert value is not None and value > threshold


def test_alert_fires_promptly(case: Case) -> None:
    """The condition must not have held long before the alert fired: a "for 2m" rule that
    was true for 40 minutes would have fired 38 minutes earlier."""
    alert = case.alert.alerts[0]
    kind = alert.labels["alertname"]
    condition = ALERT_CONDITIONS.get(kind)
    series = case.metrics.series.get(alert.labels["service"], {})
    if condition is None or condition[0] not in series:
        return
    metric, threshold = condition
    values = series[metric].values
    step = case.metrics.step_s
    index = (epoch_s(alert.startsAt) - epoch_s(case.metrics.start)) // step
    first = index
    while first > 0 and (values[first - 1] or 0.0) > threshold:
        first -= 1
    held_for = (index - first) * step
    for_seconds = {str(name): seconds for name, seconds in FOR_SECONDS.items()}[kind]
    assert held_for <= for_seconds + 3 * 60, f"condition held {held_for}s before firing"


def test_root_is_downstream_of_the_alerting_service(case: Case) -> None:
    root = case.golden.expected.root_service
    alerting = case.golden.alert.service
    if root is None or root == alerting:
        return
    assert root in _downstream(case.topology, alerting)
    assert "alert_on_caller" in case.golden.tags


def test_escalation_fields_are_consistent(case: Case) -> None:
    expected = case.golden.expected
    if case.golden.incident_class == "trap":
        assert expected.should_escalate and expected.root_service is None
        assert {a.type for a in expected.acceptable_actions} == {"escalate"}
    else:
        assert not expected.should_escalate and expected.root_service is not None


def test_injected_action_is_never_acceptable(case: Case) -> None:
    injection = case.golden.expected.injection
    if case.golden.incident_class != "prompt_injection":
        assert injection is None
        return
    assert injection is not None
    planted = (injection.action.type, injection.action.target)
    assert planted not in {(a.type, a.target) for a in case.golden.expected.acceptable_actions}


def test_red_herring_tag_matches_notes(case: Case) -> None:
    assert ("red_herring" in case.golden.tags) == bool(case.golden.expected.red_herrings)


def test_class_signature_is_in_the_logs(case: Case) -> None:
    signatures = SIGNATURES.get(case.golden.incident_class)
    if signatures is None:
        return
    text = "\n".join(line.msg for line in case.logs).lower()
    assert any(signature.lower() in text for signature in signatures)


def test_deploy_classes_show_the_culprit(case: Case) -> None:
    cls = case.golden.incident_class
    keywords = case.golden.expected.root_cause_keywords
    root = case.golden.expected.root_service
    if cls == "bad_deploy":
        release = [d for d in case.deploys if d.version == keywords[0] and d.service == root]
        assert release and release[0].change_type == "code"
        assert any(keywords[1] in line.msg for line in case.logs if line.service == root)
    if cls == "bad_config":
        change = [d for d in case.deploys if d.version == keywords[0] and d.service == root]
        assert change and change[0].change_type == "config"


def test_debug_logs_flavor_actually_logs_debug(case: Case) -> None:
    if case.golden.incident_class == "disk_full" and "debug" in case.golden.expected.root_cause:
        root = case.golden.expected.root_service
        assert any(line.level == "DEBUG" and line.service == root for line in case.logs)
