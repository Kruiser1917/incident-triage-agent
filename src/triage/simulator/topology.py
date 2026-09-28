"""Load the hand-written topology that every case shares."""

from __future__ import annotations

from pathlib import Path

import yaml

from triage.simulator.schemas import Topology


def load_topology(path: Path) -> Topology:
    return Topology.model_validate(yaml.safe_load(path.read_text()))
