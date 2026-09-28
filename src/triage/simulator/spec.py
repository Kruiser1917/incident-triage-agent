"""Declarative description of one case: what breaks, where, and how hard it is to see.

Scenario modules list their variants as explicit ``CaseSpec`` tables, so coverage of the
variation axes (service, difficulty, noise, red herrings, missing data, timing) is designed,
not left to chance. Randomness only fills in details: exact numbers, timings, log lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from triage.simulator.schemas import Difficulty, IncidentClass, MetricName, Split

NoiseLevel = Literal["low", "medium", "high"]
AlertKind = Literal["HighErrorRate", "PodRestartsHigh", "HighLatencyP95"]
HerringKind = Literal[
    "recent_deploy", "same_service_config", "redis_latency_spike", "provider_429_burst"
]


@dataclass(frozen=True)
class RedHerring:
    kind: HerringKind
    service: str = ""  # target service for recent_deploy


@dataclass(frozen=True)
class CaseSpec:
    incident_class: IncidentClass
    number: int
    difficulty: Difficulty
    split: Split
    service: str  # root-cause service
    alert: AlertKind
    alert_hour: int  # UTC hour when the alert fires; drives the traffic level
    severity: Literal["critical", "warning"] = "critical"
    alert_service: str = ""  # service the alert fires on; defaults to the root service
    noise: NoiseLevel = "low"
    red_herrings: tuple[RedHerring, ...] = ()
    missing_metrics: tuple[MetricName, ...] = ()  # series dropped for the root service
    metric_gaps: bool = False  # scrape gaps in the root service's series
    flavor: str = ""  # class-specific variant of the root cause
    release_hours_ago: float = 0.0  # for release-induced incidents

    @property
    def case_id(self) -> str:
        return f"{self.incident_class}-{self.number:03d}"

    @property
    def alerting_service(self) -> str:
        return self.alert_service or self.service
