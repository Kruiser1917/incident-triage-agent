"""bad_config: a configuration change (not code) breaks a service right after it is applied.

Config changes are hot-reloaded, so there is no rollout: the only traces are the config
revision in the deploy history, the "applied config revision" log line, and symptoms that
start seconds later. Some symptoms imitate other classes on purpose (a tiny DB pool looks like
a connection leak); the discriminator is the config change. Revert the revision to fix.
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

# (config change as recorded in the deploy history, setting name used as answer keyword)
BAD_CHANGES = {
    "payments-service": ("Set PROVIDER_READ_TIMEOUT_MS=200 (was 2000)", "PROVIDER_READ_TIMEOUT_MS"),
    "orders-service": ("Set DB_POOL_SIZE=2 (was 20)", "DB_POOL_SIZE"),
    "ledger-service": ("Set DB_STATEMENT_TIMEOUT=50ms (was 5s)", "DB_STATEMENT_TIMEOUT"),
    "api-gateway": ("Set PAYMENTS_ROUTE_TIMEOUT=150ms (was 15s)", "PAYMENTS_ROUTE_TIMEOUT"),
}

VARIANTS = (
    CaseSpec("bad_config", 1, "easy", "dev", "payments-service", "HighErrorRate", 14),
    CaseSpec("bad_config", 2, "easy", "test", "orders-service", "HighErrorRate", 11),
    CaseSpec("bad_config", 3, "easy", "test", "ledger-service", "HighErrorRate", 16),
    CaseSpec("bad_config", 4, "easy", "test", "api-gateway", "HighErrorRate", 10,
             noise="medium"),
    CaseSpec("bad_config", 5, "medium", "dev", "orders-service", "HighLatencyP95", 13,
             severity="warning", noise="medium",
             red_herrings=(RedHerring("recent_deploy", "payments-service"),)),
    CaseSpec("bad_config", 6, "medium", "test", "ledger-service", "HighErrorRate", 19,
             alert_service="payments-service", noise="medium"),
    CaseSpec("bad_config", 7, "medium", "test", "payments-service", "HighErrorRate", 12,
             alert_service="api-gateway", noise="medium",
             red_herrings=(RedHerring("provider_429_burst"),)),
    CaseSpec("bad_config", 8, "medium", "test", "api-gateway", "HighErrorRate", 18,
             noise="high",
             red_herrings=(RedHerring("recent_deploy", "orders-service"),)),
    CaseSpec("bad_config", 9, "hard", "dev", "payments-service", "HighErrorRate", 15,
             alert_service="orders-service", noise="high", flavor="slow_burn",
             red_herrings=(RedHerring("same_service_config"),)),
    CaseSpec("bad_config", 10, "hard", "test", "ledger-service", "HighErrorRate", 20,
             alert_service="api-gateway", noise="high",
             missing_metrics=("error_rate",), red_herrings=(RedHerring("redis_latency_spike"),)),
)  # fmt: skip


def build(b: CaseBuilder, spec: CaseSpec) -> ScenarioResult:
    service = spec.service
    rng = b.rng.derive("bad-config")
    onset_ts = alerts.onset(b, spec.alert, rng)
    if spec.flavor == "slow_burn":  # harmless at first, fails more as traffic grows
        change_ts = b.alert_ts - rng.randint(35, 45) * MINUTE
    else:  # effects appear seconds after the hot reload (a minute for pool exhaustion)
        lag = MINUTE if service == "orders-service" else 0
        change_ts = onset_ts - lag - rng.randint(4, 12)
    description, setting = BAD_CHANGES[service]
    change_handle = b.plan_deploy(change_ts, service, "config", description)
    b.keep_quiet(service, change_ts - 2 * HOUR, b.now_ts)
    planned = [herrings.plan(b, h, service) for h in spec.red_herrings]
    background.finalize(b)

    start = change_ts + rng.randint(2, 10)  # hot reload takes effect within seconds
    end = b.now_ts + 1
    error_pct = rng.uniform(20, 45)
    if service == "payments-service":
        # Only calls slower than 200 ms fail; in the slow-burn variant that share grows with
        # traffic until it crosses the alert threshold at onset.
        full_from = onset_ts if spec.flavor == "slow_burn" else start

        def failing(ts: int, value: float) -> float:
            progress = min(1.0, (ts - start) / max(full_from - start, 1))
            return max(value, 1.0 + (error_pct - 1.0) * progress)

        b.update(service, "error_rate", start, end, failing)
        b.scale("payment-provider-api", "rps", start, end, 2.2)  # retries after each timeout
        b.scale(service, "latency_p95", start, end, 1.8)
        burst(b, service, start, full_from, 1.0, rng, _provider_timeout)
        burst(b, service, full_from, b.now_ts, 6.0, rng, _provider_timeout)
        burst(b, service, full_from, b.now_ts, 3.0, rng, _authorize_failed)
        propagate(b, service, full_from, b.now_ts, error_pct, "server_error", rng)
    elif service == "orders-service":
        pool = 2 * b.topology.services[service].replicas
        b.update(service, "db_connections", start, end, lambda _, v: min(v, float(pool)))
        b.floor(service, "db_connections", start + MINUTE, end, float(pool))
        b.floor(service, "latency_p95", start + MINUTE, end, 30000 * rng.uniform(0.95, 1.0))
        b.floor(service, "error_rate", start + MINUTE, end, error_pct)
        burst(b, service, start + MINUTE, b.now_ts, 3.0, rng, _tiny_pool)
        propagate(b, service, start + MINUTE, b.now_ts, error_pct, "timeout", rng)
    elif service == "ledger-service":
        b.floor(service, "error_rate", start, end, error_pct)
        burst(b, service, start, b.now_ts, 4.0, rng, _statement_timeout)
        burst(b, "postgres", start, b.now_ts, 3.0, rng, _pg_cancel)
        propagate(b, service, start, b.now_ts, error_pct, "server_error", rng)
    else:  # api-gateway: the payments route now times out after 150 ms
        gateway_pct = rng.uniform(12, 20)
        b.floor(service, "error_rate", start, end, gateway_pct)
        burst(b, service, start, b.now_ts, 6.0, rng, _gateway_route_timeout)
        error_pct = gateway_pct

    herring_notes = tuple(herrings.apply(b, p) for p in planned)
    alerts.enforce_condition(b, spec.alert, spec.alerting_service)
    degrade_observability(b, spec, ())

    change = b.deploy(change_handle)
    alert = alerts.build_alert(b, spec.alert, spec.alerting_service, spec.severity)
    expected = Expected(
        root_cause=(
            f"config change {change.version} on {service} "
            f"({relative(change.ts, b.alert_ts)}: '{change.description}') broke it: "
            f"{_effect(service)}; code did not change"
        ),
        root_cause_keywords=(change.version, setting),
        root_service=service,
        should_escalate=False,
        must_call_tools=("get_deploys", "search_logs"),
        acceptable_actions=(
            Action(type="revert_config", target=service, to_version=change.previous_version),
        ),
        red_herrings=herring_notes,
    )
    tags = (*common_tags(spec), "config_change")
    return ScenarioResult(alert, expected, (*tags, "slow_burn") if spec.flavor else tags)


def _effect(service: str) -> str:
    return {
        "payments-service": "provider calls time out after 200 ms and payments fail",
        "orders-service": "the DB pool is only 2 connections per pod, requests time out waiting",
        "ledger-service": "writes are cancelled by a 50 ms statement timeout",
        "api-gateway": "the payments route times out after 150 ms with 504",
    }[service]


def _provider_timeout(rng: Rng) -> catalog.Message:
    return "WARN", (
        "c.a.p.provider.ProviderClient : read timeout after 200ms calling payment-provider-api; "
        f"retrying (attempt {rng.randint(2, 3)}/3)"
    )


def _authorize_failed(rng: Rng) -> catalog.Message:
    return "ERROR", (
        "c.a.p.api.PaymentController : POST /v1/payments failed: provider authorize timed out "
        f"after 3 attempts (read timeout 200ms) paymentId={catalog.payment_id(rng)}"
    )


def _tiny_pool(rng: Rng) -> catalog.Message:
    return "ERROR", (
        f"orders.api: POST /api/v1/orders failed after {rng.randint(30001, 30040)}ms: "
        "sqlalchemy.exc.TimeoutError: QueuePool limit of size 2 overflow 0 reached, "
        "connection timed out, timeout 30.00 (Background on this error at: "
        "https://sqlalche.me/e/20/3o7r)"
    )


def _statement_timeout(rng: Rng) -> catalog.Message:
    return "ERROR", (
        f'msg="request failed" method=POST path=/v1/entries status=500 '
        f"duration_ms={rng.randint(50, 58)} "
        'err="ERROR: canceling statement due to statement timeout (SQLSTATE 57014)" '
        f"request_id={catalog.request_id(rng)}"
    )


def _pg_cancel(rng: Rng) -> catalog.Message:
    return "ERROR", (
        "ERROR:  canceling statement due to statement timeout"
        "\nSTATEMENT:  INSERT INTO entries (id, account_id, amount, currency, idempotency_key) "
        "VALUES ($1, $2, $3, $4, $5)"
    )


def _gateway_route_timeout(rng: Rng) -> catalog.Message:
    return "ERROR", catalog.gateway_access(
        rng, "POST", "/api/v1/payments", 504, "payments-service", 150, "flags=UT"
    )
