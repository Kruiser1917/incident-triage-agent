import json
import shutil
from pathlib import Path

import pytest

from tests.simulator.conftest import REPO_ROOT
from triage.simulator.generate import GOLDEN_PATH
from triage.simulator.validate import validate


@pytest.fixture
def dataset(tmp_path: Path, generated_root: Path) -> Path:
    root = tmp_path / "copy"
    shutil.copytree(generated_root, root)
    return root


def _rewrite_first_record(root: Path, change: dict[str, object]) -> None:
    path = root / GOLDEN_PATH
    lines = path.read_text().splitlines()
    record = json.loads(lines[0])
    record["expected"].update(change)
    lines[0] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n")


def test_committed_dataset_is_valid() -> None:
    report = validate(REPO_ROOT)
    assert report.errors == []


def test_duplicate_id_is_reported(dataset: Path) -> None:
    path = dataset / GOLDEN_PATH
    first = path.read_text().splitlines()[0]
    path.write_text(path.read_text() + first + "\n")
    assert any("duplicate id" in e for e in validate(dataset).errors)


def test_unfindable_keyword_is_reported(dataset: Path) -> None:
    _rewrite_first_record(dataset, {"root_cause_keywords": ["flux capacitor"]})
    assert any("not findable" in e for e in validate(dataset).errors)


def test_unknown_service_is_reported(dataset: Path) -> None:
    _rewrite_first_record(dataset, {"root_service": "billing-service"})
    assert any("unknown services" in e for e in validate(dataset).errors)


def test_missing_fixture_file_is_reported(dataset: Path) -> None:
    (dataset / "fixtures" / "oom_kill-001" / "metrics.json").unlink()
    assert any("missing fixture files" in e for e in validate(dataset).errors)


def test_orphan_fixture_dir_is_reported(dataset: Path) -> None:
    (dataset / "fixtures" / "oom_kill-999").mkdir()
    assert any("orphan" in e for e in validate(dataset).errors)
