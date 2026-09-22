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

from pydantic import BaseModel

REQUIRED_STEPS: frozenset[str] = frozenset({"classify", "investigate", "hypothesize", "judge"})


class ProviderConfig(BaseModel):
    """Where to find credentials for an OpenAI-compatible provider.

    Holds env var *names* (e.g. ``LLM_API_KEY``), never secret values.
    """

    # TODO: fields base_url_env, api_key_env


class StepConfig(BaseModel):
    """Model settings for one agent step."""

    # TODO: fields provider, model, temperature, max_output_tokens


class EmbeddingsConfig(BaseModel):
    """Embedding model used for runbook retrieval."""

    # TODO: fields provider, model, dimensions


class ModelsConfig(BaseModel):
    """Root of config/models.yaml."""

    # TODO: fields providers, steps, embeddings
    # TODO: a model validator for provider references and required steps


class LimitsConfig(BaseModel):
    """Root of config/limits.yaml: per-run stop conditions."""

    # TODO: fields max_steps, max_cost_usd, timeout_s, rub_per_usd


def load_models_config(path: Path) -> ModelsConfig:
    """Parse and validate config/models.yaml."""
    raise NotImplementedError


def load_limits_config(path: Path) -> LimitsConfig:
    """Parse and validate config/limits.yaml."""
    raise NotImplementedError
