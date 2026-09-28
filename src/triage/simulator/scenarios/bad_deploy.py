"""bad_deploy: a code release breaks a request path minutes after rollout.

Errors step up as new pods come online, and the new exception appears only on pods of the
new ReplicaSet. The ``slow`` flavor is a performance regression instead (an N+1 query):
latency rather than errors, with a jump in database query volume. Roll back to fix.
"""

from __future__ import annotations

from triage.simulator import alerts, background, catalog, herrings
from triage.simulator.builder import CaseBuilder
from triage.simulator.clock import HOUR, relative
from triage.simulator.rng import Rng
from triage.simulator.scenarios.base import ScenarioResult, common_tags, degrade_observability
from triage.simulator.scenarios.common import burst, propagate
from triage.simulator.schemas import Action, Expected
from triage.simulator.spec import CaseSpec, RedHerring

# (release description, exception keyword, exception message)
REGRESSIONS = {
    "payments-service": (
        "Apply geo risk rules to the 3DS2 challenge flow",
        "NullPointerException",
        "c.a.p.api.ErrorHandler : unhandled exception on POST /v1/payments"
        "\njava.lang.NullPointerException: Cannot invoke "
        '"com.acme.payments.model.BillingAddress.getCountry()" because "address" is null'
        "\n\tat com.acme.payments.risk.GeoRules.check(GeoRules.java:41)"
        "\n\tat com.acme.payments.risk.RiskScorer.score(RiskScorer.java:63)",
    ),
    "orders-service": (
        "Support multi-currency pricing payloads",
        "KeyError",
        "orders.api: unhandled exception on POST /api/v1/orders"
        "\nTraceback (most recent call last):"
        '\n  File "/app/orders/api/orders.py", line 112, in create_order'
        '\n    currency = payload["pricing"]["currency"]'
        "\nKeyError: 'currency'",
    ),
    "ledger-service": (
        "Refactor entry posting to support reversals",
        "nil pointer",
        'msg="panic recovered" method=POST path=/v1/entries status=500 '
        'err="runtime error: invalid memory address or nil pointer dereference" '
        'stack="goroutine 4183 [running]:\\nledger/internal/entries.(*Service).Post(0x0, ...)'
        '\\n\\t/app/internal/entries/service.go:88 +0x1c4"',
    ),
}
SLOW_RELEASE = "Load order items for order history responses"

VARIANTS = (
    CaseSpec("bad_deploy", 1, "easy", "dev", "payments-service", "HighErrorRate", 14),
    CaseSpec("bad_deploy", 2, "easy", "test", "orders-service", "HighErrorRate", 11),
    CaseSpec("bad_deploy", 3, "easy", "test", "ledger-service", "HighErrorRate", 16),
    CaseSpec("bad_deploy", 4, "easy", "test", "payments-service", "HighErrorRate", 10,
             noise="medium"),
    CaseSpec("bad_deploy", 5, "medium", "dev", "orders-service", "HighErrorRate", 13,
             noise="medium",
             red_herrings=(RedHerring("recent_deploy", "ledger-service"),)),
    CaseSpec("bad_deploy", 6, "medium", "test", "ledger-service", "HighErrorRate", 19,
             alert_service="payments-service", noise="medium"),
    CaseSpec("bad_deploy", 7, "medium", "test", "payments-service", "HighErrorRate", 12,
             noise="high",
             red_herrings=(RedHerring("redis_latency_spike"),)),
    CaseSpec("bad_deploy", 8, "medium", "test", "orders-service", "HighLatencyP95", 18,
             severity="warning", noise="medium", flavor="slow"),
    CaseSpec("bad_deploy", 9, "hard", "dev", "payments-service", "HighErrorRate", 15,
             noise="high", flavor="partial",
             red_herrings=(RedHerring("provider_429_burst"),)),
    CaseSpec("bad_deploy", 10, "hard", "test", "ledger-service", "HighErrorRate", 20,
             alert_service="api-gateway", noise="high",
             missing_metrics=("error_rate",),
             red_herrings=(RedHerring("recent_deploy", "api-gateway"),)),
)  # fmt: skip


def build(b: CaseBuilder, spec: CaseSpec) -> ScenarioResult:
    service = spec.service
    rng = b.rng.derive("bad-deploy")
    # Errors cross the threshold as soon as the first new pod serves; a latency regression
    # only once most pods are new.
    lead = rng.randint(40, 100) if spec.flavor == "slow" else rng.randint(15, 35)
    release_ts = alerts.onset(b, spec.alert, rng) - lead
    description = SLOW_RELEASE if spec.flavor == "slow" else REGRESSIONS[service][0]
    release_handle = b.plan_deploy(release_ts, service, "code", description)
    b.keep_quiet(service, release_ts - 2 * HOUR, b.now_ts)
    planned = [herrings.plan(b, h, service) for h in spec.red_herrings]
    background.finalize(b)

    # Rolling update: new pods start serving one by one.
    replicas = b.topology.services[service].replicas
    starts = []
    t = release_ts
    for _ in range(replicas):
        t += rng.randint(15, 35)
        starts.append(t)

    def rolled_out(ts: int) -> float:
        return sum(1 for s in starts if s <= ts) / replicas

    if spec.flavor == "slow":
        keyword = "db_queries"
        slow_factor = rng.uniform(12, 20)
        b.update(
            service, "latency_p95", release_ts, b.now_ts + 1,
            lambda ts, v: v * (1 + (slow_factor - 1) * rolled_out(ts)),
        )  # fmt: skip
        b.update(
            "postgres",
            "rps",
            release_ts,
            b.now_ts + 1,
            lambda ts, v: v * (1 + 1.8 * rolled_out(ts)),
        )
        b.update("postgres", "cpu", release_ts, b.now_ts + 1, lambda ts, v: v + 30 * rolled_out(ts))
        b.add(service, "cpu", starts[-1], b.now_ts + 1, 15)
        burst(b, service, starts[0], b.now_ts, 4.0, rng, _slow_request)
        error_pct = 0.0
    else:
        keyword = REGRESSIONS[service][1]
        broken_share = rng.uniform(6, 10) if spec.flavor == "partial" else rng.uniform(20, 50)
        error_pct = broken_share
        b.update(
            service, "error_rate", release_ts, b.now_ts + 1,
            lambda ts, v: v + broken_share * rolled_out(ts),
        )  # fmt: skip
        exception = REGRESSIONS[service][2]
        per_minute = 2.0 if spec.flavor == "partial" else 5.0
        burst(b, service, starts[0], b.now_ts, per_minute, rng, lambda _: ("ERROR", exception))
        propagate(b, service, starts[-1], b.now_ts, error_pct, "server_error", rng)

    herring_notes = tuple(herrings.apply(b, p) for p in planned)
    alerts.enforce_condition(b, spec.alert, spec.alerting_service)
    degrade_observability(b, spec, ())

    release = b.deploy(release_handle)
    alert = alerts.build_alert(b, spec.alert, spec.alerting_service, spec.severity)
    effect = (
        "made order history responses slow (hundreds of database queries per request)"
        if spec.flavor == "slow"
        else f"introduced a regression ({keyword}) that fails a share of requests with 5xx"
    )
    expected = Expected(
        root_cause=(
            f"{service} {release.version} (deployed {relative(release.ts, b.alert_ts)}: "
            f"'{release.description}') {effect}"
        ),
        root_cause_keywords=(release.version, keyword),
        root_service=service,
        should_escalate=False,
        must_call_tools=("get_deploys", "search_logs"),
        acceptable_actions=(
            Action(type="rollback", target=service, to_version=release.previous_version),
        ),
        red_herrings=herring_notes,
    )
    tags = common_tags(spec) + (("partial_failure",) if spec.flavor == "partial" else ())
    return ScenarioResult(alert, expected, tags)


def _slow_request(rng: Rng) -> catalog.Message:
    customer = "cus_" + rng.token(10, catalog.ID_ALPHABET)
    queries = rng.randint(180, 520)
    return "WARN", (
        f"orders.api: slow request GET /api/v1/orders?customer_id={customer} took "
        f"{rng.randint(1800, 5200)}ms (db_queries={queries})"
    )
