"""개선 제약: 형식 검사와 위반 검출 (M12-a). 현장 의견에서 온 제약을 코드가 확인한다."""

import pytest

from core.improvement.changes import apply_params
from core.improvement.constraints import constraint_errors, constraint_violations
from core.registry import load_pack, load_params

METRICS = ["assignment_rate", "on_time_rate", "avg_travel_min"]
AREA = {"type": "param", "path": "matching.area_extension_km[2]", "max": 4, "source": "현장 담당자",
        "note": "강남 3km 초과 시 이동 30분 이상"}
ON_TIME = {"type": "metric", "metric": "on_time_rate", "max_drop": 0.03, "source": "운영팀"}


@pytest.fixture(scope="module")
def params():
    return load_params(load_pack("dispatch"))


def test_valid_constraints_have_no_errors(params):
    assert constraint_errors([AREA, ON_TIME], params, METRICS) == []
    assert constraint_errors([], params, METRICS) == []


@pytest.mark.parametrize("bad, expect", [
    ({**AREA, "path": "matching.nope[2]"}, "없는 파라미터"),
    ({**AREA, "path": "matching.bounds"}, "바꿀 수 없는 경로"),
    ({**ON_TIME, "metric": "unknown_rate"}, "없는 지표"),
    ({**AREA, "type": "other"}, "type"),
    ({**AREA, "limit": 3}, "모르는 키"),
    ({**AREA, "max": None}, "min·max 중 하나"),
    ({**AREA, "max": "4km"}, "숫자"),
    ({**AREA, "min": 5}, "min이 max보다 큼"),
    ({**ON_TIME, "max_drop": -0.1}, "0 이상"),
    ({k: v for k, v in AREA.items() if k != "source"}, "source"),
])
def test_constraint_format_errors(params, bad, expect):
    bad = {k: v for k, v in bad.items() if v is not None}
    errors = constraint_errors([bad], params, METRICS)
    assert errors and expect in errors[0], errors


def test_not_a_list(params):
    assert constraint_errors({"type": "param"}, params, METRICS)


def test_param_violation_global_value(params):
    candidate = apply_params(params, {"params_changes": [{"path": "matching.area_extension_km[2]", "value": 5}]})
    v = constraint_violations([AREA], candidate)
    assert len(v) == 1 and v[0]["index"] == 0 and "5" in v[0]["message"] and "현장 담당자" in v[0]["message"]
    assert constraint_violations([AREA], params) == []          # 현재 값 3은 제약 안


def test_param_violation_inside_override_rule(params):
    """구간 조건으로만 바꾼 값도 검사한다 (요소 경로, 목록 전체 설정 모두)."""
    by_element = apply_params(params, {"override_rules": [
        {"when": {"area_zone": ["boundary"]}, "set": {"matching.area_extension_km[2]": 5}}]})
    by_list = apply_params(params, {"override_rules": [
        {"when": {"hour": ["10"]}, "set": {"matching.area_extension_km": [0, 1, 6]}}]})
    within = apply_params(params, {"override_rules": [
        {"when": {"hour": ["10"]}, "set": {"matching.area_extension_km": [0, 2, 4]}}]})
    assert "구간 조건" in constraint_violations([AREA], by_element)[0]["message"]
    assert len(constraint_violations([AREA], by_list)) == 1
    assert constraint_violations([AREA], within) == []


def test_whole_list_path_checks_every_element(params):
    cap = {"type": "param", "path": "matching.time_window_min", "max": 150, "source": "s"}
    candidate = apply_params(params, {"params_changes": [{"path": "matching.time_window_min[2]", "value": 180}]})
    assert constraint_violations([cap], params) == []
    assert len(constraint_violations([cap], candidate)) == 1


def test_metric_violations():
    sim = {"before": {"on_time_rate": 0.494, "assignment_rate": 0.693, "avg_travel_min": 8.4},
           "after": {"on_time_rate": 0.439, "assignment_rate": 0.827, "avg_travel_min": 9.9}}
    assert len(constraint_violations([ON_TIME], {}, sim)) == 1                       # 5.5%p 감소 > 3%p
    assert constraint_violations([{**ON_TIME, "max_drop": 0.06}], {}, sim) == []
    rise = {"type": "metric", "metric": "avg_travel_min", "max_rise": 1.0, "source": "s"}
    floor = {"type": "metric", "metric": "assignment_rate", "min": 0.85, "source": "s"}
    assert len(constraint_violations([rise, floor], {}, sim)) == 2
    assert constraint_violations([ON_TIME], {}, None) == []                          # 시뮬레이션 전에는 지표 제약을 볼 수 없음
    assert constraint_violations([ON_TIME], {}, {"before": {}, "after": {}}) == []   # 해당 지표가 없으면 판단하지 않음


def test_constraints_are_public():
    import core
    assert core.constraint_errors is constraint_errors and core.constraint_violations is constraint_violations
