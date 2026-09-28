"""Build every case from its spec and write fixtures plus the golden set.

Each case gets its own seed derived from a master seed and its id, so adding or changing
one case never reshuffles the others.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from triage.simulator.builder import CaseBuilder
from triage.simulator.clock import DAY, HOUR, MINUTE, epoch_s
from triage.simulator.rng import Rng, derive_seed
from triage.simulator.scenarios import SCENARIOS
from triage.simulator.schemas import GoldenCase, Topology
from triage.simulator.spec import CaseSpec
from triage.simulator.topology import load_topology

MASTER_SEED = 20260922
FIRST_DAY = epoch_s(datetime(2026, 3, 2, tzinfo=UTC))  # cases fall within four weeks from here
FIXTURE_FILES = ("alert.json", "logs.jsonl", "metrics.json", "deploys.json")
GOLDEN_PATH = Path("evals/datasets/golden.jsonl")
TOPOLOGY_PATH = Path("fixtures/topology.yaml")


@dataclass(frozen=True)
class BuiltCase:
    golden: GoldenCase
    files: dict[str, str]


def all_specs() -> tuple[CaseSpec, ...]:
    return tuple(spec for scenario in SCENARIOS for spec in scenario.variants)


def build_case(spec: CaseSpec, topology: Topology) -> BuiltCase:
    rng = Rng(derive_seed(MASTER_SEED, spec.case_id))
    timing = rng.derive("alert-time")
    alert_ts = (
        FIRST_DAY
        + timing.randint(0, 27) * DAY
        + spec.alert_hour * HOUR
        + timing.randint(0, 59) * MINUTE
        + timing.choice((0, 15, 30, 45))
    )
    b = CaseBuilder(spec.case_id, topology, rng, alert_ts, spec.noise)
    scenario = next(s for s in SCENARIOS if spec in s.variants)
    result = scenario.build(b, spec)
    golden = GoldenCase(
        id=spec.case_id,
        incident_class=spec.incident_class,
        difficulty=spec.difficulty,
        split=spec.split,
        fixture_dir=f"fixtures/{spec.case_id}",
        alert=result.alert.summary,
        expected=result.expected,
        tags=result.tags,
    )
    files = {
        "alert.json": json.dumps(result.alert.payload, indent=2, sort_keys=True) + "\n",
        "logs.jsonl": b.render_logs(),
        "metrics.json": b.render_metrics(),
        "deploys.json": b.render_deploys(),
    }
    return BuiltCase(golden, files)


def generate(root: Path) -> list[GoldenCase]:
    """(Re)generate all fixtures under ``root``; returns the golden records written."""
    topology = load_topology(root / TOPOLOGY_PATH)
    cases = [build_case(spec, topology) for spec in all_specs()]
    for case in cases:
        case_dir = root / case.golden.fixture_dir
        case_dir.mkdir(parents=True, exist_ok=True)
        for name, content in case.files.items():
            (case_dir / name).write_text(content, encoding="utf-8", newline="\n")
    golden_path = root / GOLDEN_PATH
    golden_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(c.golden.model_dump(mode="json"), sort_keys=True) for c in cases]
    golden_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return [c.golden for c in cases]
