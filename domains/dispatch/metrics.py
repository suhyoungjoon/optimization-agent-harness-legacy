"""dispatch 도메인 지표. 결정 레코드만으로 계산하므로 규칙 agent와 AI agent에 똑같이 쓴다."""

from collections import defaultdict

from core.interfaces import DecisionRecord

from .models import Instance
from .rule_engine import PLACED_STATUSES, duration_min, parse_start, slot_gap, travel_min


def compute(inst: Instance, decisions: list[DecisionRecord], params: dict) -> dict[str, float]:
    total = len(inst.orders)
    placed = []                                  # (worker, order, start)
    stages: dict[int, int] = defaultdict(int)
    for d in decisions:
        if d.status != "success" or d.decision is None:
            continue
        order = inst.order_index.get(d.item_id)
        worker = inst.worker_index.get(str(d.decision.get("worker_id")))
        start = parse_start(d.decision)
        if order is None or worker is None or start is None:
            continue
        placed.append((worker, order, start))
        stage = d.decision.get("matching_stage")
        if isinstance(stage, int):
            stages[stage] += 1

    n = len(placed)
    # 이동시간: 작업자·날짜별로 시간순 정렬, 첫 작업은 작업자 좌표에서 출발
    routes = defaultdict(list)
    for worker, order, start in placed:
        routes[(worker.id, order.day)].append((start, worker, order))
    travels = []
    busy = 0
    for jobs in routes.values():
        jobs.sort(key=lambda j: j[0])
        px, py = jobs[0][1].x, jobs[0][1].y
        for _start, worker, order in jobs:
            travels.append(travel_min(px, py, order.x, order.y, params))
            busy += duration_min(order, params, worker)
            px, py = order.x, order.y
    # 가용시간: 작업자별 그날 시간역량(Y 구간) 합
    available = sum(e - s for w in inst.workers for day in inst.active_days() for s, e in w.windows(day))
    on_time = sum(slot_gap(o, start, params) == 0 for _, o, start in placed)   # 약속 시간대 == 희망 시간대

    metrics = {
        "assignment_rate": n / total if total else 0.0,
        "avg_travel_min": sum(travels) / n if n else 0.0,
        "desired_time_match_rate": on_time / n if n else 0.0,
        # 희망 시간대에 약속한 건 / 전체 건. 위 비율은 배정된 건 중이라 배정이 늘면 저절로 떨어진다
        "on_time_rate": on_time / total if total else 0.0,
        "worker_utilization": busy / available if available else 0.0,
    }
    for stage in range(1, len(params["matching"]["time_window_min"]) + 1):
        metrics[f"stage_{stage}_share"] = stages[stage] / n if n else 0.0
    return {k: float(v) for k, v in metrics.items()}
