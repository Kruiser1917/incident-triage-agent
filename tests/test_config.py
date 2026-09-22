from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from triage.config import (
    LimitsConfig,
    ModelsConfig,
    load_limits_config,
    load_models_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_YAML = REPO_ROOT / "config" / "models.yaml"
LIMITS_YAML = REPO_ROOT / "config" / "limits.yaml"


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def models_data() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(MODELS_YAML.read_text())
    return data


@pytest.fixture
def limits_data() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(LIMITS_YAML.read_text())
    return data


# --- models.yaml ---------------------------------------------------------------


def test_repo_models_config_loads() -> None:
    cfg = load_models_config(MODELS_YAML)
    assert isinstance(cfg, ModelsConfig)
    assert {"classify", "investigate", "hypothesize", "judge"} <= set(cfg.steps)
    assert cfg.steps["classify"].provider in cfg.providers
    assert cfg.embeddings.dimensions > 0


def test_unknown_key_is_rejected(tmp_path: Path, models_data: dict[str, Any]) -> None:
    models_data["steps"]["classify"]["temprature"] = 0.5  # typo on purpose
    with pytest.raises(ValidationError):
        load_models_config(_write(tmp_path, models_data))


def test_unknown_provider_reference_is_rejected(
    tmp_path: Path, models_data: dict[str, Any]
) -> None:
    models_data["steps"]["hypothesize"]["provider"] = "does-not-exist"
    with pytest.raises(ValidationError):
        load_models_config(_write(tmp_path, models_data))


def test_embeddings_unknown_provider_is_rejected(
    tmp_path: Path, models_data: dict[str, Any]
) -> None:
    models_data["embeddings"]["provider"] = "does-not-exist"
    with pytest.raises(ValidationError):
        load_models_config(_write(tmp_path, models_data))


def test_missing_required_step_is_rejected(tmp_path: Path, models_data: dict[str, Any]) -> None:
    del models_data["steps"]["judge"]
    with pytest.raises(ValidationError):
        load_models_config(_write(tmp_path, models_data))


@pytest.mark.parametrize("temperature", [-0.1, 2.1])
def test_temperature_out_of_range_is_rejected(
    tmp_path: Path, models_data: dict[str, Any], temperature: float
) -> None:
    models_data["steps"]["classify"]["temperature"] = temperature
    with pytest.raises(ValidationError):
        load_models_config(_write(tmp_path, models_data))


def test_models_config_is_immutable() -> None:
    cfg = load_models_config(MODELS_YAML)
    with pytest.raises(ValidationError):
        cfg.steps["classify"].model = "something-else"  # type: ignore[misc]


def test_missing_models_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_models_config(tmp_path / "nope.yaml")


# --- limits.yaml ---------------------------------------------------------------


def test_repo_limits_config_loads() -> None:
    cfg = load_limits_config(LIMITS_YAML)
    assert isinstance(cfg, LimitsConfig)
    assert cfg.max_steps == 12
    assert cfg.max_cost_usd == pytest.approx(0.15)
    assert cfg.timeout_s == 90


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_steps", 0), ("max_cost_usd", 0), ("timeout_s", -1), ("rub_per_usd", 0)],
)
def test_non_positive_limits_are_rejected(
    tmp_path: Path, limits_data: dict[str, Any], field: str, value: float
) -> None:
    limits_data[field] = value
    with pytest.raises(ValidationError):
        load_limits_config(_write(tmp_path, limits_data))


def test_limits_unknown_key_is_rejected(tmp_path: Path, limits_data: dict[str, Any]) -> None:
    limits_data["max_stpes"] = 5
    with pytest.raises(ValidationError):
        load_limits_config(_write(tmp_path, limits_data))
