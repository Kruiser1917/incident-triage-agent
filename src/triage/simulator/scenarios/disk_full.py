"""disk_full: a volume fills up and writes start failing.

Flavors:
- postgres_wal: WAL archiving to object storage fails (403 from S3), so WAL segments pile up
  on the database volume. At 100% PostgreSQL PANICs, restarts into recovery, and every
  client sees "the database system is in recovery mode".
- debug_logs: someone set LOG_LEVEL=debug on a service days ago; its local volume fills with
  logs until audit/temp writes fail with "No space left on device".
Alerts are either the early DiskUsageHigh warning (before writes fail) or 5xx at a consumer.
"""

from __future__ import annotations

from collections.abc import Callable

from triage.simulator import alerts, background, catalog, herrings
from triage.simulator.builder import CaseBuilder
from triage.simulator.clock import HOUR, MINUTE, from_epoch_s, relative
from triage.simulator.rng import Rng
from triage.simulator.scenarios.base import ScenarioResult, common_tags, degrade_observability
from triage.simulator.scenarios.common import burst, propagate
from triage.simulator.schemas import Action, Expected, LogLevel, ToolName
from triage.simulator.spec import CaseSpec, RedHerring

WAL = "postgres_wal"
DEBUG = "debug_logs"
DEBUG_CHANGE = "Set LOG_LEVEL=debug (was info) to investigate statement export issues"

VARIANTS = (
    CaseSpec("disk_full", 1, "easy", "dev", "postgres", "DiskUsageHigh", 14,
             severity="warning", flavor=WAL),
    CaseSpec("disk_full", 2, "easy", "test", "ledger-service", "DiskUsageHigh", 11,
             severity="warning", flavor=DEBUG, release_hours_ago=30.0),
    CaseSpec("disk_full", 3, "easy", "test", "orders-service", "HighErrorRate", 16,
             flavor=DEBUG, release_hours_ago=26.0),
    CaseSpec("disk_full", 4, "easy", "test", "payments-service", "DiskUsageHigh", 10,
             severity="warning", noise="medium", flavor=DEBUG, release_hours_ago=40.0),
    CaseSpec("disk_full", 5, "medium", "dev", "postgres", "HighErrorRate", 13,
             alert_service="orders-service", noise="medium", flavor=WAL,
             red_herrings=(RedHerring("recent_deploy", "orders-service"),)),
    CaseSpec("disk_full", 6, "medium", "test", "ledger-service", "HighErrorRate", 19,
             noise="medium", flavor=DEBUG, release_hours_ago=22.0,
             red_herrings=(RedHerring("redis_latency_spike"),)),
    CaseSpec("disk_full", 7, "medium", "test", "postgres", "HighErrorRate", 12,
             alert_service="ledger-service", noise="medium", flavor=WAL, metric_gaps=True),
    CaseSpec("disk_full", 8, "medium", "test", "orders-service", "HighErrorRate", 18,
             alert_service="api-gateway", noise="high", flavor=DEBUG, release_hours_ago=34.0),
    CaseSpec("disk_full", 9, "hard", "dev", "postgres", "HighErrorRate", 15,
             alert_service="api-gateway", noise="high", flavor=WAL,
             missing_metrics=("disk_usage",),
             red_herrings=(RedHerring("recent_deploy", "ledger-service"),)),
    CaseSpec("disk_full", 10, "hard", "test", "payments-service", "HighErrorRate", 20,
             alert_service="api-gateway", noise="high", flavor=DEBUG, release_hours_ago=72.0,
             red_herrings=(RedHerring("provider_429_burst"),)),
)  # fmt: skip

WRITE_FAILURES = {
    "ledger-service": (
        'msg="request failed" method=POST path=/v1/entries status=500 '
        'err="write audit record: write /var/lib/ledger/audit/entries.log: '
        'no space left on device"'
    ),
    "orders-service": (
        "orders.api: POST /api/v1/orders failed: OSError: [Errno 28] No space left on device: "
        "'/var/lib/orders/exports/tmp-export.csv'"
    ),
    "payments-service": (
        "c.a.p.audit.AuditWriter : failed to append audit record: "
        "java.io.IOException: No space left on device"
    ),
}


def build(b: CaseBuilder, spec: CaseSpec) -> ScenarioResult:
    if spec.flavor == WAL:
        return _postgres_wal(b, spec)
    return _debug_logs(b, spec)


def _fill(b: CaseBuilder, service: str, start: int, spec: CaseSpec, rng: Rng) -> int:
    """Grow the disk linearly from its normal level at ``start`` so that it crosses the alert
    threshold exactly when the alert condition starts to hold: 90% for the early DiskUsageHigh
    warning, 100% (writes fail) for error alerts. Returns when the volume is, or will be, full."""
    onset_ts = alerts.onset(b, spec.alert, rng)
    target = alerts.DISK_THRESHOLD_PCT if spec.alert == "DiskUsageHigh" else 100.0
    level = b.value_at(service, "disk_usage", b.times[0]) or 60.0
    slope = (target - level) / max(onset_ts - start, MINUTE)
    b.update(
        service, "disk_usage", max(start, b.times[0]), b.now_ts + 1,
        lambda ts, _: min(100.0, level + slope * (ts - start)),
    )  # fmt: skip
    return start + int((100.0 - level) / slope)


def _postgres_wal(b: CaseBuilder, spec: CaseSpec) -> ScenarioResult:
    rng = b.rng.derive("disk-wal")
    planned = [herrings.plan(b, h, spec.service) for h in spec.red_herrings]
    background.finalize(b)
    failing_since = b.alert_ts - rng.randint(8, 30) * HOUR
    full_ts = _fill(b, "postgres", failing_since, spec, rng)

    segment = rng.randint(0x3000, 0x7000)
    for ts in range(b.logs_start, min(full_ts, b.now_ts), rng.randint(90, 150)):
        name = f"000000010000004A0000{segment:04X}"
        b.log(ts * 1000, "postgres", "ERROR", (
            f"ERROR: {_wal_g_time(ts)} failed to upload 'wal_005/{name}.br': AccessDenied: "
            "Access Denied status code: 403"
        ))  # fmt: skip
        b.log(ts * 1000 + 40, "postgres", "INFO", (
            "LOG:  archive command failed with exit code 1"
            f"\nDETAIL:  The failed archive command was: wal-g wal-push pg_wal/{name}"
        ))  # fmt: skip
        if rng.chance(0.3):
            b.log(ts * 1000 + 90, "postgres", "WARN", (
                f'WARNING:  archiving write-ahead log file "{name}" failed too many times, '
                "will try again later"
            ))  # fmt: skip
        segment += 1

    must_call: tuple[ToolName, ...] = ("search_logs",)
    if full_ts <= b.now_ts:  # the volume is full: PANIC, crash loop, clients fail
        _postgres_crash_loop(b, full_ts, rng)
        error_pct = rng.uniform(40, 80)
        for client in ("orders-service", "ledger-service"):
            b.floor(client, "error_rate", full_ts, b.now_ts + 1, error_pct)
            burst(b, client, full_ts, b.now_ts, 3.0, rng, _recovery_mode_line(client, b))
            propagate(b, client, full_ts, b.now_ts, error_pct, "server_error", rng, depth=1)
        b.scale("postgres", "rps", full_ts, b.now_ts + 1, 0.05)
        b.scale("postgres", "db_connections", full_ts, b.now_ts + 1, 0.1)
    herring_notes = tuple(herrings.apply(b, p) for p in planned)
    alerts.enforce_condition(b, spec.alert, spec.alerting_service)
    degrade_observability(b, spec, ())
    if "disk_usage" not in spec.missing_metrics:
        must_call = (*must_call, "get_metrics")

    state = (
        "the volume is full, PostgreSQL PANICs and crash-loops in recovery mode"
        if full_ts <= b.now_ts
        else "the volume is above 90% and will fill up soon"
    )
    expected = Expected(
        root_cause=(
            "WAL archiving from postgres fails (wal-g upload to object storage returns 403 "
            f"AccessDenied since about {relative(failing_since, b.alert_ts)}), so WAL segments "
            f"accumulate in pg_wal and fill the database disk; {state}"
        ),
        root_cause_keywords=("disk", "WAL"),
        root_service="postgres",
        should_escalate=False,
        must_call_tools=must_call,
        acceptable_actions=(
            Action(type="clear_disk", target="postgres"),
            Action(type="escalate", target="postgres"),
        ),
        red_herrings=herring_notes,
    )
    alert = alerts.build_alert(b, spec.alert, spec.alerting_service, spec.severity)
    return ScenarioResult(alert, expected, common_tags(spec))


def _wal_g_time(ts: int) -> str:
    return from_epoch_s(ts).strftime("%Y/%m/%d %H:%M:%S.000000")


def _postgres_crash_loop(b: CaseBuilder, full_ts: int, rng: Rng) -> None:
    t = full_ts
    while t < b.now_ts:
        pid = rng.randint(2000, 9000)
        lines: tuple[tuple[LogLevel, str], ...] = (
            (
                "FATAL",
                f'PANIC:  could not write to file "pg_wal/xlogtemp.{pid}": '
                "No space left on device",
            ),
            ("INFO", f"LOG:  server process (PID {pid}) was terminated by signal 6: Aborted"),
            ("INFO", "LOG:  terminating any other active server processes"),
            ("INFO", "LOG:  all server processes terminated; reinitializing"),
            ("INFO", "LOG:  database system was interrupted; last known up at recovery start"),
        )  # fmt: skip
        for offset, (level, msg) in enumerate(lines):
            b.log(t * 1000 + offset * 150, "postgres", level, msg)
        for _ in range(rng.randint(3, 6)):
            b.log(
                t * 1000 + rng.randint(1000, 40000), "postgres", "FATAL",
                "FATAL:  the database system is in recovery mode",
            )  # fmt: skip
        t += rng.randint(40, 90)


def _recovery_mode_line(client: str, b: CaseBuilder) -> Callable[[Rng], catalog.Message]:
    ip = b.pod_ip("postgres-0")

    def make(rng: Rng) -> catalog.Message:
        if client == "orders-service":
            return "ERROR", (
                "orders.api: POST /api/v1/orders failed: sqlalchemy.exc.OperationalError: "
                f'(psycopg2.OperationalError) connection to server at "postgres" ({ip}), '
                "port 5432 failed: FATAL:  the database system is in recovery mode"
            )
        return "ERROR", (
            f'msg="request failed" method=POST path=/v1/entries status=500 '
            'err="FATAL: the database system is in recovery mode (SQLSTATE 57P03)" '
            f"request_id={catalog.request_id(rng)}"
        )

    return make


def _debug_logs(b: CaseBuilder, spec: CaseSpec) -> ScenarioResult:
    service = spec.service
    rng = b.rng.derive("disk-debug")
    change_ts = b.alert_ts - int(spec.release_hours_ago * HOUR) - rng.randint(0, 50) * MINUTE
    change_handle = b.plan_deploy(change_ts, service, "config", DEBUG_CHANGE)
    b.keep_quiet(service, change_ts - HOUR, change_ts + HOUR)
    planned = [herrings.plan(b, h, service) for h in spec.red_herrings]
    background.finalize(b)

    full_ts = _fill(b, service, change_ts, spec, rng)
    burst(b, service, b.logs_start, b.now_ts, 6.0, rng, _debug_line(service))
    if full_ts <= b.now_ts:
        error_pct = rng.uniform(20, 45)
        b.floor(service, "error_rate", full_ts, b.now_ts + 1, error_pct)
        failure = WRITE_FAILURES[service]
        burst(b, service, full_ts, b.now_ts, 4.0, rng, lambda _: ("ERROR", failure))
        propagate(b, service, full_ts, b.now_ts, error_pct, "server_error", rng)
    herring_notes = tuple(herrings.apply(b, p) for p in planned)
    alerts.enforce_condition(b, spec.alert, spec.alerting_service)
    degrade_observability(b, spec, ())

    change = b.deploy(change_handle)
    state = (
        "writes now fail with 'No space left on device'"
        if full_ts <= b.now_ts
        else "the volume is above 90% and will fill up soon"
    )
    expected = Expected(
        root_cause=(
            f"config change {change.version} set LOG_LEVEL=debug on {service} "
            f"{relative(change.ts, b.alert_ts)}; debug logs fill its local disk; {state}"
        ),
        root_cause_keywords=("disk", "debug"),
        root_service=service,
        should_escalate=False,
        must_call_tools=("get_deploys", "get_metrics", "search_logs"),
        acceptable_actions=(
            Action(type="clear_disk", target=service),
            Action(type="revert_config", target=service, to_version=change.previous_version),
        ),
        red_herrings=herring_notes,
    )
    alert = alerts.build_alert(b, spec.alert, spec.alerting_service, spec.severity)
    tags = common_tags(spec) + (("old_change",) if spec.release_hours_ago >= 24 else ())
    return ScenarioResult(alert, expected, tags)


def _debug_line(service: str) -> Callable[[Rng], catalog.Message]:
    def make(rng: Rng) -> catalog.Message:
        if service == "payments-service":
            return "DEBUG", (
                f"c.a.p.risk.RiskScorer : score inputs paymentId={catalog.payment_id(rng)} "
                f"features={rng.randint(40, 90)} bytes={rng.randint(8000, 64000)}"
            )
        if service == "orders-service":
            return "DEBUG", (
                f"orders.db: SELECT orders.id, orders.status FROM orders WHERE orders.id = "
                f"'{catalog.order_id(rng)}' -- {rng.randint(1, 9)} rows in {rng.randint(1, 9)}ms"
            )
        return "DEBUG", (
            f'msg="statement export chunk" account={catalog.account_id(rng)} '
            f"rows={rng.randint(500, 5000)} bytes={rng.randint(40000, 400000)}"
        )

    return make
