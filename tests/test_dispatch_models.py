"""dispatch 모델: 일자별 시간역량과 약속 시간대."""

from domains.dispatch.models import Branch, Instance, Order, Worker, hhmm_to_min, slot_of

DAY = (hhmm_to_min("09:00"), hhmm_to_min("18:00"))


def test_windows_default_to_base_hours():
    w = Worker("W1", "A", ["FTTx"], [], 90, 0, 0, DAY)
    assert w.windows(1) == [DAY]
    assert w.windows(7) == [DAY]


def test_windows_follow_daily_capacity():
    split = [(hhmm_to_min("09:00"), hhmm_to_min("12:00")), (hhmm_to_min("13:30"), hhmm_to_min("18:00"))]
    w = Worker("W1", "A", ["FTTx"], [], 90, 0, 0, DAY, capacity={2: split, 3: []})
    assert w.windows(1) == [DAY]
    assert w.windows(2) == split
    assert w.windows(3) == []                      # 하루 전체 N


def test_slot_of_floors_to_appointment_slot():
    assert slot_of(hhmm_to_min("09:00"), 60) == hhmm_to_min("09:00")
    assert slot_of(hhmm_to_min("09:53"), 60) == hhmm_to_min("09:00")
    assert slot_of(hhmm_to_min("10:00"), 60) == hhmm_to_min("10:00")


def test_active_days_come_from_orders():
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    orders = [Order(f"O{d}", d, "A", "install", "FTTx", "none", "house", 540, 1, 1) for d in (2, 5, 5)]
    inst = Instance(branches={"A": Branch("A", square)}, boundary_zone_km=1, workers=[], orders=orders,
                    days=2, geo={})
    assert inst.active_days() == [2, 5]


def test_work_types_and_group_defaults():
    w = Worker("W1", "A", ["FTTx"], [], 90, 0, 0, DAY)
    assert w.work_types == ["install", "repair"]          # 대부분 개통·장애를 같이 처리
    assert w.group == ""
