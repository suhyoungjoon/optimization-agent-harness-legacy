"""core: 레지스트리, 저장소, 규칙 agent 실행 러너, 코어-도메인 경계."""

import re
from pathlib import Path

import pytest

from core.evaluation.runner import run_rule_agent
from core.registry import list_domains, load_pack, load_params
from core.storage.store import Store, to_jsonable

CORE_DIR = Path(__file__).resolve().parent.parent / "core"
DOMAIN_TERMS = re.compile(r"작업자|지시서|지점|명장|dispatch|worker|branch", re.IGNORECASE)


def test_core_does_not_mention_domain_terms():
    """핵심 원칙 1: 코어는 도메인을 모른다."""
    leaks = [f"{p.relative_to(CORE_DIR)}:{i}: {line.strip()}"
             for p in CORE_DIR.rglob("*.py")
             for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
             if DOMAIN_TERMS.search(line)]
    assert leaks == []


def test_registry():
    assert "dispatch" in list_domains()
    assert load_pack("dispatch").name == "dispatch"
    with pytest.raises(KeyError):
        load_pack("nope")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "harness.db")


def test_dataset_is_idempotent(store):
    a = store.save_dataset("dispatch", 42, ["P2", "P1"], 1500)
    b = store.save_dataset("dispatch", 42, ["P1", "P2"], 1500)
    assert a == b
    assert a["id"] == "dispatch-s42-P1-P2" and a["faults"] == ["P1", "P2"]
    assert store.save_dataset("dispatch", 42, [], 1500)["id"] == "dispatch-s42-clean"
    assert store.get_dataset("missing") is None


def test_rule_run_roundtrip(store):
    pack = load_pack("dispatch")
    params = load_params(pack)
    dataset = store.save_dataset("dispatch", 42, [], 1500)
    run_id = run_rule_agent(store, pack, dataset, params)
    run = store.get_run(run_id)
    assert run["status"] == "done"
    assert run["agent"] == "rule" and run["seed"] == 42 and run["dataset_id"] == dataset["id"]
    assert run["params_version"] == params["version"] and run["params"] == params
    assert run["violations"] == []
    assert 0 < run["metrics"]["assignment_rate"] <= 1
    decisions = store.get_decisions(run_id)
    assert len(decisions) == 1850
    assert set(decisions[0]) >= {"item_id", "decision", "status", "reason_code", "evidence", "dims", "metrics"}


def test_to_jsonable_handles_tuples_and_dataclasses():
    from core.interfaces import Violation
    assert to_jsonable({"a": (1, 2), "v": Violation("x", "r", "m")}) == \
        {"a": [1, 2], "v": {"item_id": "x", "rule": "r", "message": "m"}}
