"""분석 agent용 도메인 도구: 작업자 단위 통계와 시간대별 수요·가용 인력.

코어의 범용 집계(차원별 실패율)로는 '특정 작업자 활용률이 낮다' 같은 자원 쪽 패턴이 보이지 않는다.
"""

from core.interfaces import DecisionRecord

from collections import Counter

from .models import Instance, Worker, min_to_hhmm
from .rule_engine import duration_min, parse_start

_BRANCH = {"type": "string", "description": "지점 (A, B, C). 생략하면 전체"}
_GROUP = {"type": "string", "description": "작업그룹 (예: A1). 생략하면 전체"}


def _capacity_summary(w: Worker, days: list[int]) -> str:
    """일자별 시간역량을 같은 구간끼리 묶은 요약. 예: "09:00-18:00 7일; 10:00-18:00 3일"."""
    patterns = Counter(",".join(f"{min_to_hhmm(s)}-{min_to_hhmm(e)}" for s, e in w.windows(d)) or "N" for d in days)
    return "; ".join(f"{p} {n}일" for p, n in sorted(patterns.items(), key=lambda kv: (-kv[1], kv[0])))


def build_analysis_tools(inst: Instance, decisions: list[DecisionRecord], params: dict) -> list[dict]:
    placed = [d for d in decisions if d.status == "success" and d.decision]
    day_list = inst.active_days()
    days = len(day_list)

    def worker_stats(args: dict):
        branch, group = args.get("branch"), args.get("group")
        rows = []
        for w in inst.workers:
            if (branch and w.branch != branch) or (group and w.group != group):
                continue
            jobs = [d for d in placed if d.decision.get("worker_id") == w.id]
            booked = [oid for oid, b in inst.fixed.items() if b.worker_id == w.id]
            busy = sum(duration_min(inst.order_index[i], params, w) for i in [d.item_id for d in jobs] + booked)
            available = sum(e - s for d in day_list for s, e in w.windows(d))
            rows.append({
                "worker_id": w.id, "branch": w.branch, "group": w.group, "skills": w.skills,
                "work_types": w.work_types, "certs": w.certs, "cei": w.cei,
                "available": f"{min_to_hhmm(w.available[0])}-{min_to_hhmm(w.available[1])}",
                "capacity": _capacity_summary(w, day_list),
                "jobs": len(jobs), "booked_jobs": len(booked), "busy_min": busy, "available_min": available,
                "utilization": round(busy / available, 3) if available else 0.0,
            })
        sort_by = args.get("sort_by") or "busy_min"
        rows.sort(key=lambda r: (r[sort_by], r["worker_id"]))
        limit = int(args.get("limit") or 30)
        n = len(rows)
        return {"days": days, "workers": n,
                "mean_utilization": round(sum(r["utilization"] for r in rows) / n, 3) if n else 0.0,
                "rows": rows[:limit]}

    slot = params["appointment"]["slot_min"]

    def demand_by_hour(args: dict):
        branch = args.get("branch")
        orders = [o for o in inst.orders if not branch or o.branch == branch]
        booked = [o for o in orders if o.id in inst.fixed]
        orders = [o for o in orders if o.id not in inst.fixed]
        workers = [w for w in inst.workers if not branch or w.branch == branch]
        status = {d.item_id: d.status for d in decisions}
        rows = []
        for hour in sorted({o.desired // 60 for o in orders + booked}):
            in_hour = [o for o in orders if o.desired // 60 == hour]
            start = hour * 60
            rows.append({
                "hour": f"{hour:02d}",
                "booked": sum(o.desired // 60 == hour for o in booked),   # 예약으로 이미 잡힌 지시서
                "orders": len(in_hour),                                    # 예약 안 된(배정 대상) 지시서
                "assigned": sum(status.get(o.id) == "success" for o in in_hour),
                "unassigned": sum(status.get(o.id) not in (None, "success") for o in in_hour),
                # 그 약속 시간대에 시간역량이 조금이라도 있는 작업자 수 (날짜 평균)
                "workers_available": round(sum(any(s < start + slot and start < e for s, e in w.windows(d))
                                               for w in workers for d in day_list) / max(days, 1), 1),
            })
        return {"branch": branch or "전체", "days": days, "rows": rows}

    return [
        {
            "name": "worker_stats",
            "description": "작업자별 작업그룹, 개통/AS 역량(work_types), 기본 근무시간(available), 일자별 시간역량 요약(capacity: 일할 수 있는 구간별 일수), "
                           "배정 건수(jobs), 기할당 건수(booked_jobs), 작업시간(분, 기할당 포함), 시간역량 합계(분), 활용률(작업시간/본인 시간역량 합계). "
                           "sort_by 기준 오름차순(기본: 작업시간이 적은 순).",
            "input_schema": {"type": "object", "properties": {
                "branch": _BRANCH, "group": _GROUP, "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                "sort_by": {"type": "string", "enum": ["busy_min", "jobs", "utilization", "available_min"]}}},
            "handler": worker_stats,
        },
        {
            "name": "demand_by_hour",
            "description": "희망시간(시)별 예약된(기할당) 지시서 수(booked), 예약 안 된 지시서 수(orders)와 그중 배정·미배정 수, 그 약속 시간대에 시간역량이 있는 작업자 수(날짜 평균).",
            "input_schema": {"type": "object", "properties": {"branch": _BRANCH}},
            "handler": demand_by_hour,
        },
    ]
