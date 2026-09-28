"""Log message and deploy description templates.

Each service logs in the style of its runtime (Envoy access logs, Python logging with
gunicorn, Spring Boot, Go logfmt, PostgreSQL, Redis), so the agent must read heterogeneous
logs the way an on-call engineer does. Messages hold only the text after timestamp and level;
those live in separate fields of the log line.
"""

from __future__ import annotations

from triage.simulator.rng import Rng
from triage.simulator.schemas import LogLevel

Message = tuple[LogLevel, str]

K8S_ALPHABET = "bcdfghjklmnpqrstvwxz2456789"  # alphabet of Kubernetes generated name suffixes
ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
HEX = "0123456789abcdef"

USER_AGENTS = (
    "okhttp/4.12.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "merchant-sdk-python/2.8.1",
    "merchant-sdk-node/5.3.0",
)

AUTHORS = ("a.kowalski", "m.chen", "s.patel", "j.garcia", "l.nguyen", "r.okafor", "deploy-bot")


def order_id(rng: Rng) -> str:
    return "ord_" + rng.token(12, ID_ALPHABET)


def payment_id(rng: Rng) -> str:
    return "pay_" + rng.token(14, ID_ALPHABET)


def account_id(rng: Rng) -> str:
    return "acc_" + rng.token(10, ID_ALPHABET)


def request_id(rng: Rng) -> str:
    return rng.token(16, HEX)


# --- api-gateway (Envoy-style access log) -----------------------------------------------


def gateway_access(
    rng: Rng, method: str, path: str, status: int, upstream: str, duration_ms: int, extra: str = ""
) -> str:
    size = rng.randint(180, 4200) if status < 400 else rng.randint(40, 220)
    line = (
        f'"{method} {path} HTTP/1.1" {status} upstream={upstream} '
        f'duration_ms={duration_ms} bytes={size} ua="{rng.choice(USER_AGENTS)}"'
    )
    return f"{line} {extra}" if extra else line


def _gateway_normal(rng: Rng) -> Message:
    roll = rng.random()
    if roll < 0.35:
        return "INFO", gateway_access(
            rng, "GET", f"/api/v1/orders/{order_id(rng)}", 200, "orders-service", rng.randint(8, 60)
        )
    if roll < 0.55:
        return "INFO", gateway_access(
            rng, "POST", "/api/v1/orders", 201, "orders-service", rng.randint(40, 180)
        )
    if roll < 0.80:
        return "INFO", gateway_access(
            rng, "POST", "/api/v1/payments", 201, "payments-service", rng.randint(120, 420)
        )
    if roll < 0.92:
        return "INFO", gateway_access(
            rng,
            "GET",
            f"/api/v1/payments/{payment_id(rng)}",
            200,
            "payments-service",
            rng.randint(10, 70),
        )
    if roll < 0.97:
        return "INFO", gateway_access(
            rng, "GET", f"/api/v1/orders/{order_id(rng)}", 404, "orders-service", rng.randint(5, 20)
        )
    return "INFO", gateway_access(
        rng, "POST", "/api/v1/payments", 401, "payments-service", rng.randint(2, 6)
    )


def _gateway_issue(rng: Rng) -> Message:
    roll = rng.random()
    if roll < 0.5:
        client = "cli_" + rng.token(8, ID_ALPHABET)
        return "WARN", f"rate limit exceeded client_id={client} limit=150rps; responding 429"
    return "ERROR", gateway_access(
        rng, "POST", "/api/v1/payments", 504, "payments-service", 15000, "flags=UT"
    )


# --- orders-service (Python logging + gunicorn) ------------------------------------------


def _orders_normal(rng: Rng) -> Message:
    roll = rng.random()
    if roll < 0.35:
        return (
            "INFO",
            f"orders.api: GET /api/v1/orders/{order_id(rng)} 200 in {rng.randint(6, 45)}ms",
        )
    if roll < 0.55:
        return "INFO", f"orders.api: POST /api/v1/orders 201 in {rng.randint(35, 160)}ms"
    if roll < 0.75:
        return "INFO", f"orders.service: order {order_id(rng)} status pending -> confirmed"
    if roll < 0.92:
        return "INFO", f"orders.events: published order.confirmed order_id={order_id(rng)}"
    customer = "cus_" + rng.token(10, ID_ALPHABET)
    return "INFO", (
        f"orders.api: GET /api/v1/orders?customer_id={customer} 200 in {rng.randint(15, 90)}ms"
    )


def _orders_issue(rng: Rng) -> Message:
    roll = rng.random()
    if roll < 0.45:
        return "WARN", (
            f"orders.cache: redis GET order:{order_id(rng)} timed out after 50ms; "
            "falling back to postgres"
        )
    if roll < 0.8:
        return "WARN", (
            f"orders.api: POST /api/v1/orders 422 in {rng.randint(5, 15)}ms: "
            "currency 'XTS' is not supported"
        )
    merchant = rng.randint(100, 999)
    return "ERROR", (
        f"orders.webhooks: delivery to https://hooks.merchant-{merchant}.example/orders failed: "
        f"ReadTimeout (attempt {rng.randint(2, 5)}/5)"
    )


# --- payments-service (Spring Boot) --------------------------------------------------------


def _payments_normal(rng: Rng) -> Message:
    roll = rng.random()
    pid = payment_id(rng)
    if roll < 0.35:
        return "INFO", (
            f"c.a.p.api.PaymentController : POST /v1/payments status=201 "
            f"duration={rng.randint(110, 400)}ms paymentId={pid}"
        )
    if roll < 0.65:
        return "INFO", (
            f"c.a.p.provider.ProviderClient : authorize approved paymentId={pid} "
            f"providerRef=psp_{rng.token(10, ID_ALPHABET)} latency={rng.randint(90, 320)}ms"
        )
    if roll < 0.85:
        return "INFO", (
            f"c.a.p.ledger.LedgerClient : posted ledger entry "
            f"entryId=ent_{rng.token(12, ID_ALPHABET)} paymentId={pid}"
        )
    return "INFO", (
        f"c.a.p.api.PaymentController : POST /v1/payments/{pid}/capture status=200 "
        f"duration={rng.randint(60, 220)}ms"
    )


def _payments_issue(rng: Rng) -> Message:
    roll = rng.random()
    if roll < 0.45:
        return "WARN", (
            f"c.a.p.provider.ProviderClient : authorize declined paymentId={payment_id(rng)} "
            "code=51 (insufficient funds)"
        )
    if roll < 0.8:
        return "WARN", (
            "c.a.p.provider.ProviderClient : read timeout after 2000ms calling "
            "payment-provider-api; retrying (attempt 2/3)"
        )
    return "ERROR", (
        f"c.a.p.api.ErrorHandler : idempotency conflict for key idk_{rng.token(12, ID_ALPHABET)}: "
        "request body differs from original request"
    )


# --- ledger-service (Go, logfmt) ---------------------------------------------------------


def _ledger_normal(rng: Rng) -> Message:
    roll = rng.random()
    rid = request_id(rng)
    if roll < 0.55:
        return "INFO", (
            f'msg="request completed" method=POST path=/v1/entries status=201 '
            f"duration_ms={rng.randint(4, 25)} request_id={rid}"
        )
    if roll < 0.95:
        return "INFO", (
            f'msg="request completed" method=GET path=/v1/accounts/{account_id(rng)}/balance '
            f"status=200 duration_ms={rng.randint(1, 9)} request_id={rid}"
        )
    return "INFO", (
        f'msg="balance snapshot refreshed" accounts={rng.randint(9000, 12000)} '
        f"duration_ms={rng.randint(180, 420)}"
    )


def _ledger_issue(rng: Rng) -> Message:
    if rng.random() < 0.5:
        return "WARN", (
            f'msg="slow query" duration_ms={rng.randint(500, 1400)} '
            'query="SELECT balance FROM account_balances WHERE account_id = $1 FOR UPDATE"'
        )
    return "WARN", (
        f'msg="request failed" method=POST path=/v1/entries status=409 '
        f'err="duplicate idempotency key" request_id={request_id(rng)}'
    )


# --- postgres / redis ------------------------------------------------------------------------


def _postgres_normal(rng: Rng) -> Message:
    roll = rng.random()
    if roll < 0.4:
        buffers = rng.randint(800, 4200)
        return "INFO", (
            f"LOG:  checkpoint complete: wrote {buffers} buffers "
            f"({buffers / 163.84:.1f}%); 0 WAL file(s) added, 0 removed, "
            f"{rng.randint(1, 4)} recycled; write={rng.uniform(20, 29):.3f} s"
        )
    if roll < 0.75:
        table = rng.choice(
            ("orders.public.orders", "ledger.public.entries", "orders.public.events")
        )
        return "INFO", (
            f'LOG:  automatic vacuum of table "{table}": index scans: 1, '
            f"pages: 0 removed, {rng.randint(20000, 90000)} remain"
        )
    app = rng.choice(("orders-service", "ledger-service"))
    return "INFO", (
        f"LOG:  connection authorized: user={app.split('-')[0]} "
        f"database={app.split('-')[0]} application_name={app}"
    )


def _postgres_issue(rng: Rng) -> Message:
    if rng.random() < 0.6:
        return "INFO", (
            f"LOG:  duration: {rng.uniform(1000, 2600):.3f} ms  statement: "
            "SELECT o.* FROM orders o "
            "JOIN order_items i ON i.order_id = o.id WHERE o.customer_id = $1 ORDER BY o.created_at"
        )
    return "ERROR", (
        'ERROR:  duplicate key value violates unique constraint "entries_idempotency_key_key"'
    )


def _redis_normal(rng: Rng) -> Message:
    roll = rng.random()
    if roll < 0.5:
        return "INFO", f"* Background saving started by pid {rng.randint(1000, 9000)}"
    if roll < 0.8:
        return "INFO", "* DB saved on disk"
    return "INFO", "* Background saving terminated with success"


NORMAL = {
    "api-gateway": _gateway_normal,
    "orders-service": _orders_normal,
    "payments-service": _payments_normal,
    "ledger-service": _ledger_normal,
    "postgres": _postgres_normal,
    "redis": _redis_normal,
}

ISSUES = {
    "api-gateway": _gateway_issue,
    "orders-service": _orders_issue,
    "payments-service": _payments_issue,
    "ledger-service": _ledger_issue,
    "postgres": _postgres_issue,
}


def startup_lines(service: str, app_version: str, rng: Rng) -> tuple[Message, ...]:
    """What a freshly started container prints."""
    if service == "payments-service":
        return (
            (
                "INFO",
                f"c.a.p.PaymentsApplication : Starting PaymentsApplication v{app_version[1:]}",
            ),
            (
                "INFO",
                f"c.a.p.PaymentsApplication : Started PaymentsApplication in "
                f"{rng.uniform(18, 27):.2f} seconds "
                f"(process running for {rng.uniform(28, 31):.1f})",
            ),
        )
    if service == "orders-service":
        return (
            ("INFO", "gunicorn.error: Starting gunicorn 22.0.0"),
            ("INFO", "gunicorn.error: Listening at: http://0.0.0.0:8000 (1)"),
            ("INFO", f"gunicorn.error: Booting worker with pid: {rng.randint(7, 12)}"),
        )
    return (
        ("INFO", f'msg="starting {service}" version={app_version} go=go1.23.4'),
        ("INFO", 'msg="http server listening" addr=:8080'),
    )


def config_reload_line(service: str, revision: str, description: str) -> str:
    """Config changes are hot-reloaded by the running process (no pod restart)."""
    if service == "payments-service":
        return f"c.a.p.config.ConfigWatcher : applied config revision {revision}: {description}"
    if service == "orders-service":
        return f"orders.config: reloaded config revision {revision}: {description}"
    return f'msg="config reloaded" revision={revision} change="{description}"'


# --- Deploy descriptions -------------------------------------------------------------------

CODE_CHANGES = {
    "api-gateway": (
        "Update CORS allowlist for partner dashboard",
        "Upgrade Envoy to 1.31.2",
        "Propagate x-request-id to upstream services",
        "Add /api/v1/payments/{id}/refunds route",
    ),
    "orders-service": (
        "Add pagination to order history endpoint",
        "Upgrade SQLAlchemy to 2.0.36",
        "Refactor order status webhooks",
        "Add retry jitter to webhook deliveries",
    ),
    "payments-service": (
        "Add 3DS2 frictionless flow for EU cards",
        "Bump Spring Boot to 3.3.5",
        "Improve idempotency key logging",
        "Export provider decline codes as metrics",
    ),
    "ledger-service": (
        "Batch ledger entry inserts",
        "Upgrade pgx to v5.7.1",
        "Add account statement endpoint",
        "Refactor balance recalculation job",
    ),
}

# Config changes that are safe by construction: used for background and red-herring deploys.
HARMLESS_CONFIG_CHANGES = {
    "api-gateway": (
        "Set LOG_LEVEL=info (was debug)",
        "Raise per-client rate limit to 200 req/s (was 150)",
    ),
    "orders-service": (
        "Set LOG_LEVEL=info (was debug)",
        "Enable feature flag order_export_csv",
    ),
    "payments-service": (
        "Set LOG_LEVEL=info (was debug)",
        "Enable feature flag checkout_banner_v2",
    ),
    "ledger-service": (
        "Set LOG_LEVEL=info (was debug)",
        "Set STATEMENT_PAGE_SIZE=200 (was 100)",
    ),
}
