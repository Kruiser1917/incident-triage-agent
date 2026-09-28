"""cert_expired: a service's TLS server certificate expires and every handshake to it fails.

No deploy, no load change: errors at the callers step up at a round wall-clock time (the
certificate's NotAfter). The broken service itself looks idle rather than failing, because
requests never reach it; its only own trace is handshake alerts in its logs. Rotate the
certificate to fix.
"""

from __future__ import annotations

from collections.abc import Callable

from triage.simulator import alerts, background, catalog, herrings
from triage.simulator.builder import CaseBuilder
from triage.simulator.clock import from_epoch_s
from triage.simulator.rng import Rng
from triage.simulator.scenarios.base import ScenarioResult, common_tags, degrade_observability
from triage.simulator.scenarios.common import burst, propagate
from triage.simulator.schemas import Action, Expected
from triage.simulator.spec import CaseSpec, RedHerring

VARIANTS = (
    CaseSpec("cert_expired", 1, "easy", "dev", "ledger-service", "HighErrorRate", 14,
             alert_service="payments-service"),
    CaseSpec("cert_expired", 2, "easy", "test", "payments-service", "HighErrorRate", 11,
             alert_service="orders-service"),
    CaseSpec("cert_expired", 3, "easy", "test", "orders-service", "HighErrorRate", 16,
             alert_service="api-gateway"),
    CaseSpec("cert_expired", 4, "easy", "test", "ledger-service", "HighErrorRate", 10,
             alert_service="payments-service", noise="medium"),
    CaseSpec("cert_expired", 5, "medium", "dev", "payments-service", "HighErrorRate", 13,
             alert_service="orders-service", noise="medium",
             red_herrings=(RedHerring("recent_deploy", "orders-service"),)),
    CaseSpec("cert_expired", 6, "medium", "test", "orders-service", "HighErrorRate", 19,
             alert_service="api-gateway", noise="medium",
             red_herrings=(RedHerring("redis_latency_spike"),)),
    CaseSpec("cert_expired", 7, "medium", "test", "ledger-service", "HighErrorRate", 12,
             alert_service="api-gateway", noise="medium"),
    CaseSpec("cert_expired", 8, "medium", "test", "payments-service", "HighErrorRate", 18,
             alert_service="api-gateway", noise="high", metric_gaps=True),
    CaseSpec("cert_expired", 9, "hard", "dev", "ledger-service", "HighErrorRate", 15,
             alert_service="api-gateway", noise="high",
             red_herrings=(RedHerring("recent_deploy", "payments-service"),)),
    CaseSpec("cert_expired", 10, "hard", "test", "orders-service", "HighErrorRate", 20,
             alert_service="api-gateway", noise="high", missing_metrics=("rps",),
             red_herrings=(RedHerring("same_service_config"),)),
)  # fmt: skip


def build(b: CaseBuilder, spec: CaseSpec) -> ScenarioResult:
    service = spec.service
    rng = b.rng.derive("cert")
    planned = [herrings.plan(b, h, service) for h in spec.red_herrings]
    background.finalize(b)

    # NotAfter = issuance time + validity: errors start at that exact second.
    expiry = alerts.onset(b, spec.alert, rng)
    not_after = from_epoch_s(expiry).strftime("%a %b %d %H:%M:%S UTC %Y")
    end = b.now_ts + 1
    b.scale(service, "rps", expiry, end, rng.uniform(0.02, 0.06))  # only probes still arrive
    propagate(
        b, service, expiry, b.now_ts, 100.0, "tls_expired", rng,
        lines_per_min=2.0, not_after=not_after,
    )  # fmt: skip
    burst(b, service, expiry, b.now_ts, 3.0, rng, _handshake_line(service, b))
    herring_notes = tuple(herrings.apply(b, p) for p in planned)
    alerts.enforce_condition(b, spec.alert, spec.alerting_service)
    degrade_observability(b, spec, ())

    at = from_epoch_s(expiry).strftime("%H:%M:%S")
    expected = Expected(
        root_cause=(
            f"the TLS server certificate of {service} expired at {at} UTC (NotAfter: "
            f"{not_after}); every TLS handshake to {service} fails, so its callers return "
            "errors; nothing was deployed"
        ),
        root_cause_keywords=("certificate", "expired"),
        root_service=service,
        should_escalate=False,
        must_call_tools=("search_logs",),
        acceptable_actions=(Action(type="rotate_cert", target=service),),
        red_herrings=herring_notes,
    )
    alert = alerts.build_alert(b, spec.alert, spec.alerting_service, spec.severity)
    return ScenarioResult(alert, expected, common_tags(spec))


def _handshake_line(service: str, b: CaseBuilder) -> Callable[[Rng], catalog.Message]:
    """What the server side logs when clients reject its expired certificate."""

    def make(rng: Rng) -> catalog.Message:
        caller = rng.choice(b.topology.dependents_of(service))
        ip = b.pod_ip(rng.choice(b.pods_at(caller, b.alert_ts)))
        port = rng.randint(32768, 60999)
        if service == "ledger-service":
            return "WARN", (
                f'msg="http: TLS handshake error from {ip}:{port}: remote error: '
                'tls: unknown certificate"'
            )
        if service == "payments-service":
            return "WARN", (
                "o.a.tomcat.util.net.NioEndpoint : Handshake failed for client connection from "
                f"IP address [{ip}] and port [{port}]: javax.net.ssl.SSLHandshakeException: "
                "Received fatal alert: certificate_expired"
            )
        return "ERROR", (
            "gunicorn.error: Error handling request (no URI read)\nssl.SSLError: "
            "[SSL: SSLV3_ALERT_CERTIFICATE_EXPIRED] sslv3 alert certificate expired (_ssl.c:2580)"
        )

    return make
