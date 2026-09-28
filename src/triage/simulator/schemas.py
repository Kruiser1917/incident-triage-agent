"""Pydantic contracts for simulated fixtures and the golden dataset.

The simulator writes these files; tools (phase 2) and evals read them. One schema module
means a format change breaks loudly in one place instead of drifting between writers
and readers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MetricName = Literal[
    "cpu", "memory", "error_rate", "latency_p95", "db_connections", "disk_usage", "rps"
]
LogLevel = Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
LogSource = Literal["app", "k8s"]
ChangeType = Literal["code", "config"]
ActionType = Literal[
    "rollback", "restart", "scale", "rotate_cert", "revert_config", "clear_disk", "escalate"
]
ToolName = Literal[
    "search_logs", "get_metrics", "get_deploys", "get_topology", "search_runbooks", "propose_action"
]
IncidentClass = Literal[
    "oom_kill",
    "db_connection_leak",
    "bad_deploy",
    "disk_full",
    "upstream_throttling",
    "cert_expired",
    "bad_config",
    "dependency_cascade",
    "trap",
    "prompt_injection",
]
Difficulty = Literal["easy", "medium", "hard"]
Split = Literal["dev", "test"]
ServiceKind = Literal["service", "datastore", "external"]
Runtime = Literal["java", "python", "go", "postgres", "redis", "external"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Topology (fixtures/topology.yaml) -----------------------------------------------


class ServiceSpec(_Strict):
    kind: ServiceKind
    runtime: Runtime
    owner: str
    description: str
    replicas: int = Field(ge=1)
    depends_on: tuple[str, ...] = ()
    metrics: tuple[MetricName, ...]
    memory_limit_mib: int | None = Field(default=None, gt=0)
    db_pool_max: int | None = Field(default=None, gt=0)
    max_connections: int | None = Field(default=None, gt=0)


class Topology(_Strict):
    services: dict[str, ServiceSpec]

    @model_validator(mode="after")
    def _check_dependencies(self) -> Self:
        for name, spec in self.services.items():
            unknown = sorted(set(spec.depends_on) - self.services.keys())
            if unknown:
                raise ValueError(f"{name} depends on unknown services: {unknown}")
            if name in spec.depends_on:
                raise ValueError(f"{name} depends on itself")
        return self

    def dependents_of(self, service: str) -> tuple[str, ...]:
        """Services that call ``service`` (its upstream callers)."""
        return tuple(name for name, spec in self.services.items() if service in spec.depends_on)


# --- Case fixtures (fixtures/<case_id>/) ----------------------------------------------


class AlertItem(_Strict):
    """One alert in an Alertmanager webhook payload (field names follow Alertmanager)."""

    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str]
    startsAt: datetime
    endsAt: datetime
    generatorURL: str
    fingerprint: str


class AlertmanagerWebhook(_Strict):
    """alert.json: Alertmanager webhook payload, version 4."""

    version: Literal["4"]
    groupKey: str
    truncatedAlerts: int = Field(ge=0)
    status: Literal["firing", "resolved"]
    receiver: str
    groupLabels: dict[str, str]
    commonLabels: dict[str, str]
    commonAnnotations: dict[str, str]
    externalURL: str
    alerts: tuple[AlertItem, ...] = Field(min_length=1)


class LogLine(_Strict):
    """One line of logs.jsonl."""

    ts: datetime
    service: str
    pod: str
    level: LogLevel
    source: LogSource
    msg: str


class MetricSeries(_Strict):
    unit: str
    values: tuple[float | None, ...]  # None = scrape gap


class MetricsFile(_Strict):
    """metrics.json: raw per-minute series; tools summarize them, never return them raw."""

    start: datetime
    step_s: int = Field(gt=0)
    series: dict[str, dict[MetricName, MetricSeries]]


class Deploy(_Strict):
    """One entry of deploys.json. Config changes carry config revisions (cfg-NNNN)."""

    ts: datetime
    service: str
    change_type: ChangeType
    version: str
    previous_version: str
    description: str
    author: str


# --- Golden dataset (evals/datasets/golden.jsonl) -------------------------------------


class Action(_Strict):
    type: ActionType
    target: str
    to_version: str | None = None


class Injection(_Strict):
    """Instruction smuggled into tool output, and the action it tries to trigger."""

    instruction: str
    action: Action


class Expected(_Strict):
    root_cause: str
    root_cause_keywords: tuple[str, ...] = Field(min_length=1)
    root_service: str | None  # None for trap cases: the right answer is to escalate
    should_escalate: bool
    must_call_tools: tuple[ToolName, ...]
    must_not_call_tools: tuple[ToolName, ...] = ()
    acceptable_actions: tuple[Action, ...] = Field(min_length=1)
    red_herrings: tuple[str, ...] = ()
    injection: Injection | None = None


class AlertSummary(_Strict):
    name: str
    service: str
    severity: str
    summary: str


class GoldenCase(_Strict):
    model_config = ConfigDict(
        validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )

    id: str
    incident_class: IncidentClass = Field(alias="class")
    difficulty: Difficulty
    split: Split
    fixture_dir: str
    alert: AlertSummary
    expected: Expected
    tags: tuple[str, ...] = ()
