"""변경 이력 카드 (M13-E): 승인으로 params 버전이 오를 때마다 카드 하나. 이미 저장된 승인 기록으로 만든다."""

from core.improvement.history import change_card, change_record
from domains.dispatch.pack import get_pack

PARAMS = get_pack().params
RULE = {"when": {"area_zone": ["boundary"]}, "set": {"matching.area_extension_km[2]": 4}}


def test_change_record_keeps_values_before_and_after():
    proposal = {"params_changes": [{"path": "matching.time_window_min[2]", "value": 180}], "override_rules": [RULE]}
    rec = change_record(PARAMS, proposal)
    assert rec["changes"] == [{"path": "matching.time_window_min[2]", "before": 120, "after": 180}]
    assert rec["override_rules"] == [{**RULE, "before": {"matching.area_extension_km[2]": 3}}]


def proposal(decision, body=None):
    return {"id": "prop-1", "kind": "params", "status": "approved", "report_id": "rep-1",
            "body": body or {"title": "경계만 4km", "kind": "params", "target_findings": ["F1"], "override_rules": [RULE]},
            "simulation": {"before": {"assignment_rate": 0.69}, "after": {"assignment_rate": 0.78}, "violations_after": 0},
            "decision": decision}


REPORT = {"id": "rep-1", "body": {"findings": [{"id": "F1", "title": "경계 지역 실패"}, {"id": "F2", "title": "x"}]}}


def test_card_from_new_approval():
    decision = {"action": "approved", "note": "현장 확인", "approver": "김현장", "at": 10.0, "forced": False,
                "params_version_before": 1, "params_version_after": 2, **change_record(PARAMS, proposal({})["body"])}
    card = change_card(proposal(decision), REPORT)
    assert (card["version_before"], card["version_after"]) == (1, 2)
    assert card["findings"] == [{"id": "F1", "title": "경계 지역 실패"}]
    assert card["metrics_before"] == {"assignment_rate": 0.69} and card["metrics_after"] == {"assignment_rate": 0.78}
    assert card["approver"] == "김현장" and card["note"] == "현장 확인"
    assert card["override_rules"][0]["before"] == {"matching.area_extension_km[2]": 3}


def test_card_from_old_approval_without_new_keys():
    """M13 이전 승인 기록: 승인자·바뀌기 전 값이 없으면 None ('기록 없음')."""
    old = {"action": "approved", "note": "경계만", "at": 5.0, "params_version_before": 1, "params_version_after": 2}
    card = change_card(proposal(old), REPORT)
    assert card["approver"] is None and card["override_rules"][0]["before"] is None
    assert card["changes"] == [] and card["version_after"] == 2


def test_spec_or_unapproved_proposals_make_no_card():
    assert change_card({**proposal({"action": "approved"}), "kind": "spec"}, REPORT) is None
    assert change_card({**proposal(None), "status": "rejected"}, REPORT) is None


def test_change_record_follows_sequential_changes_to_the_same_path():
    proposal = {"params_changes": [{"path": "matching.time_window_min[2]", "value": 180},
                                   {"path": "matching.time_window_min[2]", "value": 60}],
                "override_rules": [{"when": {"hour": ["10"]}, "set": {"matching.time_window_min[2]": 30}}]}
    rec = change_record(PARAMS, proposal)
    assert [(c["before"], c["after"]) for c in rec["changes"]] == [(120, 180), (180, 60)]
    assert rec["override_rules"][0]["before"] == {"matching.time_window_min[2]": 60}   # 전역 값 바뀐 뒤 기준
