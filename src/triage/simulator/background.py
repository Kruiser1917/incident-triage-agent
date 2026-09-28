"""The "normal day" every case starts from: traffic-shaped metrics, routine logs, low-level
background errors and unrelated deploys. Without this noise the dataset would test nothing:
the incident signal would be the only signal.
"""

from __future__ import annotations

from triage.simulator import catalog
from triage.simulator.builder import CaseBuilder
from triage.simulator.clock import DAY, HOUR, MINUTE
from triage.simulator.rng import Rng
from triage.simulator.schemas import MetricName, ServiceSpec

# Relative traffic by UTC hour: night trough, lunch and evening peaks.
HOURLY_TRAFFIC = (
    0.32, 0.26, 0.22, 0.20, 0.21, 0.27, 0.40, 0.58, 0.76, 0.88, 0.95, 0.99,
    1.00, 0.98, 0.95, 0.93, 0.92, 0.94, 0.97, 1.00, 0.93, 0.78, 0.60, 0.44,
)  # fmt: skip

BASE_RPS = {
    "api-gateway": 420.0,
    "orders-service": 160.0,
    "payments-service": 85.0,
    "ledger-service": 130.0,
    "postgres": 950.0,
    "redis": 2600.0,
    "payment-provider-api": 60.0,
}
BASE_LATENCY_MS = {
    "api-gateway": 95.0,
    "orders-service": 70.0,
    "payments-service": 180.0,
    "ledger-service": 25.0,
    "postgres": 6.0,
    "redis": 1.2,
    "payment-provider-api": 150.0,
}
CPU_PROFILE = {  # (idle percent, extra percent at peak traffic)
    "java": (12.0, 38.0),
    "python": (8.0, 45.0),
    "go": (4.0, 28.0),
    "postgres": (10.0, 35.0),
    "redis": (3.0, 12.0),
}
MEMORY_FRACTION = {"java": 0.50, "python": 0.40, "go": 0.33, "postgres": 0.62, "redis": 0.45}

LOG_LINES_PER_MIN = {  # a sample of routine lines, not the full firehose
    "api-gateway": 1.8,
    "orders-service": 1.3,
    "payments-service": 1.3,
    "ledger-service": 1.0,
    "postgres": 0.3,
    "redis": 0.1,
}
NOISE_MULTIPLIER = {"low": 0.5, "medium": 1.0, "high": 1.8}
ISSUE_CHANCE_PER_MIN = {"low": 0.03, "medium": 0.06, "high": 0.15}
ERROR_BLIP_CHANCE = {"low": 0.01, "medium": 0.02, "high": 0.05}

BASE_VERSION_RANGES = {  # (major, minor range) per service
    "api-gateway": (1, 28, 34),
    "orders-service": (2, 10, 16),
    "payments-service": (3, 5, 9),
    "ledger-service": (1, 19, 24),
}
RECENT_DEPLOY_BLACKOUT_S = 2 * HOUR  # background deploys never land this close to the alert
DEPLOY_HISTORY_S = 7 * DAY


def traffic(ts: int) -> float:
    """Relative traffic (0.2..1.0) at ``ts``, linearly interpolated between hours."""
    seconds = ts % DAY
    hour = seconds // HOUR
    fraction = (seconds % HOUR) / HOUR
    here = HOURLY_TRAFFIC[hour]
    return here + (HOURLY_TRAFFIC[(hour + 1) % 24] - here) * fraction


def finalize(b: CaseBuilder) -> None:
    """Plan background deploys, assign versions, then fill metrics and logs.

    Call after the scenario and red herrings have registered their deploys and quiet
    windows, and before they overlay incident signals.
    """
    _plan_background_deploys(b)
    rng = b.rng.derive("versions")
    base_versions = {}
    for service, (major, low, high) in BASE_VERSION_RANGES.items():
        base_versions[service] = f"v{major}.{rng.randint(low, high)}.{rng.randint(0, 3)}"
    base_config = {service: rng.randint(100, 400) for service in b.topology.services}
    b.finalize_deploys(base_versions, base_config)
    _fill_metrics(b)
    _fill_logs(b)
    _rollout_logs(b)


# --- deploys -----------------------------------------------------------------------------


def _plan_background_deploys(b: CaseBuilder) -> None:
    rng = b.rng.derive("background-deploys")
    services = tuple(BASE_VERSION_RANGES)
    first_day = (b.alert_ts - DEPLOY_HISTORY_S) // DAY * DAY
    for _ in range(rng.randint(8, 14)):
        service = rng.choice(services)
        hour = rng.choice((9, 10, 11, 13, 14, 15, 16, 17, 20))  # mostly business hours
        ts = first_day + rng.randint(0, 7) * DAY + hour * HOUR + rng.randint(0, 59) * MINUTE
        if ts > b.alert_ts - RECENT_DEPLOY_BLACKOUT_S or b.is_quiet(service, ts):
            continue
        if rng.chance(0.2):
            b.plan_deploy(
                ts, service, "config", rng.choice(catalog.HARMLESS_CONFIG_CHANGES[service])
            )
        else:
            b.plan_deploy(ts, service, "code", rng.choice(catalog.CODE_CHANGES[service]))


def _rollout_logs(b: CaseBuilder) -> None:
    """Rollout events and start-up lines for code deploys; reload lines for config changes."""
    for record in b.deploys:
        spec = b.topology.services[record.service]
        if spec.kind != "service":
            continue
        if record.change_type == "config":
            rng = b.rng.derive("reload", record.service, record.ts)
            for pod in b.pods_at(record.service, record.ts):
                msg = catalog.config_reload_line(record.service, record.version, record.description)
                b.log(
                    record.ts * 1000 + rng.randint(500, 8000), record.service, "INFO", msg, pod=pod
                )
            continue
        old = b.generation_at(record.service, record.ts - 1)
        new = b.generation_at(record.service, record.ts)
        rng = b.rng.derive("rollout", record.service, record.ts)
        t = record.ts * 1000
        b.log(
            t, record.service, "INFO",
            f"Scaled up replica set {new.replica_set} to {spec.replicas}",
            source="k8s", pod=new.pods[0],
        )  # fmt: skip
        version = b.app_version_at(record.service, record.ts)
        for pod in new.pods:
            t += rng.randint(4000, 15000)
            b.log(t, record.service, "INFO", f"Created pod: {pod}", source="k8s", pod=pod)
            for level, msg in catalog.startup_lines(record.service, version, rng):
                t += rng.randint(300, 4000)
                b.log(t, record.service, level, msg, pod=pod)
        b.log(
            t + rng.randint(2000, 9000), record.service, "INFO",
            f"Scaled down replica set {old.replica_set} to 0",
            source="k8s", pod=old.pods[0],
        )  # fmt: skip


# --- metrics -----------------------------------------------------------------------------


def _fill_metrics(b: CaseBuilder) -> None:
    for name, spec in b.topology.services.items():
        for metric in spec.metrics:
            if name == "postgres" and metric == "db_connections":
                continue  # derived from client pools below
            rng = b.rng.derive("metric", name, metric)
            b.set_series(name, metric, _baseline(b, name, spec, metric, rng))
    if (
        b.topology.services.get("postgres")
        and "db_connections" in b.topology.services["postgres"].metrics
    ):
        b.set_series("postgres", "db_connections", _postgres_connections(b))


def _baseline(
    b: CaseBuilder, name: str, spec: ServiceSpec, metric: MetricName, rng: Rng
) -> list[float | None]:
    values: list[float | None] = []
    if metric == "rps":
        for ts in b.times:
            values.append(BASE_RPS[name] * traffic(ts) * (1 + rng.uniform(-0.04, 0.04)))
    elif metric == "latency_p95":
        base = BASE_LATENCY_MS[name] * rng.uniform(0.9, 1.1)
        for ts in b.times:
            load = 0.85 + 0.3 * traffic(ts)
            values.append(base * load * (1 + rng.uniform(-0.06, 0.06)))
    elif metric == "error_rate":
        base = rng.uniform(0.05, 0.2)
        blip = ERROR_BLIP_CHANCE[b.noise]
        for _ in b.times:
            value = base * (1 + rng.uniform(-0.5, 0.5))
            values.append(value * rng.uniform(4, 9) if rng.chance(blip) else value)
    elif metric == "cpu":
        idle, load = CPU_PROFILE[spec.runtime]
        for ts in b.times:
            values.append(idle + load * traffic(ts) + rng.uniform(-1.5, 1.5))
    elif metric == "memory":
        values.extend(_memory(b, spec, rng))
    elif metric == "db_connections":
        pool = (spec.db_pool_max or 10) * spec.replicas
        for ts in b.times:
            values.append(pool * (0.12 + 0.22 * traffic(ts)) * (1 + rng.uniform(-0.1, 0.1)))
    elif metric == "disk_usage":
        level = rng.uniform(55, 68) if spec.kind == "datastore" else rng.uniform(22, 45)
        for _ in b.times:
            level += 0.004 + rng.uniform(-0.01, 0.01)
            values.append(level)
    return values


def _memory(b: CaseBuilder, spec: ServiceSpec, rng: Rng) -> list[float | None]:
    limit = float(spec.memory_limit_mib or 1024)
    base = limit * MEMORY_FRACTION[spec.runtime] * rng.uniform(0.95, 1.05)
    values: list[float | None] = []
    level = base
    for _ in b.times:
        if spec.runtime == "java":  # heap sawtooth: steady allocation, periodic GC
            level += limit * rng.uniform(0.004, 0.008)
            if level > base + limit * 0.08:
                level = base + limit * rng.uniform(0.0, 0.01)
        else:  # small bounded random walk
            level += limit * rng.uniform(-0.002, 0.002)
            level = min(max(level, base * 0.97), base * 1.03)
        values.append(level)
    return values


def _postgres_connections(b: CaseBuilder) -> list[float | None]:
    rng = b.rng.derive("metric", "postgres", "db_connections")
    clients = [s for s in ("orders-service", "ledger-service") if b.has(s, "db_connections")]
    values: list[float | None] = []
    for i in range(len(b.times)):
        total = 14.0  # replication, monitoring, admin sessions
        for client in clients:
            in_use = b.values(client, "db_connections")[i] or 0.0
            total += in_use * 1.3  # pools keep idle connections open too
        values.append(total + rng.uniform(-2, 2))
    return values


# --- logs --------------------------------------------------------------------------------


def _fill_logs(b: CaseBuilder) -> None:
    multiplier = NOISE_MULTIPLIER[b.noise]
    issue_chance = ISSUE_CHANCE_PER_MIN[b.noise]
    first_minute = b.logs_start - b.logs_start % MINUTE
    for service, rate in LOG_LINES_PER_MIN.items():
        normal = catalog.NORMAL[service]
        issue = catalog.ISSUES.get(service)
        rng = b.rng.derive("logs", service)
        for minute in range(first_minute, b.now_ts, MINUTE):
            count = int(rate * multiplier + rng.random())
            for _ in range(count):
                level, msg = normal(rng)
                b.log(minute * 1000 + rng.randint(0, 59999), service, level, msg)
            if issue is not None and rng.chance(issue_chance):
                level, msg = issue(rng)
                b.log(minute * 1000 + rng.randint(0, 59999), service, level, msg)
