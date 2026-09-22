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

### Open
- **[OWNER WRITES]** `src/triage/config.py` — make `tests/test_config.py` pass.

- Open PR `phase-0/scaffold` -> `main`; phase 0 interview questions.

### Decisions
- Presented as a personal project.
- Langfuse in an opt-in compose profile.
- Held-out test split (ADR-0002).
- Cost tracked in RUB as reported by the provider, converted with a fixed rate.

### Notes
- Provider budget is small (checked 2026-09-22); prefer cheap models during development.

### Next step
Owner implements the config loader; then review, PR, phase 0 questions.
