"""하네스 러너: 가짜 LLM으로 레벨별 흐름(컨텍스트·도구·재시도·차단·승인 대기·트레이스)을 검증."""

import pytest

from core.harness.levels import get_level
from core.harness.runner import AbortRun, HarnessRunner
from domains.dispatch.pack import get_pack
from tests.fake_llm import FakeLLM, submit, tool_use
from tests.test_dispatch_rule_engine import small  # noqa: F401 (fixture)

# small 인스턴스의 규칙 agent 정답 (test_dispatch_rule_engine 참고)
RULE = {"O1": ("W1", "09:00", 1), "O2": ("W2", "09:00", 1), "O3": ("W2", "09:53", 1), "O9": ("W1", "15:00", 1)}
RULE_FAIL = {"O4": "NO_SKILL", "O5": "NO_CERT", "O6": "OUT_OF_AREA", "O7": "NO_TIME_MATCH", "O8": "CAPACITY"}


def rule_policy(item, n, messages, tools):
    if item in RULE:
        w, t, s = RULE[item]
        return submit(item, w, t, s)
    return submit(item, reason=RULE_FAIL[item])


@pytest.fixture
def pack():
    return get_pack()


def run(pack, inst, level, policy, **kw):
    llm = FakeLLM(policy, **kw)
    out = HarnessRunner(pack, llm, get_level(level)).run(inst, run_id="r1", salt="0")
    return out, llm, {d.item_id: d for d in out.decisions}


def test_items_processed_in_order_and_rule_policy_matches(pack, small):  # noqa: F811
    out, llm, got = run(pack, small, "L0", rule_policy)
    assert [d.item_id for d in out.decisions] == pack.items(small)
    assert got["O3"].decision == {"worker_id": "W2", "start_time": "09:53", "matching_stage": 1}
    assert got["O4"].status == "failed" and got["O4"].reason_code == "NO_SKILL"
    assert pack.validate(small, out.decisions) == []
    assert got["O1"].dims["branch"] == "A"
    assert out.usage.calls == len(small.orders)


def test_l0_prompt_has_instance_no_spec_no_domain_tools(pack, small):  # noqa: F811
    _, llm, _ = run(pack, small, "L0", rule_policy)
    first = llm.calls[0]
    assert "도메인 명세" not in first["system"][0]["text"]
    assert [t["name"] for t in first["tools"]] == ["submit_decision"]
    assert first["messages"][0]["content"][0]["text"].startswith("# 인스턴스 데이터")
    # 뒤 항목일수록 확정된 결정이 프롬프트에 쌓인다
    last = llm.calls[-1]["messages"][0]["content"][1]["text"]
    assert '"item_id": "O1"' in last


def test_l1_injects_spec(pack, small):  # noqa: F811
    _, llm, _ = run(pack, small, "L1", rule_policy)
    assert "도메인 명세" in llm.calls[0]["system"][0]["text"]
    assert "필수 조건" in llm.calls[0]["system"][0]["text"]


def test_l2_uses_tools_instead_of_instance(pack, small):  # noqa: F811
    def policy(item, n, messages, tools):
        if n == 0:
            return tool_use("get_order", {"order_id": item})
        return rule_policy(item, n, messages, tools)

    out, llm, got = run(pack, small, "L2", policy)
    first = llm.calls[0]
    assert "handler" not in first["tools"][0]
    assert {t["name"] for t in first["tools"]} >= {"find_candidates", "check_assignment", "submit_decision"}
    assert not first["messages"][0]["content"][0]["text"].startswith("# 인스턴스 데이터")
    second = next(c for c in llm.calls if c["item"] == "O3" and len(c["messages"]) == 3)
    result = second["messages"][2]["content"][0]
    assert result["type"] == "tool_result" and '"default_duration_min": 45' in result["content"]
    assert got["O3"].metrics["llm_calls"] == 2


def bad_then(fix):
    """첫 제출은 기술 없는 W1에게 O4 배정 (skill_required 위반)."""
    def policy(item, n, messages, tools):
        if item == "O4":
            return submit("O4", "W1", "12:00") if n < fix else submit("O4", reason="NO_SKILL")
        return rule_policy(item, n, messages, tools)
    return policy


def test_l2_accepts_violating_decision(pack, small):  # noqa: F811
    out, _, got = run(pack, small, "L2", bad_then(fix=99))
    assert got["O4"].status == "success"
    assert [v.rule for v in pack.validate(small, out.decisions)] == ["skill_required"]


def test_l3_retries_with_feedback(pack, small):  # noqa: F811
    out, llm, got = run(pack, small, "L3", bad_then(fix=1))
    assert got["O4"].status == "failed" and got["O4"].reason_code == "NO_SKILL"
    assert got["O4"].metrics["retries"] == 1
    retry_call = [c for c in llm.calls if c["item"] == "O4"][1]
    feedback = retry_call["messages"][-1]["content"][-1]
    assert feedback["is_error"] and "skill_required" in feedback["content"]
    assert pack.validate(small, out.decisions) == []
    assert out.traces == []  # L3는 trace=minimal


def test_l3_gives_up_after_max_retries(pack, small):  # noqa: F811
    out, _, got = run(pack, small, "L3", bad_then(fix=99))
    assert got["O4"].metrics["retries"] == 2 and got["O4"].status == "success"
    assert [v.rule for v in pack.validate(small, out.decisions)] == ["skill_required"]


def test_l4_blocks_and_holds_for_approval(pack, small):  # noqa: F811
    def policy(item, n, messages, tools):
        if item == "O1":                              # 희망 09시, 약속 11시 시간대: 3단계 매칭
            return submit("O1", "W1", "11:00", 3)
        return bad_then(fix=99)(item, n, messages, tools)

    out, _, got = run(pack, small, "L4", policy)
    assert got["O4"].status == "blocked" and got["O4"].reason_code == "BLOCKED_BY_GUARDRAIL"
    assert got["O1"].status == "pending_approval" and "3단계" in got["O1"].evidence
    assert got["O3"].status == "success"
    assert pack.validate(small, out.decisions) == []


def test_l5_records_full_trace(pack, small):  # noqa: F811
    def policy(item, n, messages, tools):
        if item == "O4" and n == 0:
            return tool_use("find_candidates", {"order_id": "O4", "stage": 1})
        return bad_then(fix=1)(item, n - (item == "O4"), messages, tools)

    out, _, got = run(pack, small, "L5", policy)
    o4 = [t for t in out.traces if t.item_id == "O4"]
    assert [t.kind for t in o4] == ["llm", "tool_call", "llm", "validate", "retry",
                                    "llm", "validate", "guardrail"]
    assert [t.step for t in o4] == list(range(len(o4)))
    assert got["O4"].reason_code == "NO_SKILL"
    assert {t.kind for t in out.traces} >= {"llm", "guardrail"}


def test_failure_modes(pack, small):  # noqa: F811
    _, _, got = run(pack, small, "L2", lambda item, n, m, t: {"type": "text", "text": "음..."})
    assert got["O1"].reason_code == "LLM_NO_DECISION" and got["O1"].metrics["llm_calls"] == 2

    _, _, got = run(pack, small, "L2", rule_policy, stop_reason="refusal")
    assert got["O1"].reason_code == "LLM_REFUSAL"

    _, _, got = run(pack, small, "L2", lambda item, n, m, t: tool_use("get_order", {"order_id": item}))
    assert got["O1"].reason_code == "LLM_MAX_TURNS"

    _, _, got = run(pack, small, "L2", lambda item, n, m, t:
                    RuntimeError("boom") if item == "O1" else rule_policy(item, n, m, t))
    assert got["O1"].reason_code == "LLM_ERROR" and got["O2"].status == "success"

    with pytest.raises(AbortRun):
        run(pack, small, "L2", lambda item, n, m, t: RuntimeError("down"))


def test_unknown_tool_returns_error(pack, small):  # noqa: F811
    def policy(item, n, messages, tools):
        return tool_use("rm_rf", {}) if n == 0 else rule_policy(item, n, messages, tools)

    out, llm, _ = run(pack, small, "L2", policy)
    err = llm.calls[1]["messages"][2]["content"][0]
    assert err["is_error"] and "사용할 수 없는 도구" in err["content"]


def test_usage_accumulates(pack, small):  # noqa: F811
    out, _, _ = run(pack, small, "L0", rule_policy)
    n = len(small.orders)
    assert out.usage.tokens["input_tokens"] == 100 * n and out.usage.tokens["cache_read_input_tokens"] == 50 * n


def test_overlap_with_earlier_placed_job_is_caught(pack, small):  # noqa: F811
    """새 결정이 이미 배정된 작업보다 앞 시각에 들어가 겹치면, validate()는 위반을 기존(늦은) 작업에
    붙인다. 검증 루프·가드레일은 이 경우도 새 결정의 위반으로 잡아야 한다."""
    def policy(item, n, messages, tools):
        if item == "O2":  # W2 10:00~10:45
            return submit("O2", "W2", "10:00", 3)
        if item == "O3":  # W2 09:00~10:30 (90분) → 뒤에 있는 O2와 겹침. 위반은 O2에 기록된다
            return submit("O3", "W2", "09:00", 1) if n == 0 else submit("O3", reason="CAPACITY")
        return rule_policy(item, n, messages, tools)

    out, llm, got = run(pack, small, "L3", policy)
    assert got["O3"].metrics["retries"] == 1 and got["O3"].status == "failed"
    feedback = [c for c in llm.calls if c["item"] == "O3"][1]["messages"][-1]["content"][-1]["content"]
    assert "schedule_overlap" in feedback
    assert pack.validate(small, out.decisions) == []

    out, _, got = run(pack, small, "L4", lambda item, n, m, t:
                      submit("O3", "W2", "09:00", 1) if item == "O3" else policy(item, n, m, t))
    assert got["O3"].status == "blocked"
    assert pack.validate(small, out.decisions) == []
