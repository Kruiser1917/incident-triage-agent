"""Scenario registry: every incident class contributes its variant table and builder."""

from __future__ import annotations

from triage.simulator.scenarios import (
    bad_config,
    bad_deploy,
    cert_expired,
    db_connection_leak,
    dependency_cascade,
    disk_full,
    oom_kill,
    prompt_injection,
    traps,
    upstream_throttling,
)
from triage.simulator.scenarios.base import Scenario, ScenarioResult

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(oom_kill.VARIANTS, oom_kill.build),
    Scenario(db_connection_leak.VARIANTS, db_connection_leak.build),
    Scenario(bad_deploy.VARIANTS, bad_deploy.build),
    Scenario(disk_full.VARIANTS, disk_full.build),
    Scenario(upstream_throttling.VARIANTS, upstream_throttling.build),
    Scenario(cert_expired.VARIANTS, cert_expired.build),
    Scenario(bad_config.VARIANTS, bad_config.build),
    Scenario(dependency_cascade.VARIANTS, dependency_cascade.build),
    Scenario(traps.VARIANTS, traps.build),
    Scenario(prompt_injection.VARIANTS, prompt_injection.build),
)

__all__ = ["SCENARIOS", "Scenario", "ScenarioResult"]
