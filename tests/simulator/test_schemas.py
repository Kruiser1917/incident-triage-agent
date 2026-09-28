import pytest
from pydantic import ValidationError

from triage.simulator.schemas import GoldenCase, Topology

SERVICE = {
    "kind": "service",
    "runtime": "go",
    "owner": "team",
    "description": "x",
    "replicas": 1,
    "metrics": ["cpu"],
}


def test_topology_rejects_unknown_dependency() -> None:
    with pytest.raises(ValidationError):
        Topology.model_validate({"services": {"a": {**SERVICE, "depends_on": ["ghost"]}}})


def test_topology_rejects_self_dependency() -> None:
    with pytest.raises(ValidationError):
        Topology.model_validate({"services": {"a": {**SERVICE, "depends_on": ["a"]}}})


def test_dependents_of() -> None:
    topology = Topology.model_validate(
        {"services": {"a": {**SERVICE, "depends_on": ["b"]}, "b": SERVICE}}
    )
    assert topology.dependents_of("b") == ("a",)


def test_golden_case_uses_class_key_on_the_wire() -> None:
    record = {
        "id": "oom_kill-001",
        "class": "oom_kill",
        "difficulty": "easy",
        "split": "dev",
        "fixture_dir": "fixtures/oom_kill-001",
        "alert": {"name": "A", "service": "s", "severity": "critical", "summary": "x"},
        "expected": {
            "root_cause": "x",
            "root_cause_keywords": ["x"],
            "root_service": "s",
            "should_escalate": False,
            "must_call_tools": ["search_logs"],
            "acceptable_actions": [{"type": "restart", "target": "s"}],
        },
    }
    case = GoldenCase.model_validate(record)
    assert case.incident_class == "oom_kill"
    assert case.model_dump(mode="json")["class"] == "oom_kill"


def test_golden_case_rejects_unknown_action() -> None:
    with pytest.raises(ValidationError):
        GoldenCase.model_validate(
            {
                "id": "x-001",
                "class": "oom_kill",
                "difficulty": "easy",
                "split": "dev",
                "fixture_dir": "fixtures/x-001",
                "alert": {"name": "A", "service": "s", "severity": "critical", "summary": "x"},
                "expected": {
                    "root_cause": "x",
                    "root_cause_keywords": ["x"],
                    "root_service": "s",
                    "should_escalate": False,
                    "must_call_tools": [],
                    "acceptable_actions": [{"type": "delete_database", "target": "s"}],
                },
            }
        )
