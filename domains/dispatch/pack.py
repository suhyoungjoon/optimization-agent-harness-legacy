"""dispatch 도메인 팩. core.interfaces.DomainPack 구현."""

from pathlib import Path

import yaml

from core.interfaces import DecisionRecord, Violation
from core.registry import domain_file

from . import generator, legacy, metrics, rule_engine
from .analysis import build_analysis_tools
from .models import Instance
from .tools import build_tools

PACK_DIR = Path(__file__).resolve().parent
NAME = "dispatch"


class DispatchPack:
    name = NAME

    def __init__(self, params: dict | None = None):
        # validate·metrics는 작업소요·이동시간 계산에 params가 필요하다.
        # 개선안 시뮬레이션은 get_pack(params=후보안)으로 팩을 새로 만든다.
        self.params = params if params is not None else yaml.safe_load(
            Path(self.params_path()).read_text(encoding="utf-8"))

    def generate(self, seed: int, faults: list[str]) -> tuple[Instance, dict]:
        return generator.generate(seed, faults)

    def load(self, source: str) -> tuple[Instance, dict]:
        # 레가시 형식(I/F별 CSV) 폴더 이름. data/legacy 바로 아래만 읽는다
        return legacy.load_dir(legacy.resolve(source))

    def items(self, instance: Instance) -> list[str]:
        # 배정 대상은 미예약 지시서만 (기할당은 이미 잡힌 일정). 순서는 규칙 agent의 solve와 같다
        return [o.id for o in rule_engine.processing_order(instance)]

    def solve(self, instance: Instance, params: dict) -> list[DecisionRecord]:
        return rule_engine.solve(instance, params)

    def validate(self, instance: Instance, decisions: list[DecisionRecord]) -> list[Violation]:
        return rule_engine.validate(instance, decisions, self.params)

    def metrics(self, instance: Instance, decisions: list[DecisionRecord]) -> dict[str, float]:
        return metrics.compute(instance, decisions, self.params)

    def tools(self, instance: Instance) -> list[dict]:
        return build_tools(instance, self.params)

    def spec_path(self) -> str:
        return str(domain_file(NAME, "domain-spec.md", PACK_DIR))

    def params_path(self) -> str:
        return str(domain_file(NAME, "params.yaml", PACK_DIR))

    def dimensions(self) -> dict:
        return yaml.safe_load((PACK_DIR / "dimensions.yaml").read_text(encoding="utf-8"))

    # --- AI agent 하네스용 ---

    def decision_schema(self) -> dict:
        n_stages = len(self.params["matching"]["time_window_min"])
        return {
            "type": "object",
            "properties": {
                "worker_id": {"type": "string", "description": "배정할 작업자 ID"},
                "day": {"type": "integer", "minimum": 1,
                        "description": "배정일. 생략하면 희망일. 희망일에 자리가 없을 때만 늦출 수 있다"},
                "start_time": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$",
                               "description": "작업 시작 시각 HH:MM"},
                "matching_stage": {"type": "integer", "minimum": 1, "maximum": n_stages,
                                   "description": "적용한 매칭 단계"},
            },
            "required": ["worker_id", "start_time", "matching_stage"],
        }

    def item_dims(self, instance: Instance, item_id: str) -> dict[str, str]:
        return rule_engine.dims_of(instance, instance.order_index[item_id])

    def approval_reasons(self, instance: Instance, record: DecisionRecord,
                         decisions: list[DecisionRecord]) -> list[str]:
        if record.decision is None:
            return []
        order = instance.order_index.get(record.item_id)
        worker = instance.worker_index.get(str(record.decision.get("worker_id")))
        start = rule_engine.parse_start(record.decision)
        if order is None or worker is None or start is None:
            return []
        params = rule_engine.effective_params(instance, order, self.params)
        rules = params["approval_required"]
        threshold = params["cei"]["master_threshold"]
        reasons = []
        day = rule_engine.parse_day(order, record.decision)
        if day is None:
            return []
        if rules.get("day_shift") and day > order.day:
            reasons.append(f"희망일({order.day}일)보다 {day - order.day}일 늦은 배정")
        stage = rule_engine.actual_stage(instance, order, worker, start, self.params)
        if stage is None:
            reasons.append("매칭 3단계 범위를 벗어난 배정")
        elif stage >= rules["matching_stage_gte"]:
            reasons.append(f"{stage}단계 매칭 배정")
        if rules.get("non_master") and worker.cei < threshold and stage is not None:
            *_, certified = rule_engine.eligible_workers(instance, order)
            schedules = rule_engine.schedules_from(instance, decisions, self.params, exclude=order.id)
            slots = rule_engine.stage_slots(instance, order, stage, certified,
                                            lambda w, dd: schedules.get((w.id, dd), []), self.params, day)
            masters = sorted(s.worker.id for s in slots if s.worker.cei >= threshold)
            if masters:
                reasons.append(f"명장 후보({', '.join(masters)})가 있는데 일반 작업자 {worker.id} 배정")
        return reasons

    def subset(self, instance: Instance, item_ids: list[str]) -> Instance:
        # 기할당은 모두 남긴다: 미룬 날의 일정도 그대로여야 한다. 날짜 번호도 원래대로
        keep = set(item_ids) | set(instance.fixed)
        orders = [o for o in instance.orders if o.id in keep]
        return Instance(branches=instance.branches, boundary_zone_km=instance.boundary_zone_km,
                        workers=instance.workers, orders=orders, days=instance.days, geo=instance.geo,
                        fixed=instance.fixed)

    def analysis_tools(self, instance: Instance, decisions: list[DecisionRecord]) -> list[dict]:
        return build_analysis_tools(instance, decisions, self.params)


def get_pack(params: dict | None = None) -> DispatchPack:
    return DispatchPack(params)
