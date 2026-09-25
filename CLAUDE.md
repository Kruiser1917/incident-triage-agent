# CLAUDE.md / AGENTS.md

Guidance for AI coding agents working in this repository. `AGENTS.md` is a symlink to this file.

## Project

An LLM agent that triages production alerts. It takes an Alertmanager-compatible webhook
payload, investigates via read-only tools (logs, metrics, deploy history, service topology,
runbook RAG), forms a root-cause hypothesis grounded in tool results, and proposes a
remediation. **State-changing actions are only proposed, never executed**; a human approves
them. Output is a structured `TriageReport`.

Infrastructure is fully simulated by deterministic, seeded fixtures (a fictional fintech
system: api-gateway, payments-service, orders-service, ledger-service, postgres, redis,
external payment-provider-api). This gives a reproducible golden eval set of 95 cases:
8 incident classes x 10 variations, 10 trap cases (correct answer: escalate), and 5
prompt-injection cases.

This is a personal learning/portfolio project. The owner is learning agentic engineering
and must be able to explain every decision in an interview.

## Working mode (mentor mode)

- Work strictly phase by phase (see `docs/PROGRESS.md`). Do not build parts of later phases.
- Before coding a phase: a 5–10 point plan, then wait for the owner's approval.
- Every non-trivial architectural decision gets an ADR in `docs/adr/NNNN-short-title.md`
  (context, decision, alternatives, consequences).
- The owner works as a tech lead directing an AI assistant: Claude implements; the owner
  sets goals, makes design decisions and reviews. After each non-trivial piece, explain
  how it works and why, in plain language with everyday analogies (not DevOps ones), so
  the owner can defend it in an interview.
- Parts the brief marked as owner-written are now **[OWNER DECIDES]**: Claude presents
  options with trade-offs, the owner chooses, Claude implements and explains.
  Exception: manual labeling for judge calibration stays manual (it is the human ground truth).
- End each phase with 5 interview-style questions about the work.
- Be direct about bad ideas; propose alternatives.
- Conversation with the owner is in Russian; everything in the repo is in English.

## Conventions

- Python 3.12, `uv` for dependencies. Pydantic v2 for all schemas.
- `make lint` (ruff + ruff format + mypy --strict) and `make test` must pass.
- Unit tests never call a real LLM (mocks or recorded responses).
  Integration tests are marked `@pytest.mark.integration` and need `make up`.
- Add a dependency only in the phase that needs it.
- Conventional Commits, small commits, one branch per phase (`phase-N/<slug>`), PR into `main`.
- Secrets only via `.env` (template: `.env.example`). Never commit keys.
- Models are never hardcoded: provider + model per step live in `config/models.yaml`;
  stop conditions in `config/limits.yaml`.
- No multi-agent setups, extra frameworks or microservices unless an ADR justifies them
  with measured numbers.

## LLM provider and budget

- Provider: polza.ai, OpenAI-compatible (`LLM_BASE_URL`, `LLM_API_KEY`). It reports cost per
  call in `usage.cost_rub`; convert to USD with `rub_per_usd` from `config/limits.yaml`.
- The API budget is small. During development run a 5–10 case subset. Full eval runs only
  on the owner's explicit command. Log tokens and cost of every run.

## Evaluation hygiene

- The golden set is split into `dev` (for prompt/agent iteration) and a held-out `test`
  split used only for reported numbers (ADR-0002). Never tune prompts on `test` cases and
  never read `test` fixtures while writing prompts.

## Layout

- `src/triage/` — package (`agent/`, `tools/`, `llm/`, `mcp_server/`, `api/`, `obs/`, `simulator/`)
- `config/` — model routing and limits
- `fixtures/`, `runbooks/`, `evals/` — simulated data, RAG corpus, eval datasets/runners
- `docs/` — `PROGRESS.md` (read at session start, update at session end), `adr/`, `results/`
- `infra/` — service init scripts for docker compose

## Commands

`make install | up | up-obs | down | test | test-integration | lint | format | evals`
