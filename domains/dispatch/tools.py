"""AI agent 도구: 선언(TOOL_SPECS)과 실행(build_tools).

handler는 (args, ctx: ToolContext)를 받아 JSON으로 바꿀 수 있는 값을 돌려준다.
오류는 {"error": ...}로 돌려주고, 코어가 tool_result의 is_error로 전달한다.
"""

from collections import Counter

from core.interfaces import DecisionRecord, ToolContext

from . import rule_engine
from .models import Instance, min_to_hhmm

_ORDER_ID = {"type": "string", "description": "지시서 ID (예: D01-O015)"}
_WORKER_ID = {"type": "string", "description": "작업자 ID (예: WA03)"}
_DAY = {"type": "integer", "minimum": 1, "description": "날짜(일). 생략하면 지시서 희망일"}

TOOL_SPECS = [
    {
        "name": "get_order",
        "description": "지시서의 소속 지점, 희망일(day)·희망시간, 긴급 여부, 늦출 수 있는 배정일 범위, 작업유형, 매체, 난이도, 건물유형, "
                       "좌표, 기본 작업소요(분), 필요한 자격을 돌려준다. "
                       "실제 작업소요는 작업자 표준작업시간에 따라 다르며 find_candidates가 후보별로 돌려준다.",
        "input_schema": {"type": "object", "properties": {"order_id": _ORDER_ID}, "required": ["order_id"]},
    },
    {
        "name": "find_candidates",
        "description": "지시서의 매칭 단계(1~3)에서 필수 조건(기술·개통/AS 역량·자격)과 시간·지역 범위를 만족하고 그날(day) 시간역량과 "
                       "일정(기할당 포함)에 들어갈 수 있는 후보 작업자와 각자의 적합 시작 시각, 이동시간, 그 작업자 기준 작업소요(분)를 "
                       "돌려준다. 희망일 1단계부터 조회하고, 희망일 3단계까지 후보가 없을 때만 다음 날을 조회한다.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": _ORDER_ID, "stage": {"type": "integer", "minimum": 1, "maximum": 3},
                           "day": _DAY},
            "required": ["order_id", "stage"],
        },
    },
    {
        "name": "get_travel_time",
        "description": "작업자가 지시서 희망시간 직전에 있던 위치(직전 작업지, 없으면 작업자 좌표)에서 지시서까지의 이동시간(분)을 돌려준다.",
        "input_schema": {
            "type": "object",
            "properties": {"worker_id": _WORKER_ID, "order_id": _ORDER_ID},
            "required": ["worker_id", "order_id"],
        },
    },
    {
        "name": "get_worker_schedule",
        "description": "작업자의 작업그룹, 기술, 개통/AS 역량(work_types), 표준작업시간(std_min: 작업유형별 기본 소요), 자격, CEI, 기본 근무시간(available), 지금 처리 중인 지시서 날짜의 시간역량"
                       "(capacity: 일할 수 있는 구간 목록, 작업은 한 구간 안에서 시작·종료)과 그날 일정(fixed=true는 예약으로 잡힌 기할당)을 "
                       "돌려준다. day를 주면 그 날짜 기준.",
        "input_schema": {"type": "object", "properties": {"worker_id": _WORKER_ID, "day": _DAY},
                         "required": ["worker_id"]},
    },
    {
        "name": "check_assignment",
        "description": "지시서를 작업자에게 해당 시각에 배정할 때 필수 조건 위반이 있는지 확인한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": _ORDER_ID,
                "worker_id": _WORKER_ID,
                "start_time": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$"},
                "day": _DAY,
            },
            "required": ["order_id", "worker_id", "start_time"],
        },
    },
]


def build_tools(inst: Instance, params: dict) -> list[dict]:
    """TOOL_SPECS에 handler를 붙여 돌려준다."""

    def order_or_error(order_id):
        order = inst.order_index.get(str(order_id))
        if order is None:
            raise ToolError(f"알 수 없는 지시서: {order_id}")
        return order

    def worker_or_error(worker_id):
        worker = inst.worker_index.get(str(worker_id))
        if worker is None:
            raise ToolError(f"알 수 없는 작업자: {worker_id}")
        return worker

    def day_or_error(o, value):
        if value is None:
            return o.day
        allowed = rule_engine.candidate_days(inst, o, params)
        try:
            day = int(value)
        except (TypeError, ValueError):
            raise ToolError(f"day는 정수: {value}") from None
        if day not in allowed:
            raise ToolError(f"{o.id}의 배정일은 {allowed[0]}~{allowed[-1]}일")
        return day

    def get_order(args: dict, ctx: ToolContext):
        o = order_or_error(args.get("order_id"))
        return {
            "order_id": o.id, "day": o.day, "branch": o.branch, "work_type": o.work_type,
            "media": o.media, "difficulty": o.difficulty, "building_type": o.building_type,
            "desired_time": min_to_hhmm(o.desired), "x": o.x, "y": o.y,
            # 작업소요는 작업자 표준작업시간에 따라 다르다. 여기서는 기본 소요 기준, 후보별 값은 find_candidates
            "default_duration_min": rule_engine.duration_min(o, params),
            "urgent": o.urgent,
            "allowed_days": rule_engine.candidate_days(inst, o, params),
            "required_cert": rule_engine.REQUIRED_CERT.get(o.difficulty),
            "outside_jurisdiction_km": round(inst.distance_outside(o.branch, o.x, o.y), 2),
        }

    def find_candidates(args: dict, ctx: ToolContext):
        o = order_or_error(args.get("order_id"))
        stage = int(args.get("stage", 1))
        if not 1 <= stage <= len(params["matching"]["time_window_min"]):
            raise ToolError(f"stage는 1~{len(params['matching']['time_window_min'])}")
        pool, skilled, capable, certified = rule_engine.eligible_workers(inst, o)
        day = day_or_error(o, args.get("day"))
        schedules = rule_engine.schedules_from(inst, ctx.decisions, params, exclude=o.id)
        slots = rule_engine.stage_slots(inst, o, stage, certified,
                                        lambda w, dd: schedules.get((w.id, dd), []), params, day)
        threshold = rule_engine.effective_params(inst, o, params)["cei"]["master_threshold"]
        return {
            "stage": stage,
            "day": day,
            "time_window_min": params["matching"]["time_window_min"][stage - 1],
            "area_extension_km": params["matching"]["area_extension_km"][stage - 1],
            "funnel": {"branch": len(pool), "skilled": len(skilled), "work_type": len(capable),
                       "certified": len(certified)},
            "candidates": [
                {"worker_id": s.worker.id, "cei": s.worker.cei, "master": s.worker.cei >= threshold,
                 "start_time": min_to_hhmm(s.start), "travel_min": s.travel,
                 "duration_min": rule_engine.duration_min(o, params, s.worker)}
                for s in sorted(slots, key=lambda s: s.worker.id)
            ],
        }

    def get_travel_time(args: dict, ctx: ToolContext):
        o = order_or_error(args.get("order_id"))
        w = worker_or_error(args.get("worker_id"))
        jobs = rule_engine.schedules_from(inst, ctx.decisions, params, exclude=o.id).get((w.id, o.day), [])
        before = [j for j in jobs if j.start <= o.desired]
        px, py, origin = ((before[-1].order.x, before[-1].order.y, before[-1].order.id) if before
                          else (w.x, w.y, "작업자 좌표"))
        return {"from": origin, "travel_min": rule_engine.travel_min(px, py, o.x, o.y, params)}

    def get_worker_schedule(args: dict, ctx: ToolContext):
        w = worker_or_error(args.get("worker_id"))
        current = inst.order_index.get(ctx.item_id)
        try:
            day = int(args["day"]) if args.get("day") is not None else (current.day if current else 1)
        except (TypeError, ValueError):
            raise ToolError(f"day는 정수: {args.get('day')}") from None
        jobs = rule_engine.schedules_from(inst, ctx.decisions, params).get((w.id, day), [])
        return {
            "worker_id": w.id, "branch": w.branch, "group": w.group, "skills": w.skills,
            "work_types": w.work_types, "std_min": w.std_min, "certs": w.certs, "cei": w.cei,
            "master": w.cei >= params["cei"]["master_threshold"],
            "available": [min_to_hhmm(w.available[0]), min_to_hhmm(w.available[1])],
            "day": day,
            "capacity": [[min_to_hhmm(s), min_to_hhmm(e)] for s, e in w.windows(day)],
            "jobs": [{"order_id": j.order.id, "start": min_to_hhmm(j.start), "end": min_to_hhmm(j.end),
                      "x": j.order.x, "y": j.order.y, "fixed": j.fixed} for j in jobs],
        }

    def check_assignment(args: dict, ctx: ToolContext):
        o = order_or_error(args.get("order_id"))
        candidate = DecisionRecord(
            item_id=o.id,
            decision={"worker_id": args.get("worker_id"), "start_time": args.get("start_time"),
                      **({"day": args["day"]} if args.get("day") is not None else {})},
            status="success", reason_code=None, evidence="", dims={})
        others = [d for d in ctx.decisions if d.item_id != o.id]
        # 겹침 위반은 늦게 시작하는 작업에 붙으므로, 후보를 넣기 전후를 비교해 새로 생긴 위반을 모두 본다
        before = Counter((v.item_id, v.rule, v.message) for v in rule_engine.validate(inst, others, params))
        violations = []
        for v in rule_engine.validate(inst, others + [candidate], params):
            key = (v.item_id, v.rule, v.message)
            if before[key]:
                before[key] -= 1
            else:
                violations.append(v)
        return {"ok": not violations,
                "violations": [{"item_id": v.item_id, "rule": v.rule, "message": v.message} for v in violations]}

    handlers = {"get_order": get_order, "find_candidates": find_candidates, "get_travel_time": get_travel_time,
                "get_worker_schedule": get_worker_schedule, "check_assignment": check_assignment}
    return [{**spec, "handler": _guard(handlers[spec["name"]])} for spec in TOOL_SPECS]


class ToolError(Exception):
    pass


def _guard(fn):
    def run(args: dict, ctx: ToolContext):
        try:
            return fn(args or {}, ctx)
        except ToolError as exc:
            return {"error": str(exc)}
    return run
