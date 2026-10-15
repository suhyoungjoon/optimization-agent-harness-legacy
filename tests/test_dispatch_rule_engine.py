"""dispatch 규칙 엔진: 손으로 만든 소규모 케이스와 전체 데이터 위반 0건."""

from pathlib import Path

import pytest
import yaml

from core.interfaces import DecisionRecord
from domains.dispatch import rule_engine
from domains.dispatch.generator import generate
from domains.dispatch.models import Branch, Instance, Order, Worker, hhmm_to_min

PARAMS = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "domains" / "dispatch" / "params.yaml").read_text(encoding="utf-8"))
DAY = (hhmm_to_min("09:00"), hhmm_to_min("18:00"))
# 작업자 표준작업시간 (손으로 계산하기 쉬운 값: 개통 60분, 장애 45분)
STD = {"install": 60, "repair": 45}
# W2의 1일차 시간역량: 12~13시 N (할당제한반복일정)
W2_DAY1 = [(hhmm_to_min("09:00"), hhmm_to_min("12:00")), (hhmm_to_min("13:00"), hhmm_to_min("18:00"))]


def order(oid, desired, x, y, media="FTTx", difficulty="none", work_type="install", building="house"):
    return Order(id=oid, day=1, branch="A", work_type=work_type, media=media, difficulty=difficulty,
                 building_type=building, desired=hhmm_to_min(desired), x=x, y=y)


@pytest.fixture
def small():
    workers = [
        Worker("W1", "A", ["FTTx"], [], 90, 2, 2, DAY, std_min=STD),              # 명장
        Worker("W2", "A", ["FTTx", "HFC"], ["pole"], 60, 8, 8, DAY, capacity={1: W2_DAY1}, std_min=STD),
    ]
    orders = [
        order("O1", "09:00", 2, 3),                                  # W1, 1단계
        order("O2", "09:00", 5, 5, media="HFC", work_type="repair"),  # HFC는 W2뿐
        order("O3", "09:00", 3, 3, difficulty="pole"),               # 승주는 W2뿐, O2 뒤로 밀림
        order("O4", "09:00", 4, 4, media="CATV"),                    # 기술 보유자 없음
        order("O5", "09:00", 4, 4, difficulty="high_risk"),          # 자격 보유자 없음
        order("O6", "09:00", 14, 4),                                 # 관할 밖 4km
        order("O7", "06:00", 4, 4),                                  # 3단계(±2시간대)로도 시간역량 전
        order("O8", "09:00", 6, 6, media="HFC", work_type="repair"),  # W2 일정이 12시 N 전까지 참
        order("O9", "15:00", 8, 8),                                  # W2가 가깝지만 명장 W1 우선
    ]
    square = lambda x0: [(x0, 0), (x0 + 10, 0), (x0 + 10, 10), (x0, 10)]  # noqa: E731
    return Instance(branches={"A": Branch("A", square(0)), "B": Branch("B", square(10))}, boundary_zone_km=1,
                    workers=workers, orders=orders, days=1, geo={})


def by_id(records):
    return {r.item_id: r for r in records}


def test_solve_small_case(small):
    got = by_id(rule_engine.solve(small, PARAMS))
    assert got["O1"].decision == {"worker_id": "W1", "start_time": "09:00", "matching_stage": 1}
    assert got["O2"].decision == {"worker_id": "W2", "start_time": "09:00", "matching_stage": 1}
    # O2(09:00~09:45) 종료 후 4km 이동 8분 → 09:53. 약속 시간대는 09~10시라 희망과 같아 1단계
    assert got["O3"].decision == {"worker_id": "W2", "start_time": "09:53", "matching_stage": 1}
    assert got["O9"].decision == {"worker_id": "W1", "start_time": "15:00", "matching_stage": 1}
    expected_fail = {"O4": "NO_SKILL", "O5": "NO_CERT", "O6": "OUT_OF_AREA",
                     "O7": "NO_TIME_MATCH", "O8": "CAPACITY"}
    for oid, code in expected_fail.items():
        assert got[oid].status == "failed" and got[oid].reason_code == code, oid
        assert got[oid].decision is None
    assert all(r.status == "success" for oid, r in got.items() if oid not in expected_fail)


def test_solve_records_evidence_and_dims(small):
    got = by_id(rule_engine.solve(small, PARAMS))
    assert "1단계" in got["O3"].evidence and "W2" in got["O3"].evidence
    assert got["O6"].dims["area_zone"] == "boundary"
    assert got["O1"].dims == {"branch": "A", "hour": "09", "work_type": "install", "media": "FTTx",
                              "building_type": "house", "difficulty": "none", "area_zone": "core"}
    assert got["O3"].metrics["duration_min"] == 90


def test_solve_small_case_has_no_violations(small):
    assert rule_engine.validate(small, rule_engine.solve(small, PARAMS), PARAMS) == []


def decide(item_id, worker_id, start, status="success"):
    return DecisionRecord(item_id=item_id, decision={"worker_id": worker_id, "start_time": start},
                          status=status, reason_code=None, evidence="", dims={})


@pytest.mark.parametrize("decisions, rule, item", [
    ([decide("O4", "W1", "10:00")], "skill_required", "O4"),
    ([decide("O5", "W1", "10:00")], "cert_required", "O5"),
    ([decide("O1", "W1", "08:30")], "outside_availability", "O1"),
    ([decide("O1", "W1", "17:30")], "outside_availability", "O1"),
    ([decide("O1", "W2", "11:30")], "outside_availability", "O1"),      # 12~13시 N에 걸침
    ([decide("O1", "W2", "12:30")], "outside_availability", "O1"),
    ([decide("O1", "W1", "09:00"), decide("O9", "W1", "09:30")], "schedule_overlap", "O9"),
    # O3(승주 90분) 13:00~14:30 직후 O2 시작: 작업은 안 겹치지만 이동 8분을 넣으면 겹침
    ([decide("O3", "W2", "13:00"), decide("O2", "W2", "14:30")], "schedule_overlap", "O2"),
    ([decide("O1", "WX", "09:00")], "invalid_decision", "O1"),
    ([decide("O1", "W1", "9시")], "invalid_decision", "O1"),
    ([decide("O1", "W1", "09:00"), decide("O1", "W1", "13:00")], "invalid_decision", "O1"),
])
def test_validate_catches_each_violation(small, decisions, rule, item):
    violations = rule_engine.validate(small, decisions, PARAMS)
    assert [(v.rule, v.item_id) for v in violations] == [(rule, item)]


def test_validate_ignores_unplaced_statuses(small):
    decisions = [decide("O4", "W1", "10:00", status="blocked"),
                 DecisionRecord("O5", None, "failed", "NO_CERT", "", {})]
    assert rule_engine.validate(small, decisions, PARAMS) == []


def test_validate_counts_pending_approval(small):
    assert rule_engine.validate(small, [decide("O4", "W1", "10:00", status="pending_approval")], PARAMS)


@pytest.mark.parametrize("faults", [[], ["P1", "P2", "P3", "P4"]])
def test_solve_full_dataset_has_no_violations(faults):
    inst, _ = generate(42, faults)
    records = rule_engine.solve(inst, PARAMS)
    assert len(records) == len(inst.orders)
    assert rule_engine.validate(inst, records, PARAMS) == []


def test_validate_accepts_each_capacity_window(small):
    assert rule_engine.validate(small, [decide("O1", "W2", "10:30")], PARAMS) == []
    assert rule_engine.validate(small, [decide("O1", "W2", "13:00")], PARAMS) == []


def test_solve_skips_capacity_gap(small):
    """12시 희망 지시서는 12~13시 N이라 1단계에 못 들고, 2단계(±1시간대)에서 N 앞 구간 11시에 잡힌다."""
    small.workers[0].skills = ["CATV"]                  # W1은 후보에서 뺀다
    small.orders = [order("P1", "12:00", 8, 8)]
    got = rule_engine.solve(small, PARAMS)[0]
    assert got.decision == {"worker_id": "W2", "start_time": "11:00", "matching_stage": 2}
    small.orders = [order("P2", "12:00", 8, 8, work_type="install", difficulty="pole")]   # 90분: 11시엔 N에 걸림
    got = rule_engine.solve(small, PARAMS)[0]
    assert got.decision == {"worker_id": "W2", "start_time": "13:00", "matching_stage": 2}


def test_actual_stage_uses_appointment_slots(small):
    o, w = small.order_index["O1"], small.worker_index["W1"]      # 희망 09:00
    stage = lambda t: rule_engine.actual_stage(small, o, w, hhmm_to_min(t), PARAMS)  # noqa: E731
    assert [stage(t) for t in ("09:00", "09:59", "10:00", "08:30", "11:59", "12:00")] == [1, 1, 2, 2, 3, None]


def test_whole_day_off_is_no_time_match(small):
    small.workers[0].capacity = {1: []}
    small.workers[1].capacity = {1: []}
    small.orders = [order("P1", "10:00", 2, 3)]
    got = rule_engine.solve(small, PARAMS)[0]
    assert got.status == "failed" and got.reason_code == "NO_TIME_MATCH"


@pytest.mark.parametrize("faults", [["P5"]])
def test_solve_with_p5_has_no_violations(faults):
    inst, _ = generate(42, faults)
    assert rule_engine.validate(inst, rule_engine.solve(inst, PARAMS), PARAMS) == []


def test_validate_requires_work_type(small):
    small.workers[0].work_types = ["install"]                      # W1은 개통만
    violations = rule_engine.validate(small, [decide("O2", "W1", "10:00")], PARAMS)   # O2는 장애(repair)
    rules = [(v.rule, v.item_id) for v in violations]
    assert ("work_type_required", "O2") in rules


def test_solve_reports_no_work_type(small):
    small.workers[0].work_types = ["install"]
    small.workers[1].work_types = ["install"]
    small.orders = [order("P1", "10:00", 2, 3, work_type="repair")]
    got = rule_engine.solve(small, PARAMS)[0]
    assert got.status == "failed" and got.reason_code == "NO_WORK_TYPE"
    assert "장애 역량 0명" in got.evidence


def test_funnel_lists_work_type_step(small):
    got = by_id(rule_engine.solve(small, PARAMS))
    assert "FTTx 기술 2명 → 개통 역량 2명" in got["O1"].evidence


def test_duration_uses_worker_standard_time(small):
    o = small.order_index["O3"]                                   # 개통·승주·주택: 기본 × 1.5
    w1, w2 = small.workers
    w2.std_min = {"install": 40, "repair": 30}
    assert rule_engine.duration_min(o, PARAMS, w2) == 60          # 40 × 1.5
    assert rule_engine.duration_min(o, PARAMS, w1) == 90          # 60 × 1.5
    w1.std_min = {}
    assert rule_engine.duration_min(o, PARAMS, w1) == PARAMS["duration"]["base_min"]["install"] * 1.5
    assert rule_engine.duration_min(o, PARAMS) == PARAMS["duration"]["base_min"]["install"] * 1.5


def test_validate_uses_assigned_workers_duration(small):
    small.worker_index["W2"].std_min = {"install": 100, "repair": 45}     # O1(개통 주택) 100분
    # W2 10:30 시작 → 12:10 종료: 12~13시 N에 걸림. 60분이면 11:30에 끝나 문제없음
    assert [v.rule for v in rule_engine.validate(small, [decide("O1", "W2", "10:30")], PARAMS)] == [
        "outside_availability"]


def test_solve_is_deterministic():
    inst, _ = generate(7, ["P1"])
    assert rule_engine.solve(inst, PARAMS) == rule_engine.solve(inst, PARAMS)
