# 0001. Core tech stack

- Status: accepted
- Date: 2026-09-22

## Context

We need an alert-triage agent whose quality, cost and latency can be measured
reproducibly, that runs locally with one command, and whose every part the author can
explain. The API budget is small, so provider access must be cheap and model choice
per step must be a config change.

## Decision

| Concern | Choice |
|---|---|
| Language / deps | Python 3.12, `uv` (lockfile, fast CI installs) |
| Schemas | Pydantic v2 everywhere (tool contracts, state, reports, config) |
| Orchestration | Hand-written ReAct loop first (phase 3 baseline), then LangGraph (phase 4) |
| LLM access | polza.ai via the OpenAI-compatible API, behind a thin in-house client |
| Storage | One Postgres 17 + pgvector: runbook vectors, `tsvector` full-text, LangGraph checkpoints |
| Tracing | Self-hosted Langfuse v3, in a separate compose profile `obs` |
| API | FastAPI (REST + SSE) |
| Quality | ruff, mypy --strict, pytest; GitHub Actions |

Provider probe (2026-09-22) confirmed: tool calling, strict `json_schema` structured output,
embeddings (`openai/text-embedding-3-small`, 1536 dims), and per-call cost returned as
`usage.cost_rub`. Costs are stored in RUB as reported and converted to USD with a fixed
`rub_per_usd` from `config/limits.yaml` so reported numbers are reproducible.

Initial models are all `deepseek/deepseek-v4-flash` (cheapest capable model at probe time);
routing to stronger models for `hypothesize` is decided in later phases by measurement.

## Alternatives considered

- **LangGraph from day one** — skipping the baseline hides what the framework actually buys
  us; the phase 3 vs phase 4 comparison is the evidence.
- **PydanticAI / OpenAI Agents SDK** — lighter, but weaker checkpointing and
  interrupt/resume for human-in-the-loop; LangGraph's are first-class.
- **Direct Anthropic/OpenAI SDKs** — separate keys and billing per vendor; one
  OpenAI-compatible gateway gives access to all models behind one interface.
- **LiteLLM** — useful for many providers; with a single OpenAI-compatible gateway it is an
  extra dependency without benefit. Revisit if we add a second provider.
- **Qdrant / Weaviate** — extra service to run; pgvector keeps vectors, full-text search and
  checkpoints in one database, enough for ~50 runbooks.
- **LangSmith / Arize Phoenix** — LangSmith is SaaS-only for free use; Phoenix is lighter
  but Langfuse has better prompt versioning and cost tracking. Langfuse v3 needs ~6
  containers (ClickHouse, Redis, MinIO, Postgres), hence the opt-in `obs` profile.

## Consequences

- `make up` stays light (one container); tracing requires `make up-obs`.
- All model calls go through one client, which is where tokens and cost get logged.
- We depend on a single gateway; the OpenAI-compatible interface keeps switching cheap.
