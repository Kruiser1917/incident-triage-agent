# Progress

## Phase 0 — scaffold: done (PR #1, merged 2026-09-25)

Repo skeleton, compose (Postgres + pgvector; Langfuse under profile `obs`), Makefile, CI,
typed config loader, CLAUDE.md/AGENTS.md, ADR-0001 (stack), ADR-0002 (dev/test split).

## Current phase: 1 — simulator and golden set (in progress, draft PR #2)

### Done
- Simulator core: schemas, topology (7 services), deterministic RNG, case builder,
  "normal day" background, 4 red-herring types, Alertmanager payloads.
- All 95 cases: 8 classes x 10 designed variants (4/4/2), 10 traps, 5 prompt injections;
  split 27 dev / 68 test. `docs/DATASET.md` documents classes, axes and simplifications.
- `make dataset | validate-dataset | show-case CASE=... [ANSWER=1]`.
- Tests (939): byte-level determinism (in-process and across PYTHONHASHSEED), committed
  dataset equals generator output, validator negative cases, per-class invariants (class
  signature in logs, alert consistent with metrics and firing promptly, root reachable from
  the alerting service, traps/injections consistent). CI confirms Linux == macOS output.
- ADR-0003 (deterministic generation, committed fixtures).
- Owner reviewed oom_kill-001 and oom_kill-010 (guided walkthrough) and accepted the sample.

### Open
- Phase 1 DoD: owner reviews 10 random cases for realism/solvability.
- 5 interview questions for phase 1; mark PR #2 ready and merge.

### Decisions
- Presented as a personal project.
- Langfuse in an opt-in compose profile.
- Held-out test split (ADR-0002); dev = one easy/medium/hard per class + 2 traps + 1 injection.
- Cost tracked in RUB as reported by the provider, converted with a fixed rate.
- 2026-09-25: owner orchestrates and reviews, Claude implements and explains
  ([OWNER DECIDES] instead of [OWNER WRITES]; manual labeling stays manual).
- 2026-09-28: fixtures committed to git (ADR-0003); sample review after the first class.
- Config changes are hot-reloaded (no pod restart); only code deploys roll out new pods.
- Incident onsets are anchored to the alert rule's `for` window (alerts fire promptly).
- Internal service calls use TLS on 8443 (needed for the cert_expired class).

### Notes
- Provider budget is small (checked 2026-09-22); phase 1 uses no LLM calls.
- Dataset size estimate: ~180 KB per case, ~17 MB for 95 cases.

### Next step
Owner reviews 10 random cases; phase 1 interview questions; merge PR #2; phase 2 plan (tools).
