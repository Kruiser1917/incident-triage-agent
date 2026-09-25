# Progress

## Current phase: 0 — scaffold (in progress)

### Done
- Repo skeleton, `pyproject.toml` (uv, Python 3.12), ruff/mypy/pytest config.
- `docker-compose.yml`: Postgres 17 + pgvector (default), Langfuse v3 stack (profile `obs`).
- `Makefile`: install, up, up-obs, down, test, test-integration, lint, format, evals (stub).
- CI: lint + unit tests; integration job that boots compose and checks pgvector.
- `config/models.yaml`, `config/limits.yaml`.
- CLAUDE.md (+ AGENTS.md symlink), ADR-0001 (stack), ADR-0002 (dev/test split).
- `make up-obs` verified locally: Langfuse 3.225.9 healthy on :3100, ~2.3 GB RAM total.
- Provider probe: tool calling, strict structured output, embeddings and `usage.cost_rub` all work.

- Typed config loader `src/triage/config.py`: 15/15 unit tests, lint and mypy clean.

### Open
- Open PR `phase-0/scaffold` -> `main`; phase 0 interview questions.

### Decisions
- Presented as a personal project.
- Langfuse in an opt-in compose profile.
- Held-out test split (ADR-0002).
- Cost tracked in RUB as reported by the provider, converted with a fixed rate.
- 2026-09-25: working mode changed — owner orchestrates and reviews, Claude implements and
  explains ([OWNER DECIDES] instead of [OWNER WRITES]; manual labeling stays manual).

### Notes
- Provider budget is small (checked 2026-09-22); prefer cheap models during development.

### Next step
Owner opens the PR; phase 0 interview questions; then phase 1 plan (simulator + golden set).
