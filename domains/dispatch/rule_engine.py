"""규칙 엔진: solve(기준선 배정)와 validate(필수조건 검증).

판단 순서는 domain-spec.md를 따르고, 수치는 모두 params로 받는다.
"""

import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from core.interfaces import DecisionRecord, Violation
from core.params import apply_overrides

from .models import Instance, Order, Worker, hhmm_to_min, min_to_hhmm, road_distance, slot_of

# 난이도 → 필요한 자격. 난이도 값과 자격 이름이 같다 (outdoor, none은 자격 불필요).
REQUIRED_CERT = {"pole": "pole", "high_risk": "high_risk"}

# 작업유형 표시 이름 (근거 문구용)
WORK_TYPE_LABEL = {"install": "개통", "repair": "장애"}

# validate가 일정에 반영하는 상태. blocked·failed는 배정되지 않은 것으로 본다.
PLACED_STATUSES = {"success", "pending_approval"}


def duration_min(order: Order, params: dict) -> int:
    w = params["weights"]
    base = params["duration"]["base_min"][order.work_type]
    return math.ceil(base * w["difficulty"][order.difficulty] * w["building"][order.building_type])


def travel_min(x1: float, y1: float, x2: float, y2: float, params: dict) -> int:
    t = params["travel"]
    return math.ceil(road_distance(x1, y1, x2, y2, t["detour_factor"]) / t["avg_speed_kmh"] * 60)


def start_range(order: Order, window: int, params: dict) -> tuple[int, int]:
    """허용 오차 window(분) 안의 약속 시간대에 들어가는 시작 시각 범위 [lo, hi] (양끝 포함).

    약속은 slot_min 단위 방문창이고, 실제 시작 시각이 속한 시간대가 약속 시간대다.
    """
    slot = params["appointment"]["slot_min"]
    base, k = slot_of(order.desired, slot), window // slot
    return base - k * slot, base + (k + 1) * slot - 1


def slot_gap(order: Order, start: int, params: dict) -> int:
    """약속 시간대와 희망 시간대의 차이(분)."""
    slot = params["appointment"]["slot_min"]
    return abs(slot_of(start, slot) - slot_of(order.desired, slot))


def fmt_windows(windows: list[tuple[int, int]]) -> str:
    return ", ".join(f"{min_to_hhmm(s)}~{min_to_hhmm(e)}" for s, e in windows) or "없음(하루 전체 N)"


def dims_of(inst: Instance, order: Order) -> dict[str, str]:
    return {
        "branch": order.branch,
        "hour": f"{order.desired // 60:02d}",
        "work_type": order.work_type,
        "media": order.media,
        "building_type": order.building_type,
        "difficulty": order.difficulty,
        "area_zone": inst.area_zone(order),
    }


def effective_params(inst: Instance, order: Order, params: dict) -> dict:
    """지시서에 해당하는 구간 조건(overrides)을 적용한 파라미터."""
    return apply_overrides(params, dims_of(inst, order))


def parse_start(decision: dict) -> int | None:
    try:
        return hhmm_to_min(str(decision["start_time"]))
    except (KeyError, ValueError):
        return None


# --- validate ----------------------------------------------------------------

def validate(inst: Instance, decisions: list[DecisionRecord], params: dict) -> list[Violation]:
    violations: list[Violation] = []
    placed: dict[tuple[str, int], list[tuple[int, int, Order]]] = defaultdict(list)
    seen: set[str] = set()

    for d in decisions:
        if d.decision is None or d.status not in PLACED_STATUSES:
            continue
        order = inst.order_index.get(d.item_id)
        worker = inst.worker_index.get(str(d.decision.get("worker_id")))
        start = parse_start(d.decision)
        if order is None or worker is None or start is None:
            violations.append(Violation(d.item_id, "invalid_decision",
                                        f"알 수 없는 지시서·작업자이거나 start_time 형식 오류: {d.decision}"))
            continue
        if d.item_id in seen:
            violations.append(Violation(d.item_id, "invalid_decision", "같은 지시서에 결정이 두 번 있음"))
            continue
        seen.add(d.item_id)

        if order.media not in worker.skills:
            violations.append(Violation(d.item_id, "skill_required",
                                        f"{worker.id}는 {order.media} 기술이 없음 (보유: {worker.skills})"))
        if order.work_type not in worker.work_types:
            violations.append(Violation(d.item_id, "work_type_required",
                                        f"{worker.id}는 {WORK_TYPE_LABEL.get(order.work_type, order.work_type)} "
                                        f"역량이 없음 (보유: {worker.work_types})"))
        cert = REQUIRED_CERT.get(order.difficulty)
        if cert and cert not in worker.certs:
            violations.append(Violation(d.item_id, "cert_required",
                                        f"{worker.id}는 {cert} 자격이 없음"))
        end = start + duration_min(order, params)
        windows = worker.windows(order.day)
        if not any(a0 <= start and end <= a1 for a0, a1 in windows):
            violations.append(Violation(
                d.item_id, "outside_availability",
                f"{min_to_hhmm(start)}~{min_to_hhmm(end)}가 {worker.id}의 {order.day}일 시간역량 "
                f"{fmt_windows(windows)} 중 한 구간 안에 들지 않음"))
        placed[(worker.id, order.day)].append((start, end, order))

    for (worker_id, _day), jobs in placed.items():
        jobs.sort(key=lambda j: (j[0], j[2].id))
        for (_s, prev_end, prev), (nxt_start, _e, nxt) in zip(jobs, jobs[1:]):
            arrive = prev_end + travel_min(prev.x, prev.y, nxt.x, nxt.y, params)
            if arrive > nxt_start:
                violations.append(Violation(
                    nxt.id, "schedule_overlap",
                    f"{worker_id}: {prev.id} 종료 후 이동하면 {min_to_hhmm(arrive)} 도착, "
                    f"{nxt.id} 시작 {min_to_hhmm(nxt_start)}보다 늦음"))
    return violations


# --- solve -------------------------------------------------------------------

@dataclass
class Job:
    start: int
    end: int
    order: Order


@dataclass
class Slot:
    worker: Worker
    start: int
    travel: int                    # 직전 위치(첫 작업이면 작업자 좌표)에서의 이동시간


def _best_slot(worker: Worker, order: Order, dur: int, lo: int, hi: int,
               jobs: list[Job], params: dict) -> Slot | None:
    """[lo, hi] 안에서 그날 시간역량 구간과 기존 일정 사이에 들어가는 시작 시각 중
    약속 시간대가 희망 시간대에 가장 가깝고, 그다음 희망시각에 가장 가까운 것."""
    best: Slot | None = None
    best_key = None
    for a0, a1 in worker.windows(order.day):
        for k in range(len(jobs) + 1):
            prev = jobs[k - 1] if k > 0 else None
            nxt = jobs[k] if k < len(jobs) else None
            px, py = (prev.order.x, prev.order.y) if prev else (worker.x, worker.y)
            earliest = prev.end + travel_min(px, py, order.x, order.y, params) if prev else a0
            latest_end = (nxt.start - travel_min(order.x, order.y, nxt.order.x, nxt.order.y, params)
                          if nxt else a1)
            s_lo = max(earliest, lo, a0)
            s_hi = min(latest_end - dur, hi, a1 - dur)
            if s_lo > s_hi:
                continue
            start = min(max(order.desired, s_lo), s_hi)
            slot = Slot(worker, start, travel_min(px, py, order.x, order.y, params))
            key = (slot_gap(order, start, params), abs(start - order.desired), slot.travel)
            if best is None or key < best_key:
                best, best_key = slot, key
    return best


def _fail(inst: Instance, order: Order, code: str, evidence: str) -> DecisionRecord:
    return DecisionRecord(item_id=order.id, decision=None, status="failed", reason_code=code,
                          evidence=evidence, dims=dims_of(inst, order))


def eligible_workers(inst: Instance, order: Order) -> tuple[list[Worker], list[Worker], list[Worker], list[Worker]]:
    """(소속 지점 작업자, 기술 보유자, 기술·개통/AS 역량 보유자, 기술·역량·자격 보유자)."""
    pool = [w for w in inst.workers if w.branch == order.branch]
    skilled = [w for w in pool if order.media in w.skills]
    capable = [w for w in skilled if order.work_type in w.work_types]
    cert = REQUIRED_CERT.get(order.difficulty)
    certified = [w for w in capable if cert is None or cert in w.certs]
    return pool, skilled, capable, certified


def stage_slots(inst: Instance, order: Order, stage: int, certified: list[Worker],
                jobs_of: Callable[[Worker], list[Job]], params: dict) -> list[Slot]:
    """stage(1부터) 매칭 범위 안에서 들어갈 수 있는 작업자별 최선의 시작 시각."""
    params = effective_params(inst, order, params)
    window = params["matching"]["time_window_min"][stage - 1]
    ext = params["matching"]["area_extension_km"][stage - 1]
    dur = duration_min(order, params)
    in_area = [w for w in certified if inst.distance_outside(w.branch, order.x, order.y) <= ext]
    lo, hi = start_range(order, window, params)
    return [s for w in in_area if (s := _best_slot(w, order, dur, lo, hi, jobs_of(w), params))]


def pick(slots: list[Slot], params: dict) -> Slot:
    """명장 우선 → 이동시간 → CEI 높은 순 → 작업자 ID."""
    threshold = params["cei"]["master_threshold"]
    return min(slots, key=lambda s: (s.worker.cei < threshold, s.travel, -s.worker.cei, s.worker.id))


def actual_stage(inst: Instance, order: Order, worker: Worker, start: int, params: dict) -> int | None:
    """배정이 실제로 해당하는 가장 엄격한 매칭 단계. 3단계 범위도 벗어나면 None."""
    effective = effective_params(inst, order, params)
    m = effective["matching"]
    outside = inst.distance_outside(worker.branch, order.x, order.y)
    gap = slot_gap(order, start, effective)
    for stage, (window, ext) in enumerate(zip(m["time_window_min"], m["area_extension_km"]), start=1):
        if gap <= window and outside <= ext:
            return stage
    return None


def schedules_from(inst: Instance, decisions: list[DecisionRecord], params: dict,
                   exclude: str | None = None) -> dict[tuple[str, int], list[Job]]:
    """결정 목록에서 (작업자, 날짜)별 일정표를 만든다."""
    schedules: dict[tuple[str, int], list[Job]] = defaultdict(list)
    for d in decisions:
        if d.decision is None or d.status not in PLACED_STATUSES or d.item_id == exclude:
            continue
        order = inst.order_index.get(d.item_id)
        worker_id = str(d.decision.get("worker_id"))
        start = parse_start(d.decision)
        if order is None or worker_id not in inst.worker_index or start is None:
            continue
        schedules[(worker_id, order.day)].append(Job(start, start + duration_min(order, params), order))
    for jobs in schedules.values():
        jobs.sort(key=lambda j: j.start)
    return schedules


def _assign_one(inst: Instance, order: Order, params: dict,
                schedules: dict[str, list[Job]]) -> DecisionRecord:
    params = effective_params(inst, order, params)
    dur = duration_min(order, params)
    pool, skilled, capable, certified = eligible_workers(inst, order)
    cert = REQUIRED_CERT.get(order.difficulty)
    kind = WORK_TYPE_LABEL.get(order.work_type, order.work_type)
    funnel = (f"후보: {inst.branches[order.branch].name}지점({order.branch}) {len(pool)}명 → {order.media} 기술 "
              f"{len(skilled)}명 → {kind} 역량 {len(capable)}명")
    if cert:
        funnel += f" → {cert} 자격 {len(certified)}명"
    if not skilled:
        return _fail(inst, order, "NO_SKILL", f"{funnel}. 기술 보유자 없음")
    if not capable:
        return _fail(inst, order, "NO_WORK_TYPE", f"{funnel}. {kind} 역량 보유자 없음")
    if not certified:
        return _fail(inst, order, "NO_CERT", f"{funnel}. 자격 보유자 없음")

    matching = params["matching"]
    threshold = params["cei"]["master_threshold"]
    for stage, (window, ext) in enumerate(zip(matching["time_window_min"],
                                              matching["area_extension_km"]), start=1):
        slots = stage_slots(inst, order, stage, certified, lambda w: schedules[w.id], params)
        if not slots:
            continue
        chosen = pick(slots, params)
        w = chosen.worker
        schedules[w.id].append(Job(chosen.start, chosen.start + dur, order))
        schedules[w.id].sort(key=lambda j: j.start)
        grade = "명장" if w.cei >= threshold else "일반"
        diff = chosen.start - order.desired
        slot = params["appointment"]["slot_min"]
        appt = slot_of(chosen.start, slot)
        return DecisionRecord(
            item_id=order.id,
            decision={"worker_id": w.id, "start_time": min_to_hhmm(chosen.start), "matching_stage": stage},
            status="success",
            reason_code=None,
            evidence=(f"{funnel}. {stage}단계 매칭(희망 시간대 ±{window}분, +{ext}km)에서 가능 후보 {len(slots)}명 중 "
                      f"{w.id} 선택({grade}, CEI {w.cei}, 이동 {chosen.travel}분). "
                      f"약속 {min_to_hhmm(appt)}~{min_to_hhmm(appt + slot)}, 시작 {min_to_hhmm(chosen.start)}, "
                      f"희망 {min_to_hhmm(order.desired)}, 작업소요 {dur}분"),
            dims=dims_of(inst, order),
            metrics={"matching_stage": float(stage), "travel_min": float(chosen.travel),
                     "time_diff_min": float(abs(diff)), "duration_min": float(dur)},
        )

    # 3단계까지 실패: 마지막 단계 기준으로 사유를 가린다
    window, ext = matching["time_window_min"][-1], matching["area_extension_km"][-1]
    in_area = [w for w in certified if inst.distance_outside(w.branch, order.x, order.y) <= ext]
    if not in_area:
        outside = inst.distance_outside(order.branch, order.x, order.y)
        return _fail(inst, order, "OUT_OF_AREA",
                     f"{funnel}. 관할 밖 {outside:.1f}km로 최대 완화(+{ext}km) 범위를 벗어남")
    lo, hi = start_range(order, window, params)
    fits_availability = [w for w in in_area
                         if any(max(lo, a0) <= min(hi, a1 - dur) for a0, a1 in w.windows(order.day))]
    if fits_availability:
        return _fail(inst, order, "CAPACITY",
                     f"{funnel}. 희망 시간대 ±{window}분에 시간역량이 있는 {len(fits_availability)}명의 일정이 모두 참")
    return _fail(inst, order, "NO_TIME_MATCH",
                 f"{funnel}. 희망 시간대 ±{window}분이 후보 {len(in_area)}명의 {order.day}일 시간역량과 맞지 않음")


def solve(inst: Instance, params: dict) -> list[DecisionRecord]:
    """지시서를 날짜별·희망시간 순으로 하나씩 배정한다 (결정적)."""
    records: list[DecisionRecord] = []
    for day in sorted({o.day for o in inst.orders}):
        schedules: dict[str, list[Job]] = defaultdict(list)
        todays = sorted((o for o in inst.orders if o.day == day), key=lambda o: (o.desired, o.id))
        for order in todays:
            records.append(_assign_one(inst, order, params, schedules))
    return records
