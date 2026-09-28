"""Scenario registry: every incident class contributes its variant table and builder."""

from __future__ import annotations

from triage.simulator.scenarios import oom_kill
from triage.simulator.scenarios.base import Scenario, ScenarioResult

SCENARIOS: tuple[Scenario, ...] = (Scenario(oom_kill.VARIANTS, oom_kill.build),)

__all__ = ["SCENARIOS", "Scenario", "ScenarioResult"]
