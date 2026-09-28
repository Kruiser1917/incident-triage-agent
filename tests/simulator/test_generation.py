import os
import subprocess
import sys
from pathlib import Path

from tests.simulator.conftest import REPO_ROOT, dataset_files, generate_into


def test_generation_is_byte_for_byte_deterministic(tmp_path: Path, generated_root: Path) -> None:
    again = generate_into(tmp_path / "again")
    assert dataset_files(again) == dataset_files(generated_root)


def test_committed_dataset_matches_generator(generated_root: Path) -> None:
    expected = dataset_files(generated_root)
    committed = dataset_files(REPO_ROOT)
    stale = sorted(
        p for p in expected.keys() | committed.keys() if expected.get(p) != committed.get(p)
    )
    assert not stale, f"committed dataset is stale, run `make dataset`: {stale[:5]}"


def test_generation_does_not_depend_on_hash_seed(tmp_path: Path) -> None:
    # Iterating a set of strings follows PYTHONHASHSEED; any such leak would change output.
    trees = []
    for seed in ("1", "2"):
        root = tmp_path / f"seed{seed}"
        (root / "fixtures").mkdir(parents=True)
        (root / "fixtures" / "topology.yaml").write_bytes(
            (REPO_ROOT / "fixtures" / "topology.yaml").read_bytes()
        )
        env = {**os.environ, "PYTHONHASHSEED": seed}
        subprocess.run(
            [sys.executable, "-m", "triage.simulator", "--root", str(root), "generate"],
            check=True,
            env=env,
            capture_output=True,
        )
        trees.append(dataset_files(root))
    assert trees[0] == trees[1]
