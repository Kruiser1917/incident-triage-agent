"""trap: evidence is insufficient or contradictory; the right answer is to escalate.

Kinds:
- no_telemetry: the alerting service returns 5xx, but its logs are missing for the whole
  window (log pipeline gap) and some of its metrics are absent; nothing else explains it.
- conflicting: a deploy of the service and a degradation of one of its dependencies start
  in the same minute, and the service only logs generic 500s: the two cannot be told apart.
- blip: the alert fired on a short spike that is already over; no cause is visible.
- unknown_errors: steady 5xx with opaque error references, no deploys, healthy dependencies.
What is being tested is not guessing: the agent must say what is missing and escalate.
"""

from __future__ import annotations

from collections.abc import Callable

from triage.simulator import alerts, background, catalog, herrings
from triage.simulator.builder import CaseBuilder
from triage.simulator.clock import MINUTE, relative
from triage.simulator.rng import Rng
from triage.simulator.scenarios.base import ScenarioResult, common_tags
from triage.simulator.scenarios.common import burst
from triage.simulator.schemas import Action, Expected
from triage.simulator.spec import CaseSpec, RedHerring

NO_TELEMETRY = "no_telemetry"
CONFLICTING = "conflicting"
BLIP = "blip"
UNKNOWN = "unknown_errors"

VARIANTS = (
    CaseSpec("trap", 1, "easy", "dev", "orders-service", "HighErrorRate", 14, flavor=NO_TELEMETRY),
    CaseSpec("trap", 2, "easy", "test", "payments-service", "HighErrorRate", 11, flavor=BLIP),
    CaseSpec("trap", 3, "easy", "test", "ledger-service", "HighErrorRate", 16, flavor=UNKNOWN),
    CaseSpec("trap", 4, "medium", "test", "payments-service", "HighErrorRate", 10,
             noise="medium", flavor=NO_TELEMETRY),
    CaseSpec("trap", 5, "medium", "test", "orders-service", "HighErrorRate", 13,
             noise="medium", flavor=CONFLICTING),
    CaseSpec("trap", 6, "medium", "test", "payments-service", "HighErrorRate", 19,
             noise="medium", flavor=UNKNOWN),
    CaseSpec("trap", 7, "medium", "test", "ledger-service", "HighErrorRate", 12,
             noise="medium", flavor=BLIP),
    CaseSpec("trap", 8, "hard", "dev", "payments-service", "HighErrorRate", 18,
             noise="high", flavor=CONFLICTING),
    CaseSpec("trap", 9, "hard", "test", "ledger-service", "HighErrorRate", 15,
             noise="high", flavor=NO_TELEMETRY,
             red_herrings=(RedHerring("recent_deploy", "api-gateway"),)),
    CaseSpec("trap", 10, "hard", "test", "ledger-service", "HighErrorRate", 20,
             noise="high", flavor=CONFLICTING),
)  # fmt: skip

# Which dependency degrades together with the deploy in "conflicting" traps.
CONFLICT_DEPENDENCY = {
    "orders-service": "redis",
    "payments-service": "payment-provider-api",
    "ledger-service": "postgres",
}


def build(b: CaseBuilder, spec: CaseSpec) -> ScenarioResult:
    service = spec.service
    rng = b.rng.derive("trap")
    onset = alerts.onset(b, spec.alert, rng)
    deploy_handle = None
    if spec.flavor == CONFLICTING:
        description = rng.choice(catalog.CODE_CHANGES[service])
        deploy_handle = b.plan_deploy(onset - rng.randint(10, 40), service, "code", description)
    planned = [herrings.plan(b, h, service) for h in spec.red_herrings]
    background.finalize(b)
    end = b.now_ts + 1

    if spec.flavor == NO_TELEMETRY:
        b.floor(service, "error_rate", onset, end, rng.uniform(7, 14))
        b.scale(service, "latency_p95", onset, end, 1.3)
        explanation = (
            f"{service} returns 5xx since about {relative(onset, b.alert_ts)}, but it has no "
            "logs in the whole window (log pipeline gap) and its cpu/memory metrics are "
            "missing; deploys and dependencies show nothing that explains the errors"
        )
    elif spec.flavor == BLIP:
        b.floor(
            service, "error_rate", b.alert_ts - 3 * MINUTE, b.alert_ts + MINUTE, rng.uniform(6, 9)
        )
        burst(b, service, b.alert_ts - 3 * MINUTE, b.alert_ts + MINUTE, 1.0, rng, _generic(service))
        explanation = (
            f"{service} had a 5xx spike of about three minutes that is already over; logs show "
            "only a few generic 500s, and no deploy, dependency or resource signal explains it"
        )
    elif spec.flavor == UNKNOWN:
        b.floor(service, "error_rate", onset, end, rng.uniform(8, 15))
        burst(b, service, onset, b.now_ts, 2.0, rng, _generic(service))
        explanation = (
            f"{service} returns 5xx since about {relative(onset, b.alert_ts)} with opaque "
            "internal error references only; no deploy, config change, dependency problem or "
            "resource pressure is visible"
        )
    else:
        dependency = CONFLICT_DEPENDENCY[service]
        b.floor(service, "error_rate", onset, end, rng.uniform(10, 20))
        _degrade(b, dependency, onset, rng)
        burst(b, service, onset, b.now_ts, 3.0, rng, _generic(service))
        assert deploy_handle is not None
        deploy = b.deploy(deploy_handle)
        explanation = (
            f"two plausible causes start in the same minute: {service} deploy {deploy.version} "
            f"at {relative(deploy.ts, b.alert_ts)} and a degradation of {dependency}; {service} "
            "logs only generic 500s, so the evidence cannot tell which one is responsible"
        )
    herring_notes = tuple(herrings.apply(b, p) for p in planned)
    alerts.enforce_condition(b, spec.alert, spec.alerting_service)
    if spec.flavor == NO_TELEMETRY:  # the gap applies to whatever was generated above
        b.drop_logs(service)
        for metric in ("cpu", "memory"):
            b.drop_series(service, metric)

    expected = Expected(
        root_cause=f"insufficient evidence to determine the root cause: {explanation}",
        root_cause_keywords=(service,),
        root_service=None,
        should_escalate=True,
        must_call_tools=("get_deploys", "get_metrics", "search_logs"),
        acceptable_actions=(Action(type="escalate", target=service),),
        red_herrings=herring_notes,
    )
    alert = alerts.build_alert(b, spec.alert, spec.alerting_service, spec.severity)
    return ScenarioResult(alert, expected, (*common_tags(spec), "trap", spec.flavor))


def _degrade(b: CaseBuilder, dependency: str, onset: int, rng: Rng) -> None:
    end = b.now_ts + 1
    if dependency == "redis":
        b.scale("redis", "latency_p95", onset, end, rng.uniform(5, 8))
        burst(b, "orders-service", onset, b.now_ts, 2.0, rng, _redis_timeout)
    elif dependency == "payment-provider-api":
        b.add(dependency, "error_rate", onset, end, rng.uniform(10, 20))
        b.scale(dependency, "latency_p95", onset, end, rng.uniform(3, 5))
        burst(b, "payments-service", onset, b.now_ts, 2.0, rng, _provider_slow)
    else:
        b.scale("postgres", "latency_p95", onset, end, rng.uniform(4, 6))
        b.add("postgres", "cpu", onset, end, 30)


def _generic(service: str) -> Callable[[Rng], catalog.Message]:
    def make(rng: Rng) -> catalog.Message:
        ref = "err_" + rng.token(10, catalog.ID_ALPHABET)
        if service == "orders-service":
            return (
                "ERROR",
                f"orders.api: POST /api/v1/orders 500 in {rng.randint(40, 900)}ms (ref={ref})",
            )
        if service == "payments-service":
            return "ERROR", f"c.a.p.api.ErrorHandler : request failed with status 500 (ref={ref})"
        return "ERROR", (
            f'msg="request failed" method=POST path=/v1/entries status=500 err="internal error" '
            f"ref={ref}"
        )

    return make


def _redis_timeout(rng: Rng) -> catalog.Message:
    return "WARN", (
        f"orders.cache: redis GET order:{catalog.order_id(rng)} timed out after 50ms; "
        "falling back to postgres"
    )


def _provider_slow(rng: Rng) -> catalog.Message:
    return "WARN", (
        "c.a.p.provider.ProviderClient : read timeout after 2000ms calling "
        f"payment-provider-api; retrying (attempt {rng.randint(2, 3)}/3)"
    )
