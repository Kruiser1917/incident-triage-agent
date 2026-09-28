"""Workspace for one case: metric series, log lines and deploys under construction.

Scenarios write into a ``CaseBuilder``; rendering turns it into the fixture files. Timestamps
are integer epoch seconds (milliseconds for logs) to keep all arithmetic exact.
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Callable
from dataclasses import dataclass

from triage.simulator import catalog
from triage.simulator.clock import MINUTE, iso_ms, iso_s
from triage.simulator.rng import Rng
from triage.simulator.schemas import ChangeType, LogLevel, LogSource, MetricName, Topology
from triage.simulator.spec import NoiseLevel

METRICS_LOOKBACK_S = 180 * MINUTE
LOGS_LOOKBACK_S = 60 * MINUTE
POST_ALERT_S = 5 * MINUTE  # the investigation starts five minutes after the alert fires
STEP_S = 60

UNITS: dict[MetricName, str] = {
    "cpu": "percent",
    "memory": "MiB",
    "error_rate": "percent",
    "latency_p95": "ms",
    "db_connections": "connections",
    "disk_usage": "percent",
    "rps": "req/s",
}
DECIMALS: dict[MetricName, int] = {
    "cpu": 1,
    "memory": 0,
    "error_rate": 3,
    "latency_p95": 1,
    "db_connections": 0,
    "disk_usage": 2,
    "rps": 1,
}
UPPER_BOUND: dict[MetricName, float] = {"cpu": 100.0, "error_rate": 100.0, "disk_usage": 100.0}


@dataclass(frozen=True)
class DeployIntent:
    ts: int
    service: str
    change_type: ChangeType
    description: str
    author: str


@dataclass(frozen=True)
class DeployRecord:
    ts: int
    service: str
    change_type: ChangeType
    description: str
    author: str
    version: str
    previous_version: str


@dataclass(frozen=True)
class PodGeneration:
    since: int
    replica_set: str
    pods: tuple[str, ...]


@dataclass(frozen=True)
class LogRecord:
    ts_ms: int
    seq: int
    service: str
    pod: str
    level: LogLevel
    source: LogSource
    msg: str


class CaseBuilder:
    def __init__(
        self, case_id: str, topology: Topology, rng: Rng, alert_ts: int, noise: NoiseLevel
    ) -> None:
        self.case_id = case_id
        self.topology = topology
        self.rng = rng
        self.alert_ts = alert_ts
        self.noise = noise
        self.now_ts = alert_ts + POST_ALERT_S
        first = (alert_ts - METRICS_LOOKBACK_S) // STEP_S * STEP_S
        self.times: tuple[int, ...] = tuple(
            range(first, self.now_ts // STEP_S * STEP_S + 1, STEP_S)
        )
        self.logs_start = alert_ts - LOGS_LOOKBACK_S

        self.series: dict[str, dict[MetricName, list[float | None]]] = {}
        self.logs: list[LogRecord] = []
        self.intents: list[DeployIntent] = []
        self.quiet_windows: list[tuple[str, int, int]] = []
        self.deploys: tuple[DeployRecord, ...] = ()
        self.base_versions: dict[str, str] = {}
        self._record_of_intent: dict[int, DeployRecord] = {}
        self._generations: dict[str, list[PodGeneration]] = {}
        self._pod_ips: dict[str, str] = {}
        self._pick_rng = rng.derive("pod-pick")
        for name in topology.services:
            self._generations[name] = [self._new_generation(name, since=0)]

    # --- deploys -----------------------------------------------------------------------

    def plan_deploy(self, ts: int, service: str, change_type: ChangeType, description: str) -> int:
        """Register a deploy; versions are assigned later by ``finalize_deploys``."""
        if self.deploys:
            raise RuntimeError("deploys are already finalized")
        author = self.rng.derive("author", len(self.intents)).choice(catalog.AUTHORS)
        self.intents.append(DeployIntent(ts, service, change_type, description, author))
        return len(self.intents) - 1

    def keep_quiet(self, service: str, start: int, end: int) -> None:
        """Forbid background deploys of ``service`` in [start, end]."""
        self.quiet_windows.append((service, start, end))

    def is_quiet(self, service: str, ts: int) -> bool:
        return any(s == service and start <= ts <= end for s, start, end in self.quiet_windows)

    def finalize_deploys(self, base_versions: dict[str, str], base_config: dict[str, int]) -> None:
        """Walk deploys in time order and assign version / previous_version per service."""
        self.base_versions = dict(base_versions)
        current = dict(base_versions)
        config_rev = dict(base_config)
        order = sorted(
            range(len(self.intents)), key=lambda i: (self.intents[i].ts, self.intents[i].service, i)
        )
        records = []
        for index in order:
            intent = self.intents[index]
            if intent.change_type == "code":
                previous = current[intent.service]
                version = _bump(previous, self.rng.derive("bump", index))
                current[intent.service] = version
            else:
                config_rev[intent.service] += 1
                version = f"cfg-{config_rev[intent.service]:04d}"
                previous = f"cfg-{config_rev[intent.service] - 1:04d}"
            record = DeployRecord(
                intent.ts,
                intent.service,
                intent.change_type,
                intent.description,
                intent.author,
                version,
                previous,
            )
            records.append(record)
            self._record_of_intent[index] = record
            # Code deploys roll out new pods; config changes are hot-reloaded in place.
            if (
                intent.change_type == "code"
                and self.topology.services[intent.service].kind == "service"
            ):
                self._generations[intent.service].append(
                    self._new_generation(intent.service, since=intent.ts)
                )
        self.deploys = tuple(records)

    def deploy(self, handle: int) -> DeployRecord:
        return self._record_of_intent[handle]

    def app_version_at(self, service: str, ts: int) -> str:
        version = self.base_versions.get(service, "v1.0.0")
        for record in self.deploys:
            if record.service == service and record.change_type == "code" and record.ts <= ts:
                version = record.version
        return version

    # --- pods --------------------------------------------------------------------------

    def _new_generation(self, service: str, since: int) -> PodGeneration:
        spec = self.topology.services[service]
        rng = self.rng.derive("pods", service, since)
        if spec.kind != "service":
            return PodGeneration(since, service, (f"{service}-0",))
        replica_set = f"{service}-{rng.token(10, catalog.K8S_ALPHABET)}"
        pods = tuple(
            f"{replica_set}-{rng.token(5, catalog.K8S_ALPHABET)}" for _ in range(spec.replicas)
        )
        return PodGeneration(since, replica_set, pods)

    def generation_at(self, service: str, ts: int) -> PodGeneration:
        generations = self._generations[service]
        index = bisect.bisect_right([g.since for g in generations], ts) - 1
        return generations[max(index, 0)]

    def generations(self, service: str) -> tuple[PodGeneration, ...]:
        return tuple(self._generations[service])

    def pods_at(self, service: str, ts: int) -> tuple[str, ...]:
        return self.generation_at(service, ts).pods

    def pod_ip(self, pod: str) -> str:
        if pod not in self._pod_ips:
            rng = self.rng.derive("ip", pod)
            self._pod_ips[pod] = f"10.0.{rng.randint(1, 30)}.{rng.randint(2, 250)}"
        return self._pod_ips[pod]

    # --- metrics -------------------------------------------------------------------------

    def index(self, ts: int) -> int:
        return (ts - self.times[0]) // STEP_S

    def indices(self, start: int, end: int) -> range:
        """Indices of samples with start <= t < end."""
        lo = max(0, -(-(start - self.times[0]) // STEP_S))
        hi = min(len(self.times), -(-(end - self.times[0]) // STEP_S))
        return range(lo, max(lo, hi))

    def has(self, service: str, metric: MetricName) -> bool:
        return metric in self.series.get(service, {})

    def values(self, service: str, metric: MetricName) -> list[float | None]:
        return self.series[service][metric]

    def set_series(self, service: str, metric: MetricName, values: list[float | None]) -> None:
        if len(values) != len(self.times):
            raise ValueError(f"{service}/{metric}: {len(values)} values for {len(self.times)} ts")
        self.series.setdefault(service, {})[metric] = values

    def update(
        self,
        service: str,
        metric: MetricName,
        start: int,
        end: int,
        fn: Callable[[int, float], float],
    ) -> None:
        """Apply ``fn(ts, value)`` to samples in [start, end); no-op for missing series."""
        if not self.has(service, metric):
            return
        values = self.values(service, metric)
        for i in self.indices(start, end):
            value = values[i]
            if value is not None:
                values[i] = fn(self.times[i], value)

    def add(self, service: str, metric: MetricName, start: int, end: int, delta: float) -> None:
        self.update(service, metric, start, end, lambda _, v: v + delta)

    def scale(self, service: str, metric: MetricName, start: int, end: int, factor: float) -> None:
        self.update(service, metric, start, end, lambda _, v: v * factor)

    def floor(self, service: str, metric: MetricName, start: int, end: int, minimum: float) -> None:
        self.update(service, metric, start, end, lambda _, v: max(v, minimum))

    def value_at(self, service: str, metric: MetricName, ts: int) -> float | None:
        if not self.has(service, metric):
            return None
        i = self.index(ts)
        return self.values(service, metric)[i] if 0 <= i < len(self.times) else None

    def drop_series(self, service: str, metric: MetricName) -> None:
        self.series.get(service, {}).pop(metric, None)

    def gap(self, service: str, metric: MetricName, start: int, end: int) -> None:
        if not self.has(service, metric):
            return
        values = self.values(service, metric)
        for i in self.indices(start, end):
            values[i] = None

    # --- logs --------------------------------------------------------------------------

    def log(
        self,
        ts_ms: int,
        service: str,
        level: LogLevel,
        msg: str,
        *,
        source: LogSource = "app",
        pod: str | None = None,
    ) -> None:
        """Add a log line; lines outside the log window are dropped silently."""
        if not self.logs_start * 1000 <= ts_ms <= self.now_ts * 1000:
            return
        if pod is None:
            pod = self._pick_rng.choice(self.pods_at(service, ts_ms // 1000))
        self.logs.append(LogRecord(ts_ms, len(self.logs), service, pod, level, source, msg))

    def drop_logs(self, service: str) -> None:
        """Remove every line of ``service`` (a broken log pipeline)."""
        self.logs = [record for record in self.logs if record.service != service]

    # --- rendering -----------------------------------------------------------------------

    def render_logs(self) -> str:
        lines = []
        for record in sorted(self.logs, key=lambda r: (r.ts_ms, r.seq)):
            line = {
                "ts": iso_ms(record.ts_ms),
                "service": record.service,
                "pod": record.pod,
                "level": record.level,
                "source": record.source,
                "msg": record.msg,
            }
            lines.append(json.dumps(line, sort_keys=True))
        return "\n".join(lines) + "\n"

    def render_metrics(self) -> str:
        blocks = []
        for service in sorted(self.series):
            rows = []
            for metric in sorted(self.series[service]):
                series = {
                    "unit": _unit(service, metric),
                    "values": [_round(metric, v) for v in self.values(service, metric)],
                }
                rows.append(f'      "{metric}": {json.dumps(series, sort_keys=True)}')
            blocks.append(f'    "{service}": {{\n' + ",\n".join(rows) + "\n    }")
        return (
            "{\n"
            '  "series": {\n' + ",\n".join(blocks) + "\n  },\n"
            f'  "start": "{iso_s(self.times[0])}",\n'
            f'  "step_s": {STEP_S}\n'
            "}\n"
        )

    def render_deploys(self) -> str:
        entries = [
            {
                "ts": iso_s(r.ts),
                "service": r.service,
                "change_type": r.change_type,
                "version": r.version,
                "previous_version": r.previous_version,
                "description": r.description,
                "author": r.author,
            }
            for r in self.deploys
        ]
        return json.dumps(entries, indent=2, sort_keys=True) + "\n"


def _bump(version: str, rng: Rng) -> str:
    major, minor, patch = (int(part) for part in version.lstrip("v").split("."))
    if rng.chance(0.25):
        return f"v{major}.{minor + 1}.0"
    return f"v{major}.{minor}.{patch + 1}"


def _unit(service: str, metric: MetricName) -> str:
    if metric == "rps" and service == "postgres":
        return "queries/s"
    if metric == "rps" and service == "redis":
        return "ops/s"
    if metric == "latency_p95" and service == "postgres":
        return "ms (query)"
    return UNITS[metric]


def _round(metric: MetricName, value: float | None) -> float | int | None:
    if value is None:
        return None
    value = max(0.0, min(value, UPPER_BOUND.get(metric, value)))
    decimals = DECIMALS[metric]
    return round(value) if decimals == 0 else round(value, decimals)
