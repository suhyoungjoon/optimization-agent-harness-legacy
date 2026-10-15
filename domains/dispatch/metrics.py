"""dispatch 도메인 지표. 결정 레코드(와 인스턴스의 기할당)만으로 계산하므로 규칙 agent와 AI agent에 똑같이 쓴다."""

from collections import defaultdict

from core.interfaces import DecisionRecord

from .models import Instance
from .rule_engine import duration_min, fixed_jobs, parse_day, parse_start, slot_gap, travel_min


def compute(inst: Instance, decisions: list[DecisionRecord], params: dict) -> dict[str, float]:
    """미예약 지시서(배정 대상) 기준 지표. 작업자 활용률은 기할당 작업도 포함한 하루 일정 기준."""
    total = len(inst.backlog())
    placed = []                                  # (worker, order, day, start)
    stages: dict[int, int] = defaultdict(int)
    for d in decisions:
        if d.status != "success" or d.decision is None:
            continue
        order = inst.order_index.get(d.item_id)
        worker = inst.worker_index.get(str(d.decision.get("worker_id")))
        start = parse_start(d.decision)
        if order is None or worker is None or start is None or order.id in inst.fixed:
            continue
        day = parse_day(order, d.decision)
        if day is None:
            continue
        placed.append((worker, order, day, start))
        stage = d.decision.get("matching_stage")
        if isinstance(stage, int):
            stages[stage] += 1

    n = len(placed)
    # 이동시간: 작업자·날짜별 일정(기할당 포함)을 시간순으로 따라가며, 배정한 지시서로 가는 이동만 센다.
    # 첫 작업은 작업자 좌표에서 출발
    routes = defaultdict(list)
    for (worker_id, day), jobs in fixed_jobs(inst, params).items():
        routes[(worker_id, day)] += [(j.start, j.order, True) for j in jobs]
    for worker, order, day, start in placed:
        routes[(worker.id, day)].append((start, order, False))
    travels = []
    busy = 0
    for (worker_id, day), jobs in routes.items():
        worker = inst.worker_index[worker_id]
        jobs.sort(key=lambda j: (j[0], j[1].id))
        px, py = worker.x, worker.y
        for _start, order, booked in jobs:
            if not booked:
                travels.append(travel_min(px, py, order.x, order.y, params))
            busy += duration_min(order, params, worker)
            px, py = order.x, order.y
    # 가용시간: 작업자별 그날 시간역량(Y 구간) 합
    available = sum(e - s for w in inst.workers for day in inst.active_days() for s, e in w.windows(day))
    # 희망일·희망 시간대에 약속한 건
    on_time = sum(day == o.day and slot_gap(o, start, params) == 0 for _, o, day, start in placed)
    shifted = sum(day > o.day for _, o, day, _ in placed)

    metrics = {
        "assignment_rate": n / total if total else 0.0,
        "avg_travel_min": sum(travels) / n if n else 0.0,
        "desired_time_match_rate": on_time / n if n else 0.0,
        # 희망일·시간대에 약속한 건 / 전체 미예약 건. 위 비율은 배정된 건 중이라 배정이 늘면 저절로 떨어진다
        "on_time_rate": on_time / total if total else 0.0,
        "day_shift_share": shifted / n if n else 0.0,       # 배정한 건 중 희망일보다 늦게 잡은 비율
        "worker_utilization": busy / available if available else 0.0,
    }
    for stage in range(1, len(params["matching"]["time_window_min"]) + 1):
        metrics[f"stage_{stage}_share"] = stages[stage] / n if n else 0.0
    return {k: float(v) for k, v in metrics.items()}
