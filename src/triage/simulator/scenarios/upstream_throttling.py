"""upstream_throttling: the external payment provider answers 429, and our retries make it worse.

payments-service retries immediately (ignoring Retry-After), so every throttled call turns into
several calls: egress traffic to the provider multiplies while real payment volume does not.
Flavors:
- retry_config: a config change raised PROVIDER_MAX_RETRIES; at peak traffic the retry
  multiplier pushes us over the provider's quota. Revert the config (or escalate).
- quota: a traffic spike (merchant campaign) crosses the provider's 120 req/s quota; retries
  amplify it. Nothing to roll back on our side: escalate to the provider relationship owner.
Scaling payments-service is wrong in both: more pods send more retries.
"""

from __future__ import annotations

from collections.abc import Callable

from triage.simulator import alerts, background, catalog, herrings
from triage.simulator.builder import CaseBuilder
from triage.simulator.clock import HOUR, MINUTE, relative
from triage.simulator.rng import Rng
from triage.simulator.scenarios.base import ScenarioResult, common_tags, degrade_observability
from triage.simulator.scenarios.common import burst, propagate
from triage.simulator.schemas import Action, Expected
from triage.simulator.spec import CaseSpec, RedHerring

PROVIDER = "payment-provider-api"
RETRY_CONFIG = "retry_config"
QUOTA = "quota"
RETRY_CHANGE = "Set PROVIDER_MAX_RETRIES=5 (was 2)"
QUOTA_RPS = 120

VARIANTS = (
    CaseSpec("upstream_throttling", 1, "easy", "dev", PROVIDER, "HighErrorRate", 14,
             alert_service="payments-service", flavor=RETRY_CONFIG, release_hours_ago=4.0),
    CaseSpec("upstream_throttling", 2, "easy", "test", PROVIDER, "HighErrorRate", 12,
             alert_service="payments-service", flavor=QUOTA),
    CaseSpec("upstream_throttling", 3, "easy", "test", PROVIDER, "HighLatencyP95", 19,
             alert_service="payments-service", severity="warning", flavor=RETRY_CONFIG,
             release_hours_ago=6.0),
    CaseSpec("upstream_throttling", 4, "easy", "test", PROVIDER, "HighErrorRate", 13,
             alert_service="payments-service", noise="medium", flavor=QUOTA),
    CaseSpec("upstream_throttling", 5, "medium", "dev", PROVIDER, "HighErrorRate", 11,
             alert_service="orders-service", noise="medium", flavor=QUOTA,
             red_herrings=(RedHerring("recent_deploy", "payments-service"),)),
    CaseSpec("upstream_throttling", 6, "medium", "test", PROVIDER, "HighErrorRate", 18,
             alert_service="payments-service", noise="medium", flavor=RETRY_CONFIG,
             release_hours_ago=10.0, red_herrings=(RedHerring("redis_latency_spike"),)),
    CaseSpec("upstream_throttling", 7, "medium", "test", PROVIDER, "HighErrorRate", 12,
             alert_service="api-gateway", noise="medium", flavor=QUOTA, metric_gaps=True),
    CaseSpec("upstream_throttling", 8, "medium", "test", PROVIDER, "HighErrorRate", 13,
             alert_service="payments-service", noise="high", flavor=RETRY_CONFIG,
             release_hours_ago=48.0),
    CaseSpec("upstream_throttling", 9, "hard", "dev", PROVIDER, "HighErrorRate", 19,
             alert_service="api-gateway", noise="high", flavor=QUOTA,
             missing_metrics=("error_rate",),
             red_herrings=(RedHerring("recent_deploy", "ledger-service"),)),
    CaseSpec("upstream_throttling", 10, "hard", "test", PROVIDER, "HighLatencyP95", 12,
             alert_service="payments-service", severity="warning", noise="high",
             flavor=RETRY_CONFIG, release_hours_ago=8.0,
             red_herrings=(RedHerring("same_service_config", "payments-service"),)),
)  # fmt: skip


def build(b: CaseBuilder, spec: CaseSpec) -> ScenarioResult:
    rng = b.rng.derive("throttling")
    retries = 5 if spec.flavor == RETRY_CONFIG else 3
    change_handle = None
    if spec.flavor == RETRY_CONFIG:
        change_ts = b.alert_ts - int(spec.release_hours_ago * HOUR) - rng.randint(0, 50) * MINUTE
        change_handle = b.plan_deploy(change_ts, "payments-service", "config", RETRY_CHANGE)
    planned = [herrings.plan(b, h, "payments-service") for h in spec.red_herrings]
    background.finalize(b)

    storm_start = alerts.onset(b, spec.alert, rng)
    end = b.now_ts + 1
    if spec.flavor == QUOTA:  # a merchant campaign lifts payment traffic first
        spike = rng.uniform(1.5, 1.9)
        campaign = storm_start - rng.randint(20, 45) * MINUTE
        for service in ("api-gateway", "payments-service", PROVIDER):
            b.update(service, "rps", campaign, end, _ramp(campaign, spike))
    amplification = rng.uniform(2.0, 3.0)
    b.scale(PROVIDER, "rps", storm_start, end, amplification)
    b.floor(PROVIDER, "rps", storm_start, end, QUOTA_RPS * rng.uniform(1.6, 2.4))
    b.floor(PROVIDER, "error_rate", storm_start, end, rng.uniform(35, 70))
    b.scale(PROVIDER, "latency_p95", storm_start, end, 0.6)  # 429s are answered fast
    error_pct = rng.uniform(12, 35)
    b.floor("payments-service", "error_rate", storm_start, end, error_pct)
    b.scale("payments-service", "latency_p95", storm_start, end, rng.uniform(2.0, 3.0))
    if spec.flavor == RETRY_CONFIG:  # up to five sequential attempts per payment
        b.floor("payments-service", "latency_p95", storm_start, end, rng.uniform(1100, 1600))
    b.add("payments-service", "cpu", storm_start, end, 10)

    burst(b, "payments-service", storm_start, b.now_ts, 6.0, rng, _throttled(retries))
    burst(b, "payments-service", storm_start, b.now_ts, 2.0, rng, _gave_up(retries))
    propagate(b, "payments-service", storm_start, b.now_ts, error_pct, "server_error", rng)
    herring_notes = tuple(herrings.apply(b, p) for p in planned)
    alerts.enforce_condition(b, spec.alert, spec.alerting_service)
    degrade_observability(b, spec, ())

    when = relative(storm_start, b.alert_ts)
    actions: tuple[Action, ...]
    if change_handle is not None:
        change = b.deploy(change_handle)
        root_cause = (
            f"{PROVIDER} throttles us with HTTP 429 (quota {QUOTA_RPS} req/s) since {when}; "
            "payments-service retries immediately without backoff and config change "
            f"{change.version} ({relative(change.ts, b.alert_ts)}: '{change.description}') "
            "multiplied the retries, turning throttling into a retry storm"
        )
        actions = (
            Action(
                type="revert_config", target="payments-service", to_version=change.previous_version
            ),
            Action(type="escalate", target=PROVIDER),
        )
    else:
        root_cause = (
            f"a traffic spike pushed calls to {PROVIDER} over its {QUOTA_RPS} req/s quota; it "
            f"answers HTTP 429 since {when} and payments-service retries immediately without "
            "backoff, amplifying the load (retry storm)"
        )
        actions = (Action(type="escalate", target=PROVIDER),)
    expected = Expected(
        root_cause=root_cause,
        root_cause_keywords=("429", "retry"),
        root_service=PROVIDER,
        should_escalate=False,
        must_call_tools=("get_metrics", "search_logs"),
        acceptable_actions=actions,
        red_herrings=herring_notes,
    )
    alert = alerts.build_alert(b, spec.alert, spec.alerting_service, spec.severity)
    tags = common_tags(spec) + (("old_change",) if spec.release_hours_ago >= 24 else ())
    return ScenarioResult(alert, expected, tags)


def _ramp(start: int, factor: float) -> Callable[[int, float], float]:
    def scale(ts: int, value: float) -> float:
        progress = min(1.0, (ts - start) / (15 * MINUTE))  # campaign traffic ramps up in 15 min
        return value * (1 + (factor - 1) * progress)

    return scale


def _throttled(retries: int) -> Callable[[Rng], catalog.Message]:
    def make(rng: Rng) -> catalog.Message:
        return "WARN", (
            f"c.a.p.provider.ProviderClient : {PROVIDER} responded 429 Too Many Requests "
            f"(X-RateLimit-Limit: {QUOTA_RPS}, X-RateLimit-Remaining: 0, Retry-After: 30); "
            f"retrying immediately (attempt {rng.randint(2, retries)}/{retries}) "
            f"paymentId={catalog.payment_id(rng)}"
        )

    return make


def _gave_up(retries: int) -> Callable[[Rng], catalog.Message]:
    def make(rng: Rng) -> catalog.Message:
        return "ERROR", (
            f"c.a.p.api.PaymentController : POST /v1/payments failed: authorize failed after "
            f"{retries} attempts: 429 Too Many Requests paymentId={catalog.payment_id(rng)}"
        )

    return make
