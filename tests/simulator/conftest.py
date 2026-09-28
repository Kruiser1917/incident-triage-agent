import shutil
from pathlib import Path

import pytest

from triage.simulator.generate import GOLDEN_PATH, TOPOLOGY_PATH, generate

REPO_ROOT = Path(__file__).resolve().parents[2]


def generate_into(root: Path) -> Path:
    """Generate the full dataset into an empty directory that only has the topology."""
    (root / TOPOLOGY_PATH).parent.mkdir(parents=True)
    shutil.copy(REPO_ROOT / TOPOLOGY_PATH, root / TOPOLOGY_PATH)
    generate(root)
    return root


def dataset_files(root: Path) -> dict[str, bytes]:
    """All generated files under ``root`` (fixtures and golden set), keyed by relative path."""
    files = {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted((root / "fixtures").rglob("*"))
        if path.is_file() and path.name != TOPOLOGY_PATH.name
    }
    files[str(GOLDEN_PATH)] = (root / GOLDEN_PATH).read_bytes()
    return files


@pytest.fixture(scope="session")
def generated_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return generate_into(tmp_path_factory.mktemp("generated"))
