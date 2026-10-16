"""평가 러너와 비교표: 규칙·AI run 저장, 범위 지정, 트레이스, 비용, 일관성."""

import pytest

from core.evaluation.compare import compare, consistency
from core.evaluation.runner import run_ai_agent, run_rule_agent
from core.llm.client import load_config
from core.registry import load_pack, load_params
from core.storage.store import Store
from tests.fake_llm import FakeLLM, submit


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "h.db")
    pack = load_pack("dispatch")
    dataset = store.save_dataset("dispatch", 42, [], 1500)
    instance, _ = pack.generate(42, [])
    scope = pack.items(instance)[:8]
    return store, pack, dataset, scope


def rule_like(pack, dataset, scope):
    """규칙 agent의 답을 그대로 제출하는 가짜 LLM (배관 검증용)."""
    instance, _ = pack.generate(dataset["seed"], dataset["faults"])
    answers = {d.item_id: d for d in pack.solve(pack.subset(instance, scope), pack.params)}

    def policy(item, n, messages, tools):
        d = answers[item]
        if d.decision:
            return submit(item, d.decision["worker_id"], d.decision["start_time"], d.decision["matching_stage"],
                          day=d.decision.get("day"))
        return submit(item, reason=d.reason_code)
    return FakeLLM(policy)


def test_rule_and_ai_runs_on_same_scope(env):
    store, pack, dataset, scope = env
    rule_id = run_rule_agent(store, pack, dataset, load_params(pack), scope=scope, group_id="g1")
    progress = []
    ai_id = run_ai_agent(store, pack, dataset, "L5", rule_like(pack, dataset, scope), load_config(),
                         scope=scope, group_id="g1", progress=lambda d, t: progress.append((d, t)))
    rule, ai = store.get_run(rule_id), store.get_run(ai_id)
    assert rule["scope"] == scope and ai["scope"] == scope
    assert ai["agent"] == "ai" and ai["level"] == "L5" and ai["model"] == "fake-model"
    # 같은 답을 냈다. L5는 가드레일이 있어 3단계 매칭·일반 작업자 배정은 승인 대기로 남는다
    placed = lambda run_id: {d["item_id"]: d["decision"] for d in store.get_decisions(run_id)  # noqa: E731
                             if d["status"] in ("success", "pending_approval")}
    assert placed(ai_id) == placed(rule_id)
    assert ai["metrics"]["assignment_rate"] <= rule["metrics"]["assignment_rate"]
    assert ai["violations"] == [] and len(store.get_decisions(ai_id)) == 8
    assert ai["meta"]["usage"]["calls"] == 8 and ai["meta"]["usage"]["cost_usd"] is None  # fake-model은 단가 없음
    assert progress[-1] == (8, 8)
    assert store.get_traces(ai_id, scope[0])[0]["kind"] == "llm"
    assert [r["run_id"] for r in store.list_runs(group_id="g1")] == [rule_id, ai_id]


def test_ai_run_failure_is_recorded(env):
    store, pack, dataset, scope = env
    with pytest.raises(Exception):
        run_ai_agent(store, pack, dataset, "L2", FakeLLM(lambda *a: RuntimeError("down")), load_config(),
                     scope=scope)
    failed = [r for r in store.list_runs() if r["status"] == "error"]
    assert failed and "down" in failed[0]["meta"]["error"]


def test_compare_and_consistency(env):
    store, pack, dataset, scope = env
    rule_id = run_rule_agent(store, pack, dataset, load_params(pack), scope=scope)
    same = [run_ai_agent(store, pack, dataset, "L3", rule_like(pack, dataset, scope), load_config(),
                         scope=scope, repeat=r) for r in range(2)]
    # 세 번째 반복은 첫 항목만 다르게 (미배정)
    base = rule_like(pack, dataset, scope)
    differ = FakeLLM(lambda item, n, m, t: submit(item, reason="CAPACITY") if item == scope[0]
                     else base.policy(item, n, m, t))
    third = run_ai_agent(store, pack, dataset, "L3", differ, load_config(), scope=scope, repeat=2)

    assert consistency(store, same) == 1.0
    assert consistency(store, same + [third]) == pytest.approx(7 / 8)
    table = compare(store, [rule_id, *same, third])
    assert [s["agent"] for s in table["summary"]] == ["rule", "ai"]
    rule_row, ai_row = table["summary"]
    assert rule_row["cost_per_item_usd"] == 0 and rule_row["consistency"] is None
    assert ai_row["runs"] == 3 and ai_row["consistency"] == pytest.approx(7 / 8)
    assert len(table["runs"]) == 4 and table["runs"][0]["seconds_per_item"] is not None
