"""dispatch 생성기: 결정성, 규모, 결함 패턴이 통계적으로 드러나는지."""

import pytest

from domains.dispatch.generator import generate
from domains.dispatch.models import hhmm_to_min

SEED = 42


@pytest.fixture(scope="module")
def base():
    return generate(SEED, [])


def test_same_seed_same_data(base):
    again, truth = generate(SEED, [])
    assert again == base[0]
    assert truth == base[1]


def test_different_seed_different_data(base):
    other, _ = generate(SEED + 1, [])
    assert other.orders != base[0].orders


def test_scale(base):
    inst, _ = base
    assert len(inst.workers) == 30
    assert {w.branch for w in inst.workers} == {"A", "B", "C"}
    assert all(sum(w.branch == b for w in inst.workers) == 10 for b in "ABC")
    assert len(inst.orders) == 185 * 10
    assert len({o.id for o in inst.orders}) == len(inst.orders)
    assert all(w.skills for w in inst.workers)


def test_base_orders_inside_own_branch(base):
    inst, _ = base
    assert all(inst.distance_outside(o.branch, o.x, o.y) == 0 for o in inst.orders)


def test_fault_leaves_other_data_untouched(base):
    inst, _ = generate(SEED, ["P3"])
    assert inst.orders == base[0].orders


def test_unknown_fault_rejected():
    with pytest.raises(ValueError):
        generate(SEED, ["P9"])


def _p1_share(inst):
    b_orders = [o for o in inst.orders if o.branch == "B"]
    hit = [o for o in b_orders
           if o.building_type == "apartment" and o.media == "FTTx" and 9 <= o.desired // 60 < 11]
    return len(hit) / len(b_orders)


def test_p1_concentrates_demand(base):
    inst, truth = generate(SEED, ["P1"])
    assert _p1_share(base[0]) < 0.1
    assert _p1_share(inst) > 0.3
    assert truth["faults"]["P1"]["affected_items"]


def test_p2_concentrates_pole_certs(base):
    inst, truth = generate(SEED, ["P2"])
    holders = [w for w in inst.workers if "pole" in w.certs]
    assert len(holders) == sum("pole" in w.certs for w in base[0].workers)
    assert sum(w.branch == "A" for w in holders) / len(holders) >= 0.8
    assert not any(w.branch == "C" for w in holders)
    assert truth["faults"]["P2"]["affected_workers"] == sorted(w.id for w in holders)


def test_p3_shifts_availability():
    inst, truth = generate(SEED, ["P3"])
    shifted = [w for w in inst.workers if w.available[0] == hhmm_to_min("13:00")]
    assert len(shifted) == 6
    assert truth["faults"]["P3"]["affected_workers"] == sorted(w.id for w in shifted)


def test_p4_moves_orders_outside_jurisdiction():
    inst, truth = generate(SEED, ["P4"])
    outside = [o for o in inst.orders if inst.distance_outside(o.branch, o.x, o.y) > 0]
    assert len(outside) / len(inst.orders) > 0.1
    assert {o.id for o in outside} <= set(truth["faults"]["P4"]["affected_items"])
    assert all(inst.area_zone(o) == "boundary" for o in outside)


# --- 일자별 시간역량 (레가시 #2, #8, #9) -------------------------------------------

def _inside(windows, base):
    return all(base[0] <= s < e <= base[1] for s, e in windows)


def test_desired_times_are_hourly(base):
    inst, _ = base
    assert all(o.desired % 60 == 0 for o in inst.orders)


def test_base_capacity_mixes_blocks_and_reductions(base):
    inst, _ = base
    varied = [w for w in inst.workers if w.capacity]
    assert 2 <= len(varied) <= 12                                    # 일부 작업자만
    for w in varied:
        for day, windows in w.capacity.items():
            assert 1 <= day <= inst.days
            assert windows != [w.available]                          # 기본 근무시간과 다른 날만 적는다
            assert _inside(windows, w.available)                     # 늘어나는 쪽은 연동하지 않는다
            assert all(s % 30 == 0 and e % 30 == 0 for s, e in windows)
    days = [ws for w in varied for ws in w.capacity.values()]
    assert any(len(ws) == 2 for ws in days)                          # #8 반복 제한: 하루 중간이 N
    assert any(len(ws) == 1 for ws in days)                          # #9 근무시간 축소: 앞이나 뒤가 N


def test_hours_reduction_applies_from_change_day_on(base):
    inst, _ = base
    reduced = [w for w in inst.workers
               if any(len(ws) == 1 and ws[0] != w.available for ws in w.capacity.values())]
    assert reduced
    for w in reduced:
        first = min(d for d, ws in w.capacity.items() if len(ws) == 1)
        later = [w.windows(d) for d in range(first, inst.days + 1)]
        # 변경일 이후에는 매일 줄어든 시간 (반복 제한이 겹친 날은 더 줄어들 수 있다)
        assert all(sum(e - s for s, e in ws) < w.available[1] - w.available[0] for ws in later)


def test_faults_leave_base_capacity_untouched(base):
    inst, _ = generate(SEED, ["P1", "P2", "P4"])
    assert {w.id: w.capacity for w in inst.workers} == {w.id: w.capacity for w in base[0].workers}


def test_p5_hides_nine_oclock_capacity(base):
    inst, truth = generate(SEED, ["P5"])
    hidden = set(truth["faults"]["P5"]["affected_workers"])
    assert len(hidden) == 7 and all(inst.worker_index[w].branch == "A" for w in hidden)
    nine, ten = hhmm_to_min("09:00"), hhmm_to_min("10:00")
    for w in inst.workers:
        covers_nine = [any(s <= nine and ten <= e for s, e in w.windows(d)) for d in range(1, inst.days + 1)]
        if w.id in hidden:
            assert w.available == base[0].worker_index[w.id].available    # 근무시간은 09시 출근 그대로
            assert not any(covers_nine)                                      # 시간역량은 매일 09~10시 N
        else:
            assert w.capacity == base[0].worker_index[w.id].capacity


def test_p3_clips_daily_capacity_to_new_hours():
    inst, truth = generate(SEED, ["P3"])
    for wid in truth["faults"]["P3"]["affected_workers"]:
        w = inst.worker_index[wid]
        assert all(_inside(w.windows(d), w.available) for d in range(1, inst.days + 1))


def test_p5_truth_skips_workers_p3_already_moved_to_afternoon():
    inst, truth = generate(SEED, ["P3", "P5"])
    p3 = set(truth["faults"]["P3"]["affected_workers"])
    p5 = set(truth["faults"]["P5"]["affected_workers"])
    alone = set(generate(SEED, ["P5"])[1]["faults"]["P5"]["affected_workers"])
    assert p5 == alone - p3                            # 09시 시간역량이 원래 없던 작업자는 정답이 아니다
    assert alone & p3                                  # seed 42에서는 실제로 겹친다


# --- 개통/AS 역량 (#5), 작업그룹 (#10) ---------------------------------------------

def test_work_types_mostly_both(base):
    inst, _ = base
    kinds = [tuple(w.work_types) for w in inst.workers]
    assert all(k in {("install", "repair"), ("install",), ("repair",)} for k in kinds)
    both = sum(k == ("install", "repair") for k in kinds)
    assert 0.7 * len(kinds) <= both < len(kinds)           # 일부만 한쪽 전담
    assert any(k == ("install",) for k in kinds) and any(k == ("repair",) for k in kinds)


def test_groups_split_each_branch(base):
    inst, _ = base
    for b in "ABC":
        members = [w for w in inst.workers if w.branch == b]
        groups = {}
        for w in members:
            groups.setdefault(w.group, []).append(w.id)
        assert sorted(groups) == [f"{b}1", f"{b}2"]
        assert sorted(len(v) for v in groups.values()) == [5, 5]


def test_faults_leave_work_types_and_groups_untouched(base):
    inst, _ = generate(SEED, ["P1", "P2", "P3", "P4", "P5"])
    attrs = lambda i: {w.id: (w.work_types, w.group) for w in i.workers}  # noqa: E731
    assert attrs(inst) == attrs(base[0])
