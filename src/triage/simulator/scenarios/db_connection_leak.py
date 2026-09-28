"""db_connection_leak: a release leaks database connections until the pool is exhausted.

Connections are checked out on an error path and never returned, so in-use connections climb
from the release onwards. Once every pod's pool is full, requests queue for a connection and
time out: latency pins at the pool timeout, 5xx rise, callers time out too. Roll back to fix.
"""

from __future__ import annotations

from triage.simulator import alerts, background, catalog, herrings
from triage.simulator.builder import CaseBuilder
from triage.simulator.clock import HOUR, MINUTE, relative
from triage.simulator.rng import Rng
from triage.simulator.scenarios.base import ScenarioResult, common_tags, degrade_observability
from triage.simulator.scenarios.common import burst, propagate
from triage.simulator.schemas import Action, Expected, ToolName
from triage.simulator.spec import CaseSpec, RedHerring

LEAKY_RELEASES = {
    "orders-service": "Move order CSV export to a background worker",
    "ledger-service": "Stream account statements directly from the database",
}
POOL_TIMEOUT_MS = {"orders-service": 30000.0, "ledger-service": 5000.0}

VARIANTS = (
    CaseSpec("db_connection_leak", 1, "easy", "dev", "orders-service", "HighErrorRate", 14,
             release_hours_ago=2.5),
    CaseSpec("db_connection_leak", 2, "easy", "test", "ledger-service", "HighErrorRate", 11,
             release_hours_ago=3.0),
    CaseSpec("db_connection_leak", 3, "easy", "test", "orders-service", "HighLatencyP95", 16,
             severity="warning", release_hours_ago=4.0),
    CaseSpec("db_connection_leak", 4, "easy", "test", "ledger-service", "HighLatencyP95", 10,
             severity="warning", noise="medium", release_hours_ago=2.0),
    CaseSpec("db_connection_leak", 5, "medium", "dev", "ledger-service", "HighErrorRate", 13,
             noise="medium", release_hours_ago=5.0,
             red_herrings=(RedHerring("recent_deploy", "api-gateway"),)),
    CaseSpec("db_connection_leak", 6, "medium", "test", "orders-service", "HighErrorRate", 19,
             noise="medium", release_hours_ago=6.0,
             red_herrings=(RedHerring("redis_latency_spike"),)),
    CaseSpec("db_connection_leak", 7, "medium", "test", "orders-service", "HighErrorRate", 12,
             alert_service="api-gateway", noise="medium", release_hours_ago=3.0),
    CaseSpec("db_connection_leak", 8, "medium", "test", "ledger-service", "HighErrorRate", 17,
             noise="high", release_hours_ago=7.0, metric_gaps=True),
    CaseSpec("db_connection_leak", 9, "hard", "dev", "orders-service", "HighErrorRate", 15,
             noise="high", release_hours_ago=9.0, missing_metrics=("db_connections",),
             red_herrings=(RedHerring("same_service_config"),)),
    CaseSpec("db_connection_leak", 10, "hard", "test", "ledger-service", "HighErrorRate", 20,
             alert_service="payments-service", noise="high", release_hours_ago=5.0,
             red_herrings=(RedHerring("recent_deploy", "payments-service"),
                           RedHerring("provider_429_burst"))),
)  # fmt: skip


def build(b: CaseBuilder, spec: CaseSpec) -> ScenarioResult:
    service = spec.service
    rng = b.rng.derive("db-leak")
    release_ts = b.alert_ts - int(spec.release_hours_ago * HOUR) - rng.randint(0, 30) * MINUTE
    release_handle = b.plan_deploy(release_ts, service, "code", LEAKY_RELEASES[service])
    b.keep_quiet(service, release_ts - HOUR, b.now_ts)
    planned = [herrings.plan(b, h, service) for h in spec.red_herrings]
    background.finalize(b)

    # Leaked connections grow linearly from the release until the pool is full.
    pool_per_pod = b.topology.services[service].db_pool_max or 20
    replicas = b.topology.services[service].replicas
    cap = float(pool_per_pod * replicas)
    saturated_ts = alerts.onset(b, spec.alert, rng)
    normal_at_saturation = b.value_at(service, "db_connections", saturated_ts) or cap * 0.3
    rate = (cap - normal_at_saturation) / max(saturated_ts - release_ts, MINUTE)

    def leaked(ts: int) -> float:
        return max(0.0, min(ts, saturated_ts) - release_ts) * rate

    b.update(
        service, "db_connections", release_ts, b.now_ts + 1, lambda ts, v: min(cap, v + leaked(ts))
    )
    b.floor(service, "db_connections", saturated_ts, b.now_ts + 1, cap)
    b.update("postgres", "db_connections", release_ts, b.now_ts + 1, lambda ts, v: v + leaked(ts))

    # Waiting for a connection shows up in latency before errors do.
    timeout_ms = POOL_TIMEOUT_MS[service]
    squeeze_start = saturated_ts - 20 * MINUTE
    b.update(
        service, "latency_p95", squeeze_start, saturated_ts,
        lambda ts, v: v * (1 + 4 * (ts - squeeze_start) / (20 * MINUTE)),
    )  # fmt: skip
    error_pct = rng.uniform(15, 40)
    b.floor(service, "latency_p95", saturated_ts, b.now_ts + 1, timeout_ms * rng.uniform(0.95, 1.0))
    b.floor(service, "error_rate", saturated_ts, b.now_ts + 1, error_pct)
    b.scale(service, "rps", saturated_ts, b.now_ts + 1, 0.85)

    _pool_logs(b, service, pool_per_pod, squeeze_start, saturated_ts, rng)
    propagate(b, service, saturated_ts, b.now_ts, error_pct, "timeout", rng)
    herring_notes = tuple(herrings.apply(b, p) for p in planned)
    alerts.enforce_condition(b, spec.alert, spec.alerting_service)
    degrade_observability(b, spec, ())

    release = b.deploy(release_handle)
    alert = alerts.build_alert(b, spec.alert, spec.alerting_service, spec.severity)
    must_call: tuple[ToolName, ...] = ("get_deploys", "search_logs")
    if "db_connections" not in spec.missing_metrics:
        must_call = (*must_call, "get_metrics")
    expected = Expected(
        root_cause=(
            f"{service} {release.version} (deployed {relative(release.ts, b.alert_ts)}: "
            f"'{release.description}') leaks database connections; the connection pool "
            f"({pool_per_pod} per pod) is exhausted, so requests time out waiting for a connection"
        ),
        root_cause_keywords=("pool", release.version),
        root_service=service,
        should_escalate=False,
        must_call_tools=must_call,
        acceptable_actions=(
            Action(type="rollback", target=service, to_version=release.previous_version),
            Action(type="restart", target=service),
        ),
        red_herrings=herring_notes,
    )
    return ScenarioResult(alert, expected, common_tags(spec))


def _pool_logs(
    b: CaseBuilder, service: str, pool: int, squeeze_start: int, saturated_ts: int, rng: Rng
) -> None:
    if service == "orders-service":

        def near_limit(r: Rng) -> catalog.Message:
            used = r.randint(pool - 3, pool - 1)
            return "WARN", (
                f"orders.db: connection pool nearly exhausted: checked_out={used} "
                f"size={pool} overflow=0"
            )

        def exhausted(r: Rng) -> catalog.Message:
            return "ERROR", (
                f"orders.api: POST /api/v1/orders failed after {r.randint(30001, 30040)}ms: "
                f"sqlalchemy.exc.TimeoutError: QueuePool limit of size {pool} overflow 0 reached, "
                "connection timed out, timeout 30.00 (Background on this error at: "
                "https://sqlalche.me/e/20/3o7r)"
            )

        def worker_timeout(r: Rng) -> catalog.Message:
            return "ERROR", f"gunicorn.error: WORKER TIMEOUT (pid:{r.randint(20, 400)})"

        burst(b, service, squeeze_start, saturated_ts, 0.4, rng, near_limit)
        burst(b, service, saturated_ts, b.now_ts, 3.0, rng, exhausted)
        burst(b, service, saturated_ts, b.now_ts, 0.6, rng, worker_timeout)
        return

    def pool_stats(r: Rng) -> catalog.Message:
        return "WARN", (
            f'msg="db pool stats" acquired={r.randint(pool - 3, pool)} idle=0 max={pool} '
            f"wait_count={r.randint(5, 60)}"
        )

    def acquire_failed(r: Rng) -> catalog.Message:
        return "ERROR", (
            f'msg="request failed" method=POST path=/v1/entries status=500 '
            f"duration_ms={r.randint(5000, 5012)} "
            'err="failed to acquire connection from pool: context deadline exceeded" '
            f"pool_acquired={pool} pool_max={pool} request_id={catalog.request_id(r)}"
        )

    burst(b, service, squeeze_start, saturated_ts, 0.4, rng, pool_stats)
    burst(b, service, saturated_ts, b.now_ts, 3.0, rng, acquire_failed)
