"""Red herrings: plausible but unrelated signals planted near the alert.

Planning and applying are separate steps because deploy-based herrings must be registered
before versions are assigned, while metric and log effects need the background filled in.
"""

from __future__ import annotations

from dataclasses import dataclass

from triage.simulator import catalog
from triage.simulator.builder import CaseBuilder
from triage.simulator.clock import MINUTE, relative
from triage.simulator.spec import RedHerring


@dataclass(frozen=True)
class PlannedHerring:
    herring: RedHerring
    ts: int
    duration_s: int = 0
    deploy: int | None = None


def plan(b: CaseBuilder, herring: RedHerring, root_service: str) -> PlannedHerring:
    rng = b.rng.derive("herring", herring.kind, herring.service)
    if herring.kind == "recent_deploy":
        ts = b.alert_ts - rng.randint(8, 12) * MINUTE - rng.randint(0, 59)
        if rng.chance(0.5):
            description = rng.choice(catalog.CODE_CHANGES[herring.service])
            handle = b.plan_deploy(ts, herring.service, "code", description)
        else:
            description = rng.choice(catalog.HARMLESS_CONFIG_CHANGES[herring.service])
            handle = b.plan_deploy(ts, herring.service, "config", description)
        return PlannedHerring(herring, ts, deploy=handle)
    if herring.kind == "same_service_config":
        target = herring.service or root_service
        ts = b.alert_ts - rng.randint(9, 14) * MINUTE - rng.randint(0, 59)
        description = rng.choice(catalog.HARMLESS_CONFIG_CHANGES[target])
        return PlannedHerring(herring, ts, deploy=b.plan_deploy(ts, target, "config", description))
    if herring.kind == "redis_latency_spike":
        ts = b.alert_ts - rng.randint(15, 35) * MINUTE
        return PlannedHerring(herring, ts, duration_s=rng.randint(3, 6) * MINUTE)
    ts = b.alert_ts - rng.randint(12, 40) * MINUTE  # provider_429_burst
    return PlannedHerring(herring, ts, duration_s=rng.randint(2, 4) * MINUTE)


def apply(b: CaseBuilder, planned: PlannedHerring) -> str:
    """Overlay the herring's signals; returns its description for the golden record."""
    herring = planned.herring
    rng = b.rng.derive("herring-apply", herring.kind, herring.service)
    when = relative(planned.ts, b.alert_ts)
    if planned.deploy is not None:
        record = b.deploy(planned.deploy)
        return (
            f"{record.service} {record.change_type} change {record.version} at {when} "
            f"('{record.description}') is unrelated"
        )
    start, end = planned.ts, planned.ts + planned.duration_s
    minutes = planned.duration_s // MINUTE
    if herring.kind == "redis_latency_spike":
        factor = rng.uniform(4, 8)
        b.scale("redis", "latency_p95", start, end, factor)
        b.add("redis", "cpu", start, end, 20)
        b.scale("orders-service", "latency_p95", start, end, 1.2)
        for _ in range(rng.randint(5, 12)):
            msg = (
                f"orders.cache: redis GET order:{catalog.order_id(rng)} timed out after 50ms; "
                "falling back to postgres"
            )
            b.log(rng.randint(start * 1000, end * 1000), "orders-service", "WARN", msg)
        return f"redis latency spike at {when} lasting {minutes} minutes is unrelated"
    bump = rng.uniform(3, 6)
    b.add("payment-provider-api", "error_rate", start, end, bump)
    b.scale("payments-service", "latency_p95", start, end, 1.15)
    for _ in range(rng.randint(8, 15)):
        msg = (
            "c.a.p.provider.ProviderClient : payment-provider-api responded 429 Too Many "
            f"Requests; retrying in 400ms (attempt 1/3) paymentId={catalog.payment_id(rng)}"
        )
        b.log(rng.randint(start * 1000, end * 1000), "payments-service", "WARN", msg)
    return (
        f"brief HTTP 429 responses from payment-provider-api at {when} ({minutes} min) "
        "are unrelated"
    )
