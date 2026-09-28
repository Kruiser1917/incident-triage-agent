"""Human-readable view of one case, for manual realism review.

The answer key stays hidden unless asked for, so a reviewer can first try to diagnose the
case the way the agent will: from the alert, deploys, metrics and logs alone.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from triage.simulator.clock import epoch_s, relative
from triage.simulator.generate import GOLDEN_PATH, TOPOLOGY_PATH
from triage.simulator.schemas import (
    AlertmanagerWebhook,
    Deploy,
    GoldenCase,
    LogLine,
    MetricsFile,
    Topology,
)
from triage.simulator.topology import load_topology

SPARK = "▁▂▃▄▅▆▇█"
WIDTH = 62
_DEPLOYS = TypeAdapter(list[Deploy])
_K8S = "[bcdfghjklmnpqrstvwxz2456789]"  # Kubernetes name-suffix alphabet
_TEMPLATE_RULES = (
    (re.compile(rf"\b[a-z]+-[a-z-]*-{_K8S}{{10}}-{_K8S}{{5}}\b"), "<pod>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"\b[a-z]{2,4}_[0-9a-z]{8,}\b"), "<id>"),
    (re.compile(r"\b[0-9a-f]{12,}\b"), "<hex>"),
    (re.compile(r"\d+(?:\.\d+)?"), "#"),
)  # fmt: skip


@dataclass(frozen=True)
class Case:
    golden: GoldenCase
    alert: AlertmanagerWebhook
    logs: tuple[LogLine, ...]
    metrics: MetricsFile
    deploys: tuple[Deploy, ...]
    topology: Topology


def load_case(root: Path, case_id: str) -> Case:
    golden = None
    for line in (root / GOLDEN_PATH).read_text().splitlines():
        record = GoldenCase.model_validate_json(line)
        if record.id == case_id:
            golden = record
    if golden is None:
        raise SystemExit(f"unknown case: {case_id}")
    case_dir = root / golden.fixture_dir
    return Case(
        golden=golden,
        alert=AlertmanagerWebhook.model_validate_json((case_dir / "alert.json").read_text()),
        logs=tuple(
            LogLine.model_validate_json(line)
            for line in (case_dir / "logs.jsonl").read_text().splitlines()
        ),
        metrics=MetricsFile.model_validate_json((case_dir / "metrics.json").read_text()),
        deploys=tuple(_DEPLOYS.validate_json((case_dir / "deploys.json").read_text())),
        topology=load_topology(root / TOPOLOGY_PATH),
    )


def render(case: Case, *, answer: bool) -> str:
    alert = case.alert.alerts[0]
    alert_ts = epoch_s(alert.startsAt)
    out = [f"═══ {case.golden.id} " + "═" * 60, ""]
    out += _alert_section(case)
    out += ["", "DEPLOYS (last 24h; older ones are in deploys.json)"]
    out += _deploy_section(case, alert_ts)
    out += ["", f"METRICS (T-3h .. T+5m; ▲ marks the alert; {WIDTH} columns, max per column)"]
    out += _metrics_section(case, alert_ts)
    out += ["", "LOGS"]
    out += _logs_section(case, alert_ts)
    out.append("")
    if answer:
        out += _answer_section(case)
    else:
        out.append("ANSWER hidden — rerun with ANSWER=1 (make) or --answer (cli) to reveal it.")
    return "\n".join(out)


def _alert_section(case: Case) -> list[str]:
    alert = case.alert.alerts[0]
    labels = alert.labels
    return [
        f"ALERT  {labels['alertname']}  service={labels['service']}  "
        f"severity={labels['severity']}  team={labels.get('team', '-')}",
        f"       {alert.annotations['summary']} — {alert.annotations['description']}",
        f"       firing since {alert.startsAt:%Y-%m-%d %H:%M:%S} UTC",
    ]


def _deploy_section(case: Case, alert_ts: int) -> list[str]:
    rows = []
    for d in case.deploys:
        ts = epoch_s(d.ts)
        if alert_ts - 24 * 3600 <= ts <= alert_ts + 3600:
            rows.append(
                f"  {relative(ts, alert_ts):>9}  {d.service:<17} {d.previous_version:>9} → "
                f"{d.version:<9} {d.change_type:<6} {d.description}  ({d.author})"
            )
    return rows or ["  (none)"]


def _metrics_section(case: Case, alert_ts: int) -> list[str]:
    start = epoch_s(case.metrics.start)
    step = case.metrics.step_s
    alert_service = case.alert.alerts[0].labels["service"]
    scored = []
    for service, metrics in case.metrics.series.items():
        for metric, series in metrics.items():
            scored.append((_anomaly(series.values), service, metric))
    shown = [(s, m) for _, s, m in scored if s == alert_service]
    others = sorted((x for x in scored if x[1] != alert_service), key=lambda x: -x[0])
    shown += [(s, m) for _, s, m in others[:6]]
    rows = []
    n = len(next(iter(next(iter(case.metrics.series.values())).values())).values)
    column_of_alert = min(WIDTH - 1, (alert_ts - start) // step * WIDTH // n)
    for service, metric in shown:
        series = case.metrics.series[service][metric]
        present = [v for v in series.values if v is not None]
        rows.append(
            f"  {service:<20} {metric:<14} {_spark(series.values)}  "
            f"min {min(present):g} max {max(present):g} last {present[-1]:g} {series.unit}"
        )
    rows.append(" " * 38 + " " * column_of_alert + "▲")
    missing = _missing_series(case)
    if missing:
        rows.append(f"  missing series: {', '.join(missing)}")
    return rows


def _missing_series(case: Case) -> list[str]:
    """Series the topology promises but metrics.json lacks (broken scrapes)."""
    missing = []
    for service, spec in case.topology.services.items():
        present = case.metrics.series.get(service, {})
        missing += [f"{service}/{metric}" for metric in spec.metrics if metric not in present]
    return missing


def _anomaly(values: tuple[float | None, ...]) -> float:
    present = [v for v in values if v is not None]
    if len(present) < 10:
        return 0.0
    head = present[: max(10, len(present) * 2 // 5)]
    median = statistics.median(head)
    ordered = sorted(head)
    spread = ordered[len(ordered) * 9 // 10] - ordered[len(ordered) // 10]
    scale = max(spread, abs(median) * 0.05, 1e-9)
    return max(abs(v - median) for v in present) / scale


def _spark(values: tuple[float | None, ...]) -> str:
    present = [v for v in values if v is not None]
    low, high = min(present), max(present)
    span = (high - low) or 1.0
    chars = []
    for col in range(WIDTH):
        bucket = values[col * len(values) // WIDTH : (col + 1) * len(values) // WIDTH]
        points = [v for v in bucket if v is not None]
        if not points:
            chars.append(" ")
            continue
        chars.append(SPARK[min(len(SPARK) - 1, int((max(points) - low) / span * len(SPARK)))])
    return "".join(chars)


def _template(msg: str) -> str:
    text = msg.splitlines()[0]
    for pattern, replacement in _TEMPLATE_RULES:
        text = pattern.sub(replacement, text)
    return text[:150]


def _logs_section(case: Case, alert_ts: int) -> list[str]:
    levels = Counter(line.level for line in case.logs)
    rows = [
        f"  {len(case.logs)} lines, T-60m .. T+5m: "
        + ", ".join(
            f"{level} {levels[level]}" for level in ("ERROR", "WARN", "INFO") if levels[level]
        )
    ]
    patterns: dict[tuple[str, str, str], list[int]] = {}
    for line in case.logs:
        if line.level in ("WARN", "ERROR", "FATAL"):
            key = (line.service, line.level, _template(line.msg))
            patterns.setdefault(key, []).append(epoch_s(line.ts))
    rows.append("  top WARN/ERROR patterns (count, first seen, last seen):")
    top = sorted(patterns.items(), key=lambda item: -len(item[1]))[:12]
    for (service, level, template), times in top:
        rows.append(
            f"    {len(times):>3}x  {relative(min(times), alert_ts):>7} .. "
            f"{relative(max(times), alert_ts):<7} {level:<5} [{service}] {template}"
        )
    rows.append("  raw lines around the alert (T-2m .. T+1m, WARN and above):")
    near = [
        line
        for line in case.logs
        if alert_ts - 120 <= epoch_s(line.ts) <= alert_ts + 60 and line.level != "INFO"
    ]
    for line in near[:15]:
        text = line.msg.splitlines()[0][:140]
        rows.append(f"    {line.ts:%H:%M:%S} {line.level:<5} [{line.service}/{line.source}] {text}")
    return rows


def _answer_section(case: Case) -> list[str]:
    g = case.golden
    e = g.expected
    rows = [
        f"ANSWER  class={g.incident_class}  difficulty={g.difficulty}  split={g.split}",
        f"  root cause:   {e.root_cause}",
        f"  root service: {e.root_service}   escalate: {e.should_escalate}",
        f"  keywords:     {', '.join(e.root_cause_keywords)}",
        "  acceptable:   "
        + "; ".join(
            f"{a.type} {a.target}" + (f" -> {a.to_version}" if a.to_version else "")
            for a in e.acceptable_actions
        ),
        f"  must call:    {', '.join(e.must_call_tools)}",
    ]
    rows += [f"  red herring:  {note}" for note in e.red_herrings]
    rows.append(f"  tags:         {', '.join(g.tags) or '-'}")
    return rows
