"""dependency_cascade: the root cause sits in a dependency; the alert fires at a consumer.

The loudest errors are at the consumer, so the agent must walk the topology down to the
first unhealthy dependency. Flavors:
- redis_maxmemory: a config change set maxmemory-policy to noeviction; once redis reaches
  maxmemory, writes fail ("OOM command not allowed"), so payments cannot store idempotency
  keys. Revert the config (or escalate).
- postgres_lock: a long-running transaction (a manual psql session left idle in transaction)
  holds row locks on account_balances; ledger writes wait and hit their statement timeout.
  Escalate to a human: the fix is terminating that session, outside the action allowlist.
- ledger_regression: a ledger release recalculates balances with a heavy aggregate on every
  write; ledger slows down and payments time out calling it. Roll back ledger.
"""

from __future__ import annotations

from triage.simulator import alerts, background, catalog, herrings
from triage.simulator.builder import CaseBuilder
from triage.simulator.clock import HOUR, MINUTE, relative
from triage.simulator.rng import Rng
from triage.simulator.scenarios.base import ScenarioResult, common_tags, degrade_observability
from triage.simulator.scenarios.common import burst, propagate
from triage.simulator.schemas import Action, Expected
from triage.simulator.spec import CaseSpec, RedHerring

REDIS = "redis_maxmemory"
LOCK = "postgres_lock"
REGRESSION = "ledger_regression"
REDIS_CHANGE = "Set maxmemory-policy noeviction (was allkeys-lru)"
SLOW_RELEASE = "Recalculate running balance on every entry post"

VARIANTS = (
    CaseSpec("dependency_cascade", 1, "easy", "dev", "ledger-service", "HighErrorRate", 14,
             alert_service="payments-service", flavor=REGRESSION),
    CaseSpec("dependency_cascade", 2, "easy", "test", "redis", "HighErrorRate", 11,
             alert_service="payments-service", flavor=REDIS, release_hours_ago=30.0),
    CaseSpec("dependency_cascade", 3, "easy", "test", "postgres", "HighLatencyP95", 16,
             alert_service="ledger-service", severity="warning", flavor=LOCK),
    CaseSpec("dependency_cascade", 4, "easy", "test", "ledger-service", "HighLatencyP95", 10,
             alert_service="payments-service", severity="warning", noise="medium",
             flavor=REGRESSION),
    CaseSpec("dependency_cascade", 5, "medium", "dev", "redis", "HighErrorRate", 13,
             alert_service="api-gateway", noise="medium", flavor=REDIS, release_hours_ago=20.0,
             red_herrings=(RedHerring("recent_deploy", "payments-service"),)),
    CaseSpec("dependency_cascade", 6, "medium", "test", "postgres", "HighErrorRate", 19,
             alert_service="payments-service", noise="medium", flavor=LOCK,
             red_herrings=(RedHerring("provider_429_burst"),)),
    CaseSpec("dependency_cascade", 7, "medium", "test", "ledger-service", "HighErrorRate", 12,
             alert_service="api-gateway", noise="medium", flavor=REGRESSION, metric_gaps=True),
    CaseSpec("dependency_cascade", 8, "medium", "test", "redis", "HighErrorRate", 18,
             alert_service="api-gateway", noise="high", flavor=REDIS, release_hours_ago=44.0),
    CaseSpec("dependency_cascade", 9, "hard", "dev", "postgres", "HighErrorRate", 15,
             alert_service="api-gateway", noise="high", flavor=LOCK,
             red_herrings=(RedHerring("recent_deploy", "ledger-service"),)),
    CaseSpec("dependency_cascade", 10, "hard", "test", "ledger-service", "HighErrorRate", 20,
             alert_service="api-gateway", noise="high", flavor=REGRESSION,
             red_herrings=(RedHerring("same_service_config"), RedHerring("redis_latency_spike"))),
)  # fmt: skip


def build(b: CaseBuilder, spec: CaseSpec) -> ScenarioResult:
    return {REDIS: _redis, LOCK: _lock, REGRESSION: _regression}[spec.flavor](b, spec)


def _redis(b: CaseBuilder, spec: CaseSpec) -> ScenarioResult:
    rng = b.rng.derive("cascade-redis")
    change_ts = b.alert_ts - int(spec.release_hours_ago * HOUR) - rng.randint(0, 50) * MINUTE
    change_handle = b.plan_deploy(change_ts, "redis", "config", REDIS_CHANGE)
    planned = [herrings.plan(b, h, "payments-service") for h in spec.red_herrings]
    background.finalize(b)

    limit = float(b.topology.services["redis"].memory_limit_mib or 4096)
    maxmemory = limit * 0.9
    full_ts = alerts.onset(b, spec.alert, rng)
    start_level = maxmemory * rng.uniform(0.9, 0.95)
    first = b.times[0]

    def fill(ts: int, _: float) -> float:
        progress = (ts - first) / (full_ts - first)
        return min(maxmemory, start_level + (maxmemory - start_level) * progress)

    b.update("redis", "memory", first, b.now_ts + 1, fill)
    end = b.now_ts + 1
    error_pct = rng.uniform(15, 35)
    b.floor("payments-service", "error_rate", full_ts, end, error_pct)
    burst(b, "payments-service", full_ts, b.now_ts, 4.0, rng, _idempotency_oom)
    burst(b, "orders-service", full_ts, b.now_ts, 3.0, rng, _cache_write_oom)
    b.scale("orders-service", "latency_p95", full_ts, end, rng.uniform(1.4, 1.8))
    b.scale("postgres", "rps", full_ts, end, 1.3)  # cache misses fall through to the DB
    propagate(b, "payments-service", full_ts, b.now_ts, error_pct, "server_error", rng)
    notes = tuple(herrings.apply(b, p) for p in planned)
    alerts.enforce_condition(b, spec.alert, spec.alerting_service)
    degrade_observability(b, spec, ())

    change = b.deploy(change_handle)
    expected = Expected(
        root_cause=(
            f"redis reached maxmemory after config change {change.version} "
            f"({relative(change.ts, b.alert_ts)}: '{change.description}'); with noeviction it "
            "rejects writes (OOM command not allowed), so payments-service cannot store "
            "idempotency keys and fails requests"
        ),
        root_cause_keywords=("maxmemory", "redis"),
        root_service="redis",
        should_escalate=False,
        must_call_tools=("get_topology", "search_logs"),
        acceptable_actions=(
            Action(type="revert_config", target="redis", to_version=change.previous_version),
            Action(type="escalate", target="redis"),
        ),
        red_herrings=notes,
    )
    alert = alerts.build_alert(b, spec.alert, spec.alerting_service, spec.severity)
    return ScenarioResult(alert, expected, (*common_tags(spec), "cascade"))


def _lock(b: CaseBuilder, spec: CaseSpec) -> ScenarioResult:
    rng = b.rng.derive("cascade-lock")
    planned = [herrings.plan(b, h, "ledger-service") for h in spec.red_herrings]
    background.finalize(b)

    # Latency pins at the lock right away; errors follow a minute later (statement timeout).
    lag = 0 if spec.alert == "HighLatencyP95" else MINUTE
    lock_ts = alerts.onset(b, spec.alert, rng) - lag
    holder = rng.randint(20000, 29999)
    engineer = rng.choice(catalog.AUTHORS[:-1])
    b.log(
        (lock_ts - rng.randint(60, 180)) * 1000, "postgres", "INFO",
        f"LOG:  connection authorized: user={engineer} database=ledger application_name=psql",
    )  # fmt: skip
    end = b.now_ts + 1
    error_pct = rng.uniform(20, 40)
    b.floor("ledger-service", "latency_p95", lock_ts, end, 5000 * rng.uniform(0.95, 1.0))
    b.floor("ledger-service", "error_rate", lock_ts + MINUTE, end, error_pct)
    b.scale("postgres", "rps", lock_ts, end, 0.7)

    def lock_wait(r: Rng) -> catalog.Message:
        waiter = r.randint(30000, 39999)
        return "INFO", (
            f"LOG:  process {waiter} still waiting for ShareLock on transaction "
            f"{r.randint(91000000, 91999999)} after 1000.{r.randint(100, 999)} ms"
            f"\nDETAIL:  Process holding the lock: {holder}. Wait queue: {waiter}, "
            f"{r.randint(30000, 39999)}.\nCONTEXT:  while updating tuple "
            f'({r.randint(1000, 9000)},{r.randint(1, 60)}) in relation "account_balances"'
            "\nSTATEMENT:  UPDATE account_balances SET balance = balance + $1 WHERE account_id = $2"
        )

    def statement_timeout(r: Rng) -> catalog.Message:
        return "ERROR", (
            f'msg="request failed" method=POST path=/v1/entries status=500 '
            f"duration_ms={r.randint(5000, 5010)} "
            'err="ERROR: canceling statement due to statement timeout (SQLSTATE 57014)" '
            f"request_id={catalog.request_id(r)}"
        )

    burst(b, "postgres", lock_ts, b.now_ts, 3.0, rng, lock_wait)
    burst(b, "ledger-service", lock_ts + MINUTE, b.now_ts, 3.0, rng, statement_timeout)
    propagate(b, "ledger-service", lock_ts + MINUTE, b.now_ts, error_pct, "timeout", rng)
    notes = tuple(herrings.apply(b, p) for p in planned)
    alerts.enforce_condition(b, spec.alert, spec.alerting_service)
    degrade_observability(b, spec, ())

    expected = Expected(
        root_cause=(
            f"a long-running transaction on postgres (PID {holder}) holds row locks on "
            f"account_balances since about {relative(lock_ts, b.alert_ts)}; ledger-service "
            "writes wait on the lock and hit their statement timeout, and payments-service "
            "times out calling ledger"
        ),
        root_cause_keywords=("lock", "account_balances"),
        root_service="postgres",
        should_escalate=False,
        must_call_tools=("get_topology", "search_logs"),
        acceptable_actions=(Action(type="escalate", target="postgres"),),
        red_herrings=notes,
    )
    alert = alerts.build_alert(b, spec.alert, spec.alerting_service, spec.severity)
    return ScenarioResult(alert, expected, (*common_tags(spec), "cascade"))


def _regression(b: CaseBuilder, spec: CaseSpec) -> ScenarioResult:
    rng = b.rng.derive("cascade-regression")
    slow_from = alerts.onset(b, spec.alert, rng)
    release_ts = slow_from - rng.randint(40, 90)  # new pods take over within a minute
    release_handle = b.plan_deploy(release_ts, "ledger-service", "code", SLOW_RELEASE)
    b.keep_quiet("ledger-service", release_ts - 2 * HOUR, b.now_ts)
    planned = [herrings.plan(b, h, "ledger-service") for h in spec.red_herrings]
    background.finalize(b)

    end = b.now_ts + 1
    b.floor("ledger-service", "latency_p95", slow_from, end, rng.uniform(3000, 6000))
    b.floor("payments-service", "latency_p95", slow_from, end, rng.uniform(1500, 2500))
    b.add("ledger-service", "cpu", slow_from, end, 20)
    b.scale("postgres", "cpu", slow_from, end, 1.8)
    b.floor("postgres", "latency_p95", slow_from, end, rng.uniform(1500, 3000))
    error_pct = rng.uniform(10, 25)
    b.floor("ledger-service", "error_rate", slow_from, end, rng.uniform(1, 3))
    burst(b, "ledger-service", slow_from, b.now_ts, 3.0, rng, _slow_entry)
    burst(b, "postgres", slow_from, b.now_ts, 2.0, rng, _slow_aggregate)
    propagate(b, "ledger-service", slow_from, b.now_ts, error_pct, "timeout", rng)
    notes = tuple(herrings.apply(b, p) for p in planned)
    alerts.enforce_condition(b, spec.alert, spec.alerting_service)
    degrade_observability(b, spec, ())

    release = b.deploy(release_handle)
    expected = Expected(
        root_cause=(
            f"ledger-service {release.version} (deployed {relative(release.ts, b.alert_ts)}: "
            f"'{release.description}') runs a heavy SUM over entries on every write; ledger "
            "slows to seconds per request and payments-service times out calling it"
        ),
        root_cause_keywords=(release.version, "slow"),
        root_service="ledger-service",
        should_escalate=False,
        must_call_tools=("get_deploys", "get_topology", "search_logs"),
        acceptable_actions=(
            Action(type="rollback", target="ledger-service", to_version=release.previous_version),
        ),
        red_herrings=notes,
    )
    alert = alerts.build_alert(b, spec.alert, spec.alerting_service, spec.severity)
    return ScenarioResult(alert, expected, (*common_tags(spec), "cascade"))


def _idempotency_oom(rng: Rng) -> catalog.Message:
    return "ERROR", (
        "c.a.p.idempotency.IdempotencyStore : failed to store idempotency key "
        f"idk_{rng.token(12, catalog.ID_ALPHABET)}: OOM command not allowed when used memory > "
        "'maxmemory'."
    )


def _cache_write_oom(rng: Rng) -> catalog.Message:
    return "WARN", (
        f"orders.cache: redis SET order:{catalog.order_id(rng)} failed: OOM command not allowed "
        "when used memory > 'maxmemory'.; continuing without cache"
    )


def _slow_entry(rng: Rng) -> catalog.Message:
    return "WARN", (
        f'msg="slow request" method=POST path=/v1/entries duration_ms={rng.randint(2500, 6500)} '
        f"request_id={catalog.request_id(rng)}"
    )


def _slow_aggregate(rng: Rng) -> catalog.Message:
    return "INFO", (
        f"LOG:  duration: {rng.uniform(1500, 4200):.3f} ms  statement: SELECT sum(amount) "
        "FROM entries WHERE account_id = $1 AND posted_at <= $2"
    )
