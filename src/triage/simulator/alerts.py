"""Alertmanager webhook payloads for the alert kinds used by scenarios."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import quote

from triage.simulator.builder import CaseBuilder
from triage.simulator.clock import MINUTE, iso_s
from triage.simulator.rng import Rng
from triage.simulator.schemas import AlertSummary
from triage.simulator.spec import AlertKind

ERROR_RATE_THRESHOLD = 5.0  # percent
LATENCY_THRESHOLD_MS = 1000.0
DISK_THRESHOLD_PCT = 90.0
# The rule's "for" duration: how long the condition must hold before the alert fires.
FOR_SECONDS: dict[AlertKind, int] = {
    "HighErrorRate": 2 * MINUTE,
    "HighLatencyP95": 5 * MINUTE,
    "DiskUsageHigh": 10 * MINUTE,
    "PodRestartsHigh": 0,
}


@dataclass(frozen=True)
class AlertOutcome:
    payload: dict[str, object]
    summary: AlertSummary


def onset(b: CaseBuilder, kind: AlertKind, rng: Rng) -> int:
    """When the alert condition started to hold: the ``for`` duration plus one or two rule
    evaluations before the alert fired. Scenarios anchor the incident's visible start here,
    so an alert never fires long after its condition became true."""
    return b.alert_ts - FOR_SECONDS[kind] - rng.randint(15, 75)


def enforce_condition(b: CaseBuilder, kind: AlertKind, service: str) -> None:
    """Make the metric behind a firing alert actually exceed its threshold for the ``for``
    window, so the alert never contradicts the metrics the agent will look at."""
    rng = b.rng.derive("alert-condition")
    start = b.alert_ts - FOR_SECONDS[kind]
    if kind == "HighErrorRate":
        floor = ERROR_RATE_THRESHOLD * rng.uniform(1.1, 1.5)
        b.floor(service, "error_rate", start, b.alert_ts + 1, floor)
    elif kind == "HighLatencyP95":
        floor = LATENCY_THRESHOLD_MS * rng.uniform(1.05, 1.3)
        b.floor(service, "latency_p95", start, b.alert_ts + 1, floor)
    elif kind == "DiskUsageHigh":
        floor = DISK_THRESHOLD_PCT + rng.uniform(0.5, 2.0)
        b.floor(service, "disk_usage", start, b.alert_ts + 1, floor)


def build_alert(
    b: CaseBuilder, kind: AlertKind, service: str, severity: str, *, restarts: int = 0
) -> AlertOutcome:
    if kind == "HighErrorRate":
        summary = "5xx ratio > 5% for 2m"
        value = b.value_at(service, "error_rate", b.alert_ts)
        description = (
            f"{service} 5xx ratio is {value:.1f}% (threshold 5%)."
            if value is not None
            else f"{service} 5xx ratio is above 5%."
        )
        expr = (
            f'sum(rate(http_requests_total{{service="{service}",code=~"5.."}}[2m])) / '
            f'sum(rate(http_requests_total{{service="{service}"}}[2m])) > 0.05'
        )
    elif kind == "PodRestartsHigh":
        summary = "3+ container restarts in 1h"
        description = f"{restarts} container restarts across {service} pods in the last hour."
        expr = (
            "sum(increase(kube_pod_container_status_restarts_total"
            f'{{namespace="prod",container="{service}"}}[1h])) >= 3'
        )
    elif kind == "DiskUsageHigh":
        summary = "disk usage > 90% for 10m"
        value = b.value_at(service, "disk_usage", b.alert_ts)
        description = (
            f"{service} volume is {value:.1f}% full (threshold 90%)."
            if value is not None
            else f"{service} volume is more than 90% full."
        )
        expr = f'max(disk_used_percent{{namespace="prod",service="{service}"}}) > 90'
    else:
        summary = "p95 latency > 1000ms for 5m"
        value = b.value_at(service, "latency_p95", b.alert_ts)
        description = (
            f"{service} p95 latency is {value:.0f}ms (threshold 1000ms)."
            if value is not None
            else f"{service} p95 latency is above 1000ms."
        )
        expr = (
            "histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket"
            f'{{service="{service}"}}[5m]))) > 1'
        )
    labels = {
        "alertname": kind,
        "namespace": "prod",
        "service": service,
        "severity": severity,
        "team": b.topology.services[service].owner,
    }
    annotations = {
        "summary": summary,
        "description": description,
        "runbook_url": f"https://runbooks.internal/alerts/{kind}",
    }
    fingerprint = hashlib.sha256(json.dumps(labels, sort_keys=True).encode()).hexdigest()[:16]
    alert = {
        "status": "firing",
        "labels": labels,
        "annotations": annotations,
        "startsAt": iso_s(b.alert_ts),
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "http://prometheus.prod.internal:9090/graph?g0.expr=" + quote(expr),
        "fingerprint": fingerprint,
    }
    payload: dict[str, object] = {
        "version": "4",
        "groupKey": f'{{}}:{{alertname="{kind}"}}',
        "truncatedAlerts": 0,
        "status": "firing",
        "receiver": "triage-agent",
        "groupLabels": {"alertname": kind},
        "commonLabels": labels,
        "commonAnnotations": annotations,
        "externalURL": "http://alertmanager.prod.internal:9093",
        "alerts": [alert],
    }
    return AlertOutcome(
        payload, AlertSummary(name=kind, service=service, severity=severity, summary=summary)
    )
