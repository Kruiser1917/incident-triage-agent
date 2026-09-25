"""Typed loading of config/models.yaml and config/limits.yaml.

Configs are parsed once at startup into immutable Pydantic models, so a typo in
YAML fails fast with a clear error instead of surfacing mid-run as a KeyError.

Contract (enforced by tests/test_config.py):
- Unknown keys anywhere are rejected (``extra="forbid"``).
- All models are immutable (``frozen=True``).
- ``StepConfig.provider`` must name a provider defined under ``providers``.
- The steps ``classify``, ``investigate``, ``hypothesize`` and ``judge`` are required.
- ``temperature`` is within [0, 2]; ``max_output_tokens``, ``max_steps``,
  ``max_cost_usd``, ``timeout_s``, ``rub_per_usd`` and ``dimensions`` are > 0.
- A missing file raises ``FileNotFoundError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, model_validator

REQUIRED_STEPS: frozenset[str] = frozenset({"classify", "investigate", "hypothesize", "judge"})


class _StrictModel(BaseModel):
    """Base for all config models: unknown keys are errors, loaded configs are read-only."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderConfig(_StrictModel):
    """Where to find credentials for an OpenAI-compatible provider.

    Holds env var *names* (e.g. ``LLM_API_KEY``), never secret values.
    """

    base_url_env: str
    api_key_env: str


class StepConfig(_StrictModel):
    """Model settings for one agent step."""

    provider: str
    model: str
    temperature: float = Field(ge=0, le=2)
    max_output_tokens: PositiveInt


class EmbeddingsConfig(_StrictModel):
    """Embedding model used for runbook retrieval."""

    provider: str
    model: str
    dimensions: PositiveInt


class ModelsConfig(_StrictModel):
    """Root of config/models.yaml."""

    providers: dict[str, ProviderConfig]
    steps: dict[str, StepConfig]
    embeddings: EmbeddingsConfig

    @model_validator(mode="after")
    def _check_cross_references(self) -> Self:
        # Field types are already valid here; these checks need several fields at once.
        missing = REQUIRED_STEPS - self.steps.keys()
        if missing:
            raise ValueError(f"missing required steps: {sorted(missing)}")

        referenced = {step.provider for step in self.steps.values()} | {self.embeddings.provider}
        unknown = referenced - self.providers.keys()
        if unknown:
            raise ValueError(f"unknown providers referenced: {sorted(unknown)}")
        return self


class LimitsConfig(_StrictModel):
    """Root of config/limits.yaml: per-run stop conditions."""

    max_steps: PositiveInt
    max_cost_usd: PositiveFloat
    timeout_s: PositiveInt
    rub_per_usd: PositiveFloat


def load_models_config(path: Path) -> ModelsConfig:
    """Parse and validate config/models.yaml."""
    data = yaml.safe_load(path.read_text())
    return ModelsConfig.model_validate(data)


def load_limits_config(path: Path) -> LimitsConfig:
    """Parse and validate config/limits.yaml."""
    data = yaml.safe_load(path.read_text())
    return LimitsConfig.model_validate(data)
