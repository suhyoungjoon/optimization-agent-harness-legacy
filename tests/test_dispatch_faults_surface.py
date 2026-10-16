"""심어둔 패턴(P1~P5)이 결과에 실제로 드러나는지 (기획서 14장 리스크 대응).

드러나지 않으면 분석 agent의 탐지율 지표가 무의미해진다. 지시서 대부분은 예약(기할당)으로 잡히고, 예약에 실패한
미예약 건만 규칙 agent가 배정한다. 그래서 결함은 구간별 미예약 비율(예약 실패)과 최종 미처리(예약·배정 모두 실패)로 본다.
"""

import copy
from collections import Counter

import pytest

from domains.dispatch.generator import generate
from domains.dispatch.models import hhmm_to_min
from domains.dispatch.pack import get_pack

SEED = 42


@pytest.fixture(scope="module")
def pack():
    return get_pack()


def run(pack, faults, params=None):
    inst, truth = generate(SEED, faults)
    records = pack.solve(inst, params or pack.params)
    return inst, truth, records


def slice_rates(inst, records, pred):
    """(미예약 비율, 최종 미처리 비율): 구간 지시서 전체 대비."""
    selected = [o for o in inst.orders if pred(o)]
    status = {r.item_id: r.status for r in records}
    unbooked = [o for o in selected if o.id not in inst.fixed]
    unserved = sum(status.get(o.id) != "success" for o in unbooked)
    return len(unbooked) / len(selected), unserved / len(selected)


@pytest.fixture(scope="module")
def base(pack):
    return run(pack, [])


def test_p1_morning_surge_fails_in_b(pack, base):
    b_morning = lambda o: o.branch == "B" and 9 <= o.desired // 60 < 11  # noqa: E731
    inst, _, records = run(pack, ["P1"])
    unbooked, unserved = slice_rates(inst, records, b_morning)
    base_unbooked, base_unserved = slice_rates(base[0], base[2], b_morning)
    assert unbooked >= base_unbooked + 0.15
    assert unserved >= base_unserved


def test_p2_pole_work_fails_in_c(pack, base):
    c_pole = lambda o: o.branch == "C" and o.difficulty == "pole"  # noqa: E731
    inst, _, records = run(pack, ["P2"])
    _, unserved = slice_rates(inst, records, c_pole)
    assert unserved >= max(0.5, slice_rates(base[0], base[2], c_pole)[1] + 0.2)
    failed = [r for r in records if c_pole(inst.order_index[r.item_id]) and r.status != "success"]
    # 기술 필터가 자격 필터보다 먼저라 NO_SKILL이 드물게 섞일 수 있다
    assert sum(r.reason_code == "NO_CERT" for r in failed) >= 0.8 * len(failed)


def test_p3_afternoon_workers_underused(pack):
    inst, truth, records = run(pack, ["P3"])
    shifted = set(truth["faults"]["P3"]["affected_workers"])
    jobs = Counter(b.worker_id for b in inst.fixed.values())        # 예약으로 잡힌 일 + 규칙 agent가 배정한 일
    jobs.update(r.decision["worker_id"] for r in records if r.decision)
    shifted_avg = sum(jobs[w] for w in shifted) / len(shifted)
    others = [jobs[w.id] for w in inst.workers if w.id not in shifted]
    assert shifted_avg < 0.6 * (sum(others) / len(others))


def test_p4_boundary_orders_fail_and_recover_with_one_km(pack):
    inst, truth, records = run(pack, ["P4"])
    affected = set(truth["faults"]["P4"]["affected_items"])
    # 예약은 소속 관할 안에서만 잡으므로 경계 밖 고객은 모두 미예약으로 남고, 배정도 관할 밖이라 실패한다
    assert affected.isdisjoint(inst.fixed)
    out_of_area = [r for r in records if r.item_id in affected and r.reason_code == "OUT_OF_AREA"]
    assert len(out_of_area) >= 0.8 * len(affected)

    # 정답표의 param_hint: 최대 지역 완화를 +1km 늘리면 경계 밖 고객 대부분이 살아나고 배정률도 오른다
    relaxed = copy.deepcopy(pack.params)
    relaxed["matching"]["area_extension_km"][-1] += 1
    _, _, after = run(pack, ["P4"], relaxed)
    affected_rate = lambda recs: sum(r.status == "success" for r in recs if r.item_id in affected) / len(affected)  # noqa: E731
    assert affected_rate(after) >= affected_rate(records) + 0.5
    assert pack.metrics(inst, after)["assignment_rate"] >= pack.metrics(inst, records)["assignment_rate"] + 0.05


def test_p3_visible_through_analysis_tools(pack):
    inst, truth, records = run(pack, ["P3"])
    # 기본 정렬(작업시간 적은 순) 상위 7명 중 5명 이상이 P3 작업자이고, 맨 위는 오후만 가능한 작업자
    stats = next(t for t in pack.analysis_tools(inst, records) if t["name"] == "worker_stats")["handler"]({"limit": 7})
    lowest = {r["worker_id"] for r in stats["rows"]}
    assert len(lowest & set(truth["faults"]["P3"]["affected_workers"])) >= 5
    assert stats["rows"][0]["available"] == "13:00-18:00"
    hours = next(t for t in pack.analysis_tools(inst, records) if t["name"] == "demand_by_hour")["handler"]({})
    assert hours["rows"][0]["hour"] == "09" and hours["rows"][0]["workers_available"] < 30


def test_p5_pushes_branch_a_nine_oclock_into_backlog(pack, base):
    a_nine = lambda o: o.branch == "A" and o.desired // 60 == 9  # noqa: E731
    inst, _, records = run(pack, ["P5"])
    unbooked, unserved = slice_rates(inst, records, a_nine)
    base_unbooked, base_unserved = slice_rates(base[0], base[2], a_nine)
    assert unbooked >= base_unbooked + 0.3            # 09시 시간역량이 N이라 예약이 실패한다
    assert unserved >= base_unserved + 0.1


def test_p5_visible_through_analysis_tools(pack):
    inst, truth, records = run(pack, ["P5"])
    hidden = set(truth["faults"]["P5"]["affected_workers"])
    stats = next(t for t in pack.analysis_tools(inst, records) if t["name"] == "worker_stats")["handler"]({})
    rows = {r["worker_id"]: r for r in stats["rows"]}
    # 근무시간은 09시 출근인데 시간역량은 매일 10시부터: 둘을 나란히 보면 드러난다
    for wid in hidden:
        assert rows[wid]["available"] == "09:00-18:00"
        assert "09:00-" not in rows[wid]["capacity"]


def test_demand_by_hour_counts_capacity_starting_inside_the_hour(pack):
    inst, _ = generate(SEED, [])
    w = inst.workers[0]
    w.capacity = {d: [(hhmm_to_min("10:30"), hhmm_to_min("18:00"))] for d in range(1, inst.days + 1)}
    others = [x for x in inst.workers if x.branch == w.branch and x is not w]
    for x in others:
        x.capacity = {d: [] for d in range(1, inst.days + 1)}
    hours = next(t for t in pack.analysis_tools(inst, []) if t["name"] == "demand_by_hour")["handler"](
        {"branch": w.branch})
    rows = {r["hour"]: r["workers_available"] for r in hours["rows"]}
    assert rows["09"] == 0 and rows["10"] == 1      # 10:30 시작이어도 10~11시 약속은 받을 수 있다
