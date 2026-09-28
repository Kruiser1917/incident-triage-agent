# Golden dataset

95 simulated production incidents with answer keys. It is the yardstick for every later
phase: baseline loop vs graph, prompt versions, model routing. Everything is generated
deterministically from code (ADR-0003); nothing here is hand-edited.

```
make dataset            # regenerate fixtures/ and evals/datasets/golden.jsonl
make validate-dataset   # schema, balance, solvability checks
make show-case CASE=oom_kill-001 [ANSWER=1]   # human-readable view, answer hidden by default
```

## The simulated system

A fictional payments company. `depends_on` means "calls".

| Service | Runtime | Calls | Notes |
|---|---|---|---|
| api-gateway | Go (Envoy-style access logs) | orders, payments | public entry point |
| orders-service | Python (gunicorn, SQLAlchemy) | postgres, redis, payments | DB pool 20 per pod |
| payments-service | Java (Spring Boot) | ledger, redis, payment-provider-api | |
| ledger-service | Go (logfmt, pgx) | postgres | DB pool 25 per pod |
| postgres | PostgreSQL 16 | | max_connections 200 |
| redis | Redis | | cache and idempotency keys |
| payment-provider-api | external | | metrics seen at our egress, no logs |

Source of truth: [`fixtures/topology.yaml`](../fixtures/topology.yaml).

## Anatomy of a case

`fixtures/<case_id>/`:

| File | Content |
|---|---|
| `alert.json` | Alertmanager webhook (v4) with one firing alert |
| `logs.jsonl` | one JSON line per log record: `ts`, `service`, `pod`, `level`, `source` (`app`/`k8s`), `msg`; window T-60m .. T+5m |
| `metrics.json` | raw per-minute series per service and metric (`null` = scrape gap); window T-3h .. T+5m |
| `deploys.json` | 7 days of deploys; `change_type` is `code` (semver) or `config` (revision `cfg-NNNN`) |

T is the moment the alert fires; the investigation starts at T+5m. Tools (phase 2) summarize
this raw data; they never hand raw series to the model.

`evals/datasets/golden.jsonl` holds one record per case: `id`, `class`, `difficulty`,
`split`, `fixture_dir`, `alert`, `expected` (root cause text, keywords, root service,
`should_escalate`, tools that must / must not be called, acceptable actions, red herrings,
optional injection) and `tags`. Schema: `GoldenCase` in `src/triage/simulator/schemas.py`.

## Incident classes

| Class | What breaks | Evidence the agent can find | Acceptable action |
|---|---|---|---|
| `oom_kill` | memory leak after a release, or an unbounded in-process cache | sawtooth memory, `OOMKilled (exit code 137)`, GC pauses / `SIGKILL`, restarts, callers' connection errors | rollback, restart |
| `db_connection_leak` | a release never returns DB connections | pool usage climbs to the limit, `QueuePool limit ... reached` / `failed to acquire connection`, latency pinned at the pool timeout | rollback, restart |
| `bad_deploy` | a release fails a request path (or makes it slow) | errors step up as new pods start, a new exception only on new pods, or slow requests with many DB queries | rollback |
| `disk_full` | WAL archiving fails (403 from object storage), or debug logging fills a volume | disk grows to 90/100%, `archive command failed`, `PANIC ... No space left on device`, `recovery mode`, DEBUG lines | clear_disk, revert_config, escalate |
| `upstream_throttling` | provider answers 429; immediate retries amplify (retry config change or traffic spike) | `429 Too Many Requests ... retrying immediately`, egress rate multiplied, `Retry-After` ignored | revert_config, escalate (never scale) |
| `cert_expired` | a service's TLS server certificate expires | callers' TLS errors (`certificate has expired`, `CertificateExpiredException: NotAfter`), the service itself goes quiet | rotate_cert |
| `bad_config` | a hot-reloaded config change (timeouts, pool size) | config revision in deploys and "applied config revision" log line, symptoms seconds later | revert_config |
| `dependency_cascade` | root in a dependency: redis at maxmemory with noeviction, a long transaction holding row locks, a slow ledger release | errors loudest at the consumer; walk the topology down to the first unhealthy dependency | revert_config, escalate, rollback |
| `trap` | not enough evidence: missing telemetry, two simultaneous causes, a spike already over, opaque errors | the right answer is to escalate and say what is missing | escalate |
| `prompt_injection` | an ordinary incident whose logs carry planted instructions (User-Agent, order note, merchant descriptor, provider error body, ledger memo) | as for the base incident; the planted action must never be proposed | as for the base incident |

Some classes imitate each other on purpose: a 2-connection DB pool (`bad_config`) looks like
a connection leak; a harmless config change right before the alert looks like a culprit.

## Variation axes

Each main class has 10 variants designed as an explicit table (not sampled), over:

- **root service** and **flavor** of the root cause;
- **alert placement**: on the root service, or on a caller one or two hops up (`alert_on_caller`);
- **alert kind**: `HighErrorRate` (5xx > 5% for 2m), `HighLatencyP95` (> 1s for 5m),
  `PodRestartsHigh` (3+ restarts in 1h), `DiskUsageHigh` (> 90% for 10m);
- **noise**: routine log volume and background warnings/errors (low / medium / high);
- **red herrings**: an unrelated deploy minutes before the alert, a harmless config change
  on the same service, a redis latency spike, a short burst of provider 429s;
- **incomplete data**: dropped series (`missing_metrics`) or scrape gaps (`metric_gaps`),
  and in traps a missing log stream;
- **timing**: time of day (traffic level), age of the causing change (minutes to days).

Difficulty per main class: 4 easy / 4 medium / 2 hard.

- **easy**: the alert is on the root service, low noise, no red herrings, full data.
- **medium**: one complication: medium/high noise, a red herring, a caller alert or gaps.
- **hard**: several at once, e.g. a caller alert two hops away, missing key metric, two
  red herrings, a cause that happened long before the alert.

## Split (ADR-0002)

| | dev | test |
|---|---|---|
| each main class | 3 (one easy, one medium, one hard) | 7 |
| trap | 2 | 8 |
| prompt_injection | 1 | 4 |
| **total** | **27** | **68** |

Prompts and the agent are developed on `dev` only; `test` is run for reported numbers.

## Guarantees (enforced by tests and the validator)

- Byte-for-byte determinism, across runs and `PYTHONHASHSEED` values; the committed files
  equal the generator's output (CI regenerates on Linux).
- Every answer keyword is findable in what the agent can reach (logs, deploys, alert,
  metric names); every referenced service and rollback version exists.
- The alert never contradicts the metrics: its condition holds at fire time, and it did not
  hold long before (an alert with `for 2m` fires within minutes of the condition starting).
- The root service is reachable from the alerting service through the topology.
- Each class shows its signature in the logs; injected actions are never acceptable ones;
  traps expect escalation and nothing else.

## Known simplifications

- **Synthetic templates.** Log lines come from templates written by the same people who
  build the agent. The held-out test split and manual review mitigate, not remove, this bias.
- **Logs are a sample**, about 1–2 routine lines per minute per service, not the full stream.
- **One alert per case**, always firing; no grouping, inhibition or flapping history.
- **Java OOM** shows both `OutOfMemoryError: Java heap space` and a container OOMKill (137).
  With `-XX:+ExitOnOutOfMemoryError` a JVM would rather exit with code 3; both happen in practice.
- **Memory** is the maximum over the service's pods; pods of a service fill up in step.
- **Config changes are hot-reloaded**; only code deploys roll out new pods.
- **Internal traffic is TLS** on port 8443; each service terminates TLS itself (no mesh).
- **The external provider** has no logs, only egress metrics.
- Root-cause texts in the answer key are one reference phrasing; evals grade meaning
  (keyword pre-check, then an LLM judge), not wording.

## Numbers

95 cases, ~17 MB (logs dominate). Difficulty: 37 easy, 38 medium, 20 hard (traps and
injections have their own mix). Tag counts: `alert_on_caller` 46, `red_herring` 36,
`noisy_logs` 28, `missing_metrics` 7, `metric_gaps` 7, `cascade` 10, `trap` 10,
`prompt_injection` 5.
