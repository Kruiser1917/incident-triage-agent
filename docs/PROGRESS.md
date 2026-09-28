# Progress

## Phase 0 — scaffold: done (PR #1, merged 2026-09-25)

Repo skeleton, compose (Postgres + pgvector; Langfuse under profile `obs`), Makefile, CI,
typed config loader, CLAUDE.md/AGENTS.md, ADR-0001 (stack), ADR-0002 (dev/test split).

## Current phase: 1 — simulator and golden set (in progress, draft PR #2)

### Done
- Simulator core: schemas, topology (7 services), deterministic RNG, case builder,
  "normal day" background, 4 red-herring types, Alertmanager payloads.
- `oom_kill`: 10 designed variants (4/4/2, 3 dev / 7 test), two flavors.
- `make dataset | validate-dataset | show-case CASE=... [ANSWER=1]`.
- Tests: byte-level determinism (in-process and across PYTHONHASHSEED), committed dataset
  equals generator output, validator negative cases, oom_kill invariants. CI confirms
  Linux output equals macOS output.
- ADR-0003 (deterministic generation, committed fixtures).

### Open
- **Checkpoint:** owner reviews oom_kill sample cases for realism (`make show-case`).
- Then: remaining 7 classes, 10 traps, 5 injection cases; `docs/DATASET.md`;
  owner reviews 10 random cases (phase 1 DoD); 5 interview questions.

### Decisions
- Presented as a personal project.
- Langfuse in an opt-in compose profile.
- Held-out test split (ADR-0002); dev = one easy/medium/hard per class + 2 traps + 1 injection.
- Cost tracked in RUB as reported by the provider, converted with a fixed rate.
- 2026-09-25: owner orchestrates and reviews, Claude implements and explains
  ([OWNER DECIDES] instead of [OWNER WRITES]; manual labeling stays manual).
- 2026-09-28: fixtures committed to git (ADR-0003); sample review after the first class.
- Config changes are hot-reloaded (no pod restart); only code deploys roll out new pods.

### Notes
- Provider budget is small (checked 2026-09-22); phase 1 uses no LLM calls.
- Dataset size estimate: ~180 KB per case, ~17 MB for 95 cases.

### Next step
Owner reviews oom_kill-001 and oom_kill-010; apply feedback; build remaining classes.
