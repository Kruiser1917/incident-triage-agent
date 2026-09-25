# 0002. Dev / held-out test split of the golden set

- Status: accepted
- Date: 2026-09-22

## Context

The same people (and AI assistant) write both the fixture generator and the agent prompts.
If prompts are iterated against all 95 cases, reported accuracy measures overfitting to
the templates, not generalisation.

## Decision

Split the golden set, stratified by class and difficulty, into:

- `dev` (~25 cases): used freely while developing tools, prompts and the graph.
- `test` (~70 cases): held out; run only for reported numbers, on explicit command.

Each record gets a `split` field. The split is fixed by seed and checked by the dataset
validator. Prompt authors do not read `test` fixtures.

## Alternatives considered

- **Single set** (original brief) — simpler, but results are not credible.
- **k-fold cross-validation** — standard for training models, but we do not train; and it
  multiplies API cost.

## Consequences

- Development metrics (dev) and reported metrics (test) can diverge; that gap is itself a
  reported number.
- ~25 dev cases is small; per-class dev coverage is ~3 cases, so dev metrics are noisy.
