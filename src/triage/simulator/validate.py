"""Dataset validator: schema, file presence, balance, and solvability checks.

Beyond "does it parse", it checks that every case is solvable in principle: each answer
keyword must appear somewhere the agent can look, and every referenced service must exist.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from triage.simulator.generate import FIXTURE_FILES, GOLDEN_PATH, TOPOLOGY_PATH
from triage.simulator.schemas import (
    AlertmanagerWebhook,
    Deploy,
    GoldenCase,
    IncidentClass,
    LogLine,
    MetricsFile,
    Topology,
)
from triage.simulator.topology import load_topology

MAIN_CLASSES: tuple[IncidentClass, ...] = (
    "oom_kill",
    "db_connection_leak",
    "bad_deploy",
    "disk_full",
    "upstream_throttling",
    "cert_expired",
    "bad_config",
    "dependency_cascade",
)
CLASS_SIZE: dict[IncidentClass, int] = {c: 10 for c in MAIN_CLASSES} | {
    "trap": 10,
    "prompt_injection": 5,
}
DIFFICULTY_MIX = {"easy": 4, "medium": 4, "hard": 2}  # per main class
DEV_CASES: dict[IncidentClass, int] = {c: 3 for c in MAIN_CLASSES} | {
    "trap": 2,
    "prompt_injection": 1,
}
ID_PATTERN = re.compile(r"^(?P<cls>[a-z_]+)-(?P<num>\d{3})$")
_DEPLOYS = TypeAdapter(list[Deploy])


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)

    def render(self) -> str:
        lines = [f"cases: {sum(v for k, v in self.counts.items() if k.startswith('class:'))}"]
        lines += [f"  {k}: {v}" for k, v in sorted(self.counts.items())]
        lines += [f"WARNING {w}" for w in self.warnings]
        lines += [f"ERROR   {e}" for e in self.errors]
        lines.append("OK" if not self.errors else f"FAILED with {len(self.errors)} error(s)")
        return "\n".join(lines)


def validate(root: Path) -> Report:
    report = Report()
    topology = load_topology(root / TOPOLOGY_PATH)
    cases = _load_golden(root, report)
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            report.errors.append(f"duplicate id {case.id}")
        seen.add(case.id)
        report.counts[f"class:{case.incident_class}"] += 1
        report.counts[f"split:{case.split}"] += 1
        _check_case(root, case, topology, report)
    _check_balance(cases, report)
    _check_orphans(root, seen, report)
    return report


def _load_golden(root: Path, report: Report) -> list[GoldenCase]:
    path = root / GOLDEN_PATH
    if not path.exists():
        report.errors.append(f"{GOLDEN_PATH} not found")
        return []
    cases = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        try:
            cases.append(GoldenCase.model_validate_json(line))
        except ValidationError as error:
            report.errors.append(f"golden line {number}: {error.errors()[0]['msg']}")
    return cases


def _check_case(root: Path, case: GoldenCase, topology: Topology, report: Report) -> None:
    where = case.id
    match = ID_PATTERN.match(case.id)
    if not match or match["cls"] != case.incident_class:
        report.errors.append(f"{where}: id does not match '<class>-NNN'")
    if case.fixture_dir != f"fixtures/{case.id}":
        report.errors.append(f"{where}: fixture_dir should be fixtures/{case.id}")
    case_dir = root / case.fixture_dir
    missing = [name for name in FIXTURE_FILES if not (case_dir / name).exists()]
    if missing:
        report.errors.append(f"{where}: missing fixture files {missing}")
        return
    raw = {name: (case_dir / name).read_text() for name in FIXTURE_FILES}
    try:
        alert = AlertmanagerWebhook.model_validate_json(raw["alert.json"])
        logs = [LogLine.model_validate_json(line) for line in raw["logs.jsonl"].splitlines()]
        metrics = MetricsFile.model_validate_json(raw["metrics.json"])
        deploys = _DEPLOYS.validate_json(raw["deploys.json"])
    except ValidationError as error:
        report.errors.append(f"{where}: invalid fixture: {error.errors()[0]['msg']}")
        return

    labels = alert.alerts[0].labels
    if (labels.get("alertname"), labels.get("service")) != (case.alert.name, case.alert.service):
        report.errors.append(f"{where}: golden alert does not match alert.json")

    known = set(topology.services)
    referenced = {case.alert.service, *(a.target for a in case.expected.acceptable_actions)}
    referenced |= (
        {line.service for line in logs} | set(metrics.series) | {d.service for d in deploys}
    )
    if case.expected.root_service is not None:
        referenced.add(case.expected.root_service)
    unknown = sorted(referenced - known)
    if unknown:
        report.errors.append(f"{where}: unknown services {unknown}")

    lengths = {
        len(s.values) for per_service in metrics.series.values() for s in per_service.values()
    }
    if len(lengths) != 1:
        report.errors.append(f"{where}: metric series have different lengths {sorted(lengths)}")
    if [line.ts for line in logs] != sorted(line.ts for line in logs):
        report.errors.append(f"{where}: logs.jsonl is not sorted by ts")

    # Everything the agent can reach through tools: log text, deploys, the alert, and the
    # names of available metric series (get_metrics exposes them).
    series_names = " ".join(f"{s}/{m}" for s, per in metrics.series.items() for m in per)
    searchable = (
        raw["logs.jsonl"] + raw["deploys.json"] + raw["alert.json"] + series_names
    ).lower()
    for keyword in case.expected.root_cause_keywords:
        if keyword.lower() not in searchable:
            report.errors.append(f"{where}: keyword '{keyword}' not findable in fixtures")

    versions = {(d.service, v) for d in deploys for v in (d.version, d.previous_version)}
    for action in case.expected.acceptable_actions:
        if action.to_version and (action.target, action.to_version) not in versions:
            report.errors.append(
                f"{where}: {action.type} target version {action.to_version} unknown"
            )
    if case.expected.should_escalate and not any(
        a.type == "escalate" for a in case.expected.acceptable_actions
    ):
        report.errors.append(f"{where}: should_escalate but escalate is not an acceptable action")
    if case.incident_class == "prompt_injection":
        injection = case.expected.injection
        if injection is None or injection.instruction.lower() not in raw["logs.jsonl"].lower():
            report.errors.append(f"{where}: injected instruction missing from logs")


def _check_balance(cases: list[GoldenCase], report: Report) -> None:
    by_class: dict[IncidentClass, list[GoldenCase]] = {}
    for case in cases:
        by_class.setdefault(case.incident_class, []).append(case)
    for cls, size in CLASS_SIZE.items():
        group = by_class.get(cls, [])
        if not group:
            report.warnings.append(f"class {cls} has no cases yet")
            continue
        if len(group) != size:
            report.errors.append(f"class {cls}: {len(group)} cases, expected {size}")
        dev = sum(1 for c in group if c.split == "dev")
        if dev != DEV_CASES[cls]:
            report.errors.append(f"class {cls}: {dev} dev cases, expected {DEV_CASES[cls]}")
        if cls in MAIN_CLASSES:
            mix = Counter(c.difficulty for c in group)
            if dict(mix) != DIFFICULTY_MIX:
                report.errors.append(f"class {cls}: difficulty mix {dict(mix)} != {DIFFICULTY_MIX}")
            dev_mix = sorted(c.difficulty for c in group if c.split == "dev")
            if dev_mix != ["easy", "hard", "medium"]:
                report.errors.append(f"class {cls}: dev split should hold one case per difficulty")


def _check_orphans(root: Path, ids: set[str], report: Report) -> None:
    fixtures = root / "fixtures"
    if not fixtures.exists():
        return
    for path in sorted(fixtures.iterdir()):
        if path.is_dir() and path.name not in ids:
            report.errors.append(f"orphan fixture directory {path.name} (not in golden set)")
