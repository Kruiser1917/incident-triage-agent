"""Shared plumbing for scenario modules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from triage.simulator.alerts import AlertOutcome
from triage.simulator.builder import CaseBuilder
from triage.simulator.clock import MINUTE
from triage.simulator.schemas import Expected
from triage.simulator.spec import CaseSpec


@dataclass(frozen=True)
class ScenarioResult:
    alert: AlertOutcome
    expected: Expected
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Scenario:
    variants: tuple[CaseSpec, ...]
    build: Callable[[CaseBuilder, CaseSpec], ScenarioResult]


def degrade_observability(b: CaseBuilder, spec: CaseSpec, restart_times: tuple[int, ...]) -> None:
    """Apply the 'incomplete data' axis: scrape gaps around restarts and dropped series."""
    if spec.metric_gaps:
        rng = b.rng.derive("gaps")
        metrics = b.series.get(spec.service, {})
        for ts in restart_times:
            if rng.chance(0.6):
                for metric in sorted(metrics):
                    b.gap(spec.service, metric, ts, ts + rng.randint(1, 2) * MINUTE)
        if b.has(spec.service, "memory"):
            start = b.alert_ts - rng.randint(40, 120) * MINUTE
            b.gap(spec.service, "memory", start, start + rng.randint(4, 6) * MINUTE)
    for metric in spec.missing_metrics:
        b.drop_series(spec.service, metric)


def common_tags(spec: CaseSpec) -> tuple[str, ...]:
    tags = []
    if spec.red_herrings:
        tags.append("red_herring")
    if spec.noise == "high":
        tags.append("noisy_logs")
    if spec.missing_metrics:
        tags.append("missing_metrics")
    if spec.metric_gaps:
        tags.append("metric_gaps")
    if spec.alerting_service != spec.service:
        tags.append("alert_on_caller")
    return tuple(tags)
