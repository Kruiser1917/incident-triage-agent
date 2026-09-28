"""Signals shared by several scenarios.

When a dependency fails, its callers fail too, and each caller logs the failure in its own
runtime's words. Getting these secondary symptoms right is what makes cascades realistic:
the loudest errors are usually not at the root.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Literal

from triage.simulator import catalog
from triage.simulator.builder import CaseBuilder
from triage.simulator.catalog import Message
from triage.simulator.clock import MINUTE
from triage.simulator.rng import Rng

Reason = Literal["refused", "timeout", "server_error", "tls_expired"]

GATEWAY_ROUTES = {"payments-service": "/api/v1/payments", "orders-service": "/api/v1/orders"}
ENVOY_TLS_ERROR = (
    "reset reason: connection failure, transport failure reason: TLS_error:|268435581:"
    "SSL routines:OPENSSL_internal:CERTIFICATE_VERIFY_FAILED:TLS_error_end"
)


def caller_error_line(
    caller: str, callee: str, reason: Reason, rng: Rng, not_after: str = ""
) -> Message:
    """How ``caller`` logs a failed request to ``callee``. ``not_after`` is the expiry date
    of the callee's certificate, which Java includes in its TLS errors."""
    if caller == "api-gateway":
        return "ERROR", _gateway_line(callee, reason, rng)
    if caller == "orders-service":
        return "ERROR", _orders_line(reason)
    if caller == "payments-service":
        return "ERROR", _payments_line(callee, reason, rng, not_after)
    raise ValueError(f"no caller template for {caller} -> {callee}")


def _gateway_line(callee: str, reason: Reason, rng: Rng) -> str:
    path = GATEWAY_ROUTES.get(callee, "/api/v1/orders")
    if reason == "refused":
        return catalog.gateway_access(
            rng, "POST", path, 503, callee, rng.randint(1, 5),
            'flags=UF err="upstream connect error or disconnect/reset before headers. '
            'reset reason: connection failure"',
        )  # fmt: skip
    if reason == "timeout":
        return catalog.gateway_access(rng, "POST", path, 504, callee, 15000, "flags=UT")
    if reason == "tls_expired":
        return catalog.gateway_access(
            rng, "POST", path, 503, callee, rng.randint(2, 8),
            f'flags=UF err="upstream connect error or disconnect/reset before headers. '
            f'{ENVOY_TLS_ERROR}"',
        )  # fmt: skip
    status = rng.choice((500, 502))
    return catalog.gateway_access(rng, "POST", path, status, callee, rng.randint(20, 400))


def _orders_line(reason: Reason) -> str:
    # Internal traffic is TLS (mTLS between services) on port 8443.
    prefix = "orders.clients.payments: POST https://payments-service:8443/v1/payments"
    pool = "HTTPSConnectionPool(host='payments-service', port=8443)"
    if reason == "refused":
        return (
            f'{prefix} failed: ConnectionError("{pool}: Max retries exceeded with url: '
            "/v1/payments (Caused by NewConnectionError('Failed to establish a new connection: "
            "[Errno 111] Connection refused'))\")"
        )
    if reason == "timeout":
        return f'{prefix} failed: ReadTimeout("{pool}: Read timed out. (read timeout=5)")'
    if reason == "tls_expired":
        return (
            f'{prefix} failed: SSLError(MaxRetryError("{pool}: Max retries exceeded with url: '
            "/v1/payments (Caused by SSLError(SSLCertVerificationError(1, '[SSL: "
            "CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired "
            "(_ssl.c:1000)')))\"))"
        )
    return f"{prefix} returned 502: payment authorization failed"


def _payments_line(callee: str, reason: Reason, rng: Rng, not_after: str) -> str:
    head = (
        "c.a.p.ledger.LedgerClient : failed to post ledger entry "
        f"paymentId={catalog.payment_id(rng)}"
    )
    url = f"https://{callee}:8443/v1/entries"
    if reason == "refused":
        return f'{head}: I/O error on POST request for "{url}": Connection refused'
    if reason == "timeout":
        return f'{head}: I/O error on POST request for "{url}": Read timed out'
    if reason == "tls_expired":
        cause = (
            f"; nested exception is java.security.cert.CertificateExpiredException: "
            f"NotAfter: {not_after}"
            if not_after
            else ""
        )
        return (
            f'{head}: I/O error on POST request for "{url}": PKIX path validation failed: '
            f"java.security.cert.CertPathValidatorException: validity check failed{cause}"
        )
    return f"{head}: 500 Internal Server Error from POST {url}"


def propagate(
    b: CaseBuilder,
    callee: str,
    start: int,
    end: int,
    error_pct: float,
    reason: Reason,
    rng: Rng,
    *,
    lines_per_min: float = 1.0,
    depth: int = 2,
    not_after: str = "",
) -> None:
    """Callers of ``callee`` fail a share of their requests and log it, up to ``depth`` hops.

    Each hop only passes on a fraction of its errors: a caller needs the callee for some of
    its requests, not all of them.
    """
    for caller in b.topology.dependents_of(callee):
        if b.topology.services[caller].kind != "service":
            continue
        added = error_pct * rng.uniform(0.15, 0.3)
        b.add(caller, "error_rate", start, end, added)
        burst(
            b, caller, start, end, lines_per_min, rng,
            partial(caller_error_line, caller, callee, reason, not_after=not_after),
        )  # fmt: skip
        if depth > 1:
            # One hop up, the caller answers with a 5xx of its own.
            propagate(
                b, caller, start, end, added, "server_error", rng,
                lines_per_min=lines_per_min / 2, depth=depth - 1,
            )  # fmt: skip


def burst(
    b: CaseBuilder,
    service: str,
    start: int,
    end: int,
    per_minute: float,
    rng: Rng,
    make: Callable[[Rng], Message],
    *,
    pod: str | None = None,
) -> None:
    """Emit about ``per_minute`` lines per minute from ``make`` at random times in [start, end)."""
    minutes = max(0.0, (end - start) / MINUTE)
    for _ in range(int(minutes * per_minute + rng.random())):
        level, msg = make(rng)
        b.log(rng.randint(start * 1000, end * 1000 - 1), service, level, msg, pod=pod)
