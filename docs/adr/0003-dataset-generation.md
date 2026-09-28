# 0003. Deterministic dataset generation, committed to git

- Status: accepted
- Date: 2026-09-28

## Context

The golden set (95 cases) is the yardstick for every later phase: baseline vs graph,
prompt versions, model routing. A yardstick must not move by accident. Cases are synthetic,
so they are produced by a generator; the questions are how to make generation reproducible,
how to control what the cases cover, and where the output lives.

## Decision

1. **Explicit variant tables, random details.** Each incident class declares its 10 variants
   as a table (`CaseSpec`): root service, difficulty, noise level, red herrings, missing
   data, alert kind, timing. Randomness only fills in details (exact values, timestamps,
   log lines). Coverage of the variation axes is designed, not sampled.
2. **Per-case seeds.** `seed = sha256(master_seed, case_id)`; components draw from derived
   sub-streams (`rng.derive("metric", service, metric)`). Adding or editing one case, or
   one component, does not reshuffle any other.
3. **Cross-version, cross-platform determinism.**
   - Only `random.Random.random()` is used as a primitive: CPython guarantees its sequence
     across versions, but not that of `randint`, `choice` or `gauss`.
   - Only IEEE 754 basic arithmetic on generated values (exactly rounded everywhere);
     no `math.sin`/`log`, whose libm results may differ in the last bit between platforms.
     The daily traffic curve is a 24-point table with linear interpolation.
   - Integer epoch timestamps, no local time, no `now()`.
   - No iteration over sets of strings (order depends on `PYTHONHASHSEED`); JSON is written
     with sorted keys and fixed rounding.
4. **Fixtures are committed.** The dataset is frozen in git; any change shows up as a diff in
   review. A test regenerates everything and compares it byte for byte with the committed
   files, so a generator change without regeneration (or a hand edit) fails CI. Another test
   generates in two processes with different `PYTHONHASHSEED` values.
5. **Solvability is validated.** Every answer keyword must be findable in what the agent can
   reach through tools; every referenced service and rollback version must exist.
6. **Raw data in fixtures, summaries in tools.** Fixtures hold raw per-minute series and log
   lines; tools (phase 2) summarize them, as the brief requires.

## Alternatives considered

- **Generate on the fly** (`make fixtures`, only a checksum manifest in git): lighter repo,
  but dataset changes are invisible in review (only hashes change). Rejected: the dataset is
  the yardstick, its changes deserve a readable diff.
- **Fully random variants** (sample axes per case): simpler code, but some combinations
  would never appear and difficulty balance would drift. Rejected.
- **numpy for noise and curves**: convenient, but adds a dependency and its own
  cross-platform float caveats for no gain at this scale.
- **LLM-written fixtures**: realistic prose, but not reproducible, costly, and the answer key
  could silently disagree with the data. Rejected; LLMs are what we evaluate, not the source
  of truth.

## Consequences

- Repository size: about 180 KB per case, roughly 17 MB for 95 cases (logs dominate).
- Changing a template regenerates many files; such PRs are large but mechanical, and the
  determinism test proves they came from the generator.
- Synthetic logs follow templates the agent's authors know. Mitigation: the held-out test
  split (ADR-0002) and manual realism review of sampled cases.
