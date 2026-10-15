"""dispatch 지표: 손으로 계산한 값과 비교."""

import pytest

from domains.dispatch import metrics, rule_engine
from tests.test_dispatch_rule_engine import PARAMS, small  # noqa: F401 (fixture)


def test_metrics_small_case(small):  # noqa: F811
    m = metrics.compute(small, rule_engine.solve(small, PARAMS), PARAMS)
    # 성공 4건: O1(W1 09:00), O9(W1 15:00), O2(W2 09:00), O3(W2 09:53, 희망과 같은 09시 시간대라 1단계)
    assert m["assignment_rate"] == pytest.approx(4 / 9)
    assert m["desired_time_match_rate"] == pytest.approx(4 / 4)
    assert m["on_time_rate"] == pytest.approx(4 / 9)       # 희망 시간대에 약속한 4건 / 전체 9건
    assert m["stage_1_share"] == pytest.approx(1)
    assert m["stage_2_share"] == 0
    assert m["stage_3_share"] == 0
    # 이동: W1 집→O1 1km 2분, O1→O9 11km 22분 / W2 집→O2 6km 12분, O2→O3 4km 8분
    assert m["avg_travel_min"] == pytest.approx((2 + 22 + 12 + 8) / 4)
    # 작업소요 60+60+45+90분 / 시간역량 W1 540분 + W2 480분(12~13시 N)
    assert m["worker_utilization"] == pytest.approx(255 / 1020)


def test_metrics_ignore_non_success(small):  # noqa: F811
    records = [r for r in rule_engine.solve(small, PARAMS) if r.status != "success"]
    m = metrics.compute(small, records, PARAMS)
    assert m["assignment_rate"] == 0 and m["avg_travel_min"] == 0
