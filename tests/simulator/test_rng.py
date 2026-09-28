import pytest

from triage.simulator.rng import Rng, derive_seed


def test_same_seed_same_sequence() -> None:
    a, b = Rng(7), Rng(7)
    assert [a.random() for _ in range(100)] == [b.random() for _ in range(100)]


def test_pinned_values_guard_against_cpython_changes() -> None:
    # If CPython ever changes Random.random(), every fixture would change; fail loudly here.
    rng = Rng(42)
    assert rng.random() == 0.6394267984578837
    assert rng.randint(1, 6) == 1
    assert rng.token(8, "abcdef") == "bbeefaca"


def test_derived_streams_are_independent_of_consumption() -> None:
    parent = Rng(1)
    first = parent.derive("metrics").random()
    parent.random()  # consuming the parent must not shift derived streams
    assert parent.derive("metrics").random() == first
    assert parent.derive("logs").random() != first


def test_derive_seed_is_stable() -> None:
    assert derive_seed(1, "a") == derive_seed(1, "a") != derive_seed(1, "b")


def test_randint_is_inclusive_and_bounded() -> None:
    rng = Rng(3)
    draws = {rng.randint(1, 3) for _ in range(500)}
    assert draws == {1, 2, 3}


def test_choice_rejects_empty() -> None:
    with pytest.raises(ValueError):
        Rng(0).choice([])
