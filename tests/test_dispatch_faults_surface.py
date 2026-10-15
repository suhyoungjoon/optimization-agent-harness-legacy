"""심어둔 패턴(P1~P4)이 규칙 agent 결과에 실제로 드러나는지 (기획서 14장 리스크 대응).

드러나지 않으면 분석 agent의 탐지율 지표가 무의미해진다.
"""

import copy

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


def fail_rate(records, pred):
    selected = [r for r in records if pred(r)]
    return sum(r.status != "success" for r in selected) / len(selected)


@pytest.fixture(scope="module")
def base(pack):
    return run(pack, [])[2]


def test_p1_morning_surge_fails_in_b(pack, base):
    in_slice = lambda r: r.dims["branch"] == "B" and r.dims["hour"] in ("09", "10")  # noqa: E731
    _, _, records = run(pack, ["P1"])
    assert fail_rate(records, in_slice) >= fail_rate(base, in_slice) + 0.15


def test_p2_pole_work_fails_in_c(pack, base):
    c_pole = lambda r: r.dims["branch"] == "C" and r.dims["difficulty"] == "pole"  # noqa: E731
    _, _, records = run(pack, ["P2"])
    assert fail_rate(records, c_pole) >= max(0.5, fail_rate(base, c_pole) + 0.2)
    failed = [r for r in records if c_pole(r) and r.status != "success"]
    # 기술 필터가 자격 필터보다 먼저라 NO_SKILL이 드물게 섞일 수 있다
    assert sum(r.reason_code == "NO_CERT" for r in failed) >= 0.8 * len(failed)


def test_p3_afternoon_workers_underused(pack):
    _, truth, records = run(pack, ["P3"])
    shifted = set(truth["faults"]["P3"]["affected_workers"])
    jobs = {}
    for r in records:
        if r.decision:
            jobs[r.decision["worker_id"]] = jobs.get(r.decision["worker_id"], 0) + 1
    shifted_avg = sum(jobs.get(w, 0) for w in shifted) / len(shifted)
    others = [v for w, v in jobs.items() if w not in shifted]
    assert shifted_avg < 0.6 * (sum(others) / len(others))


def test_p4_boundary_orders_fail_and_recover_with_one_km(pack):
    _, truth, records = run(pack, ["P4"])
    affected = set(truth["faults"]["P4"]["affected_items"])
    out_of_area = [r for r in records if r.item_id in affected and r.reason_code == "OUT_OF_AREA"]
    assert len(out_of_area) >= 0.8 * len(affected)

    # 정답표의 param_hint: 최대 지역 완화를 +1km 늘리면 성공률이 크게 오른다
    relaxed = copy.deepcopy(pack.params)
    relaxed["matching"]["area_extension_km"][-1] += 1
    _, _, after = run(pack, ["P4"], relaxed)
    rate = lambda recs: sum(r.status == "success" for r in recs) / len(recs)  # noqa: E731
    # 경계 밖 고객 대부분이 살아난다. 전체 배정률도 오르지만, 부하가 높아 살아난 고객이 다른 지시서의 자리를
    # 차지하기도 해서 전체 증가폭은 살아난 비율보다 작다
    affected_rate = lambda recs: sum(r.status == "success" for r in recs if r.item_id in affected) / len(affected)  # noqa: E731
    assert affected_rate(after) >= affected_rate(records) + 0.5
    assert rate(after) >= rate(records) + 0.05


def test_p3_visible_through_analysis_tools(pack):
    inst, truth, records = run(pack, ["P3"])
    # 기본 정렬(작업시간 적은 순) 상위 7명 중 5명 이상이 P3 작업자이고, 맨 위는 오후만 가능한 작업자
    stats = next(t for t in pack.analysis_tools(inst, records) if t["name"] == "worker_stats")["handler"]({"limit": 7})
    lowest = {r["worker_id"] for r in stats["rows"]}
    assert len(lowest & set(truth["faults"]["P3"]["affected_workers"])) >= 5
    assert stats["rows"][0]["available"] == "13:00-18:00"
    hours = next(t for t in pack.analysis_tools(inst, records) if t["name"] == "demand_by_hour")["handler"]({})
    assert hours["rows"][0]["hour"] == "09" and hours["rows"][0]["workers_available"] < 30


def test_p5_pushes_branch_a_nine_oclock_to_later_slots(pack, base):
    _, _, records = run(pack, ["P5"])
    a_nine = lambda r: r.dims["branch"] == "A" and r.dims["hour"] == "09"  # noqa: E731

    def on_time(recs):
        selected = [r for r in recs if a_nine(r)]
        return sum(r.status == "success" and r.decision["matching_stage"] == 1 for r in selected) / len(selected)

    # 미할당보다는 희망 시간대(09시) 약속이 크게 줄고 10시 이후로 밀린다
    assert on_time(records) <= on_time(base) - 0.3


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
