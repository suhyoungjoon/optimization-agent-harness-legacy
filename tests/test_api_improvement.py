"""분석·개선 API: 분석 → 채점·판정 → 개선안 → 시뮬레이션 → 승인·반려 → 이력 (가짜 LLM, 임시 파일)."""

import json
import shutil
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from api.main import create_app
from domains.dispatch.pack import DispatchPack
from tests.fake_llm import FakeLLM, tool_use

BOUNDARY_RULE = {"when": {"area_zone": ["boundary"]}, "set": {"matching.area_extension_km[2]": 4}}


def tool_outputs(messages):
    return [json.loads(b["content"]) for m in messages if m["role"] == "user" and isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "tool_result" and not b.get("is_error")]


def tool_ids(messages, name):
    return [b["id"] for m in messages if m["role"] == "assistant" for b in m["content"]
            if b.get("type") == "tool_use" and b["name"] == name]


def analyst_or_proposer(item, n, messages, tools):
    names = {t["name"] for t in tools}
    if "submit_report" in names:
        if n == 0:
            return tool_use("aggregate", {"group_by": ["area_zone"]})
        empty = {"items": 0, "failed": 0}
        rows = {"boundary": empty, "core": empty, **{r["area_zone"]: r for r in tool_outputs(messages)[0]["rows"]}}
        call = tool_ids(messages, "aggregate")[0]
        return tool_use("submit_report", {"summary": "경계 지역", "findings": [
            {"title": "경계 지역 실패", "slice": {"area_zone": ["boundary"]}, "reason_codes": ["OUT_OF_AREA"],
             "description": f"{rows['boundary']['items']}건 중 {rows['boundary']['failed']}건 실패",
             "cited_calls": [call]},
            {"title": "중심 지역 용량 부족", "slice": {"area_zone": ["core"]}, "reason_codes": ["CAPACITY"],
             "description": f"실패 {rows['core']['failed']}건", "cited_calls": [call]},
        ]})
    if n == 0:
        return tool_use("simulate_params", {"override_rules": [BOUNDARY_RULE]})
    return tool_use("submit_proposals", {"proposals": [
        {"title": "경계 지역만 3단계 +1km", "kind": "params", "rationale": "시험 결과 개선", "target_findings": ["F1"],
         "override_rules": [BOUNDARY_RULE]},
        {"title": "과도한 완화", "kind": "params", "rationale": "x", "target_findings": ["F1"],
         "params_changes": [{"path": "matching.area_extension_km[2]", "value": 9}]},
        {"title": "명세 예외 처리 보완", "kind": "spec", "rationale": "x", "target_findings": ["F1"],
         "spec_edits": [{"section": "예외 처리", "text": "- 경계 지역 지시서는 3단계까지 시도한다"}]},
    ]})


@pytest.fixture
def files(tmp_path, monkeypatch):
    params, spec = tmp_path / "params.yaml", tmp_path / "domain-spec.md"
    shutil.copy(DispatchPack.params_path(None), params)
    shutil.copy(DispatchPack.spec_path(None), spec)
    shutil.copy(Path(DispatchPack.params_path(None)).parent / "analysis_perspectives.yaml", tmp_path)   # 관점별 분석
    monkeypatch.setattr(DispatchPack, "params_path", lambda self: str(params))
    monkeypatch.setattr(DispatchPack, "spec_path", lambda self: str(spec))
    return params, spec


@pytest.fixture
def client(tmp_path, files):
    return TestClient(create_app(tmp_path / "h.db", serve_web=False,
                                 llm_factory=lambda: FakeLLM(analyst_or_proposer)))


def wait(client, url, field="status", pending=("running", "simulating")):
    for _ in range(200):
        body = client.get(url).json()
        if body[field] not in pending:
            return body
        time.sleep(0.02)
    raise AssertionError(f"timeout: {url}")


def test_full_improvement_cycle(client, files):
    params_path, spec_path = files
    ds = client.post("/domains/dispatch/datasets", json={"seed": 42, "faults": ["P4"]}).json()
    run = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()

    # 분석 + 채점 (정답표는 채점에만 쓴다)
    rep = client.post("/analysis", json={"run_id": run["run_id"]})
    assert rep.status_code == 202
    report = wait(client, f"/analysis/{rep.json()['id']}")
    assert report["status"] == "done", report.get("error")
    assert [f["id"] for f in report["body"]["findings"]] == ["F1", "F2"]
    s = report["score"]
    assert s["detected"] == 1 and s["total"] == 1 and s["unmatched_findings"] == ["F2"] and s["unlabeled"] == 1

    labeled = client.post(f"/analysis/{report['id']}/labels", json={"finding_id": "F2", "label": "false_positive"})
    assert labeled.json()["score"]["false_positives"] == 1
    assert client.post(f"/analysis/{report['id']}/labels", json={"finding_id": "F9", "label": "valid"}).status_code == 400

    # 개선안 생성 (AI가 시뮬레이션으로 먼저 시험)
    bat = client.post("/proposals", json={"report_id": report["id"]}).json()
    batch = wait(client, f"/proposals/batches/{bat['id']}")
    assert batch["status"] == "done" and batch["meta"]["trials"] == 1
    ok, too_far, spec = batch["proposals"]
    assert (ok["status"], too_far["status"], spec["status"]) == ("proposed", "invalid", "proposed")
    assert any("허용 범위" in e for e in too_far["errors"])

    # 시뮬레이션 전에는 승인 불가
    assert client.post(f"/proposals/{ok['id']}/approve", json={}).status_code == 400
    assert client.post(f"/proposals/{ok['id']}/approve", json={"force": True}).status_code == 400

    sim = client.post(f"/proposals/{ok['id']}/simulate", json={}).json()
    assert sim["status"] == "simulated"
    assert sim["simulation"]["after"]["assignment_rate"] > sim["simulation"]["before"]["assignment_rate"]
    assert sim["simulation"]["slices"]["F1"]["after"]["fail_rate"] < sim["simulation"]["slices"]["F1"]["before"]["fail_rate"]

    # 명세 개선안은 비용 확인이 먼저
    est = client.post(f"/proposals/{spec['id']}/simulate", json={"scope": ["D01-O001", "D01-O002"]}).json()
    assert est["needs_confirmation"] and est["estimate"]["items"] == 2 and est["estimate"]["runs"] == 2

    # 승인: params.yaml 반영(주석 보존, 버전 +1)
    version = yaml.safe_load(params_path.read_text(encoding="utf-8"))["version"]
    approved = client.post(f"/proposals/{ok['id']}/approve", json={"note": "경계만 완화"}).json()
    assert approved["status"] == "approved"
    written = yaml.safe_load(params_path.read_text(encoding="utf-8"))
    assert written["version"] == version + 1 and written["overrides"]["rules"] == [BOUNDARY_RULE]
    assert "# 이 값 이상이면 명장" in params_path.read_text(encoding="utf-8")
    assert approved["decision"]["params_version_after"] == version + 1

    # 반려와 이력
    client.post(f"/proposals/{spec['id']}/reject", json={"note": "AI 재실행 비용 대비 효과 불명"})
    history = client.get("/history").json()
    assert [(h["status"], h["round"]) for h in history] == [("approved", 1), ("rejected", None)]
    assert history[0]["before"] and history[0]["cycle"]["simulation_seconds"] is not None
    assert history[0]["cycle"]["analysis_seconds"] is not None

    # 승인 후 새 규칙 agent 실행은 새 파라미터 버전을 쓴다
    run2 = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()
    assert run2["params_version"] == version + 1
    assert run2["metrics"]["assignment_rate"] > run["metrics"]["assignment_rate"]


def test_spec_simulation_runs_ai_before_and_after(client, files):
    """확인(confirm) 후 명세 개선안은 같은 범위에서 AI agent를 개선 전·후 명세로 한 번씩 실행한다."""
    ds = client.post("/domains/dispatch/datasets", json={"seed": 42, "faults": ["P4"]}).json()
    scope = client.get(f"/domains/dispatch/datasets/{ds['id']}").json()["item_ids"][:3]
    run = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule", "scope": scope}).json()
    report = wait(client, f"/analysis/{client.post('/analysis', json={'run_id': run['run_id']}).json()['id']}")
    batch = wait(client, f"/proposals/batches/{client.post('/proposals', json={'report_id': report['id']}).json()['id']}")
    spec = next(p for p in batch["proposals"] if p["kind"] == "spec")
    res = client.post(f"/proposals/{spec['id']}/simulate", json={"confirm": True, "level": "L1"})
    assert res.status_code == 202
    done = wait(client, f"/proposals/{spec['id']}")
    assert done["status"] == "simulated", done["simulation"]
    sim = done["simulation"]
    assert set(sim["run_ids"]) == {"before", "after"} and sim["items"] == 3
    after_run = client.get(f"/runs/{sim['run_ids']['after']}").json()
    assert after_run["level"] == "L1" and after_run["scope"] == scope


def p3_analyst(item, n, messages, tools):
    if n == 0:
        return tool_use("worker_stats", {"limit": 3})
    return tool_use("submit_report", {"summary": "저활용 작업자", "findings": [
        {"title": "저활용 작업자", "description": "일부 작업자의 활용률이 낮다",
         "metric": {"name": "worker_utilization", "direction": "low"},
         "cited_calls": tool_ids(messages, "worker_stats")}]})


def test_cause_confirmation_label(tmp_path, files):
    """원인 확인이 필요한 결함(P3)은 사람이 '원인 맞음'으로 판정해야 탐지로 센다."""
    client = TestClient(create_app(tmp_path / "h.db", serve_web=False, llm_factory=lambda: FakeLLM(p3_analyst)))
    ds = client.post("/domains/dispatch/datasets", json={"seed": 42, "faults": ["P3"]}).json()
    run = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()
    report = wait(client, f"/analysis/{client.post('/analysis', json={'run_id': run['run_id']}).json()['id']}")
    s = report["score"]
    assert s["faults"]["P3"]["status"] == "pending" and (s["detected"], s["pending"]) == (0, 1)

    ok = client.post(f"/analysis/{report['id']}/labels", json={"finding_id": "F1", "label": "cause_ok"}).json()
    assert ok["score"]["faults"]["P3"]["status"] == "detected" and ok["score"]["detected"] == 1
    wrong = client.post(f"/analysis/{report['id']}/labels", json={"finding_id": "F1", "label": "cause_wrong"}).json()
    assert wrong["score"]["faults"]["P3"]["status"] == "missed" and wrong["score"]["pending"] == 0


AREA_LIMIT = {"type": "param", "path": "matching.area_extension_km[2]", "max": 3, "source": "현장 담당자",
              "note": "관할 밖 3km까지만 출동 가능"}


def test_constraints_are_stored_and_checked_in_simulation(client):
    """제약은 개선안 묶음에 남고, 시뮬레이션 결과에 위반이 표시된다. 위반이 있어도 승인은 사람이 정한다."""
    ds = client.post("/domains/dispatch/datasets", json={"seed": 42, "faults": ["P4"]}).json()
    run = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()
    report = wait(client, f"/analysis/{client.post('/analysis', json={'run_id': run['run_id']}).json()['id']}")

    bad = client.post("/proposals", json={"report_id": report["id"], "constraints": [{**AREA_LIMIT, "max": "3km"}]})
    assert bad.status_code == 400 and "숫자" in bad.text

    bat = client.post("/proposals", json={"report_id": report["id"], "constraints": [AREA_LIMIT]}).json()
    batch = wait(client, f"/proposals/batches/{bat['id']}")
    assert batch["meta"]["constraints"] == [AREA_LIMIT]
    boundary = batch["proposals"][0]                                  # 경계 지역만 3단계 4km → 제약(3km) 위반
    sim = client.post(f"/proposals/{boundary['id']}/simulate", json={}).json()["simulation"]
    assert len(sim["constraint_violations"]) == 1 and "구간 조건" in sim["constraint_violations"][0]["message"]
    approved = client.post(f"/proposals/{boundary['id']}/approve", json={"note": "현장과 재협의 완료"})
    assert approved.status_code == 200 and approved.json()["status"] == "approved"


def test_no_constraints_no_violation_field(client):
    ds = client.post("/domains/dispatch/datasets", json={"seed": 42, "faults": ["P4"]}).json()
    run = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()
    report = wait(client, f"/analysis/{client.post('/analysis', json={'run_id': run['run_id']}).json()['id']}")
    batch = wait(client, f"/proposals/batches/{client.post('/proposals', json={'report_id': report['id']}).json()['id']}")
    assert "constraints" not in (batch["meta"] or {})
    sim = client.post(f"/proposals/{batch['proposals'][0]['id']}/simulate", json={}).json()["simulation"]
    assert "constraint_violations" not in sim


def test_second_cycle_gets_human_judgments_as_memory(tmp_path, files):
    """1회차의 오탐 판정과 반려 사유가 2회차 분석·개선 에이전트 입력으로 들어가고, 쓴 기억이 기록된다 (M12-c)."""
    llms = []

    def factory():
        llms.append(FakeLLM(analyst_or_proposer))
        return llms[-1]

    client = TestClient(create_app(tmp_path / "h.db", serve_web=False, llm_factory=factory))
    ds = client.post("/domains/dispatch/datasets", json={"seed": 42, "faults": ["P4"]}).json()
    run = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()

    # 1회차: 분석 → 오탐 판정, 개선안 → 명세안 반려
    first = wait(client, f"/analysis/{client.post('/analysis', json={'run_id': run['run_id']}).json()['id']}")
    assert first["body"]["memory"]["item_ids"] == []                       # 처음에는 기억이 없다
    client.post(f"/analysis/{first['id']}/labels", json={"finding_id": "F2", "label": "false_positive"})
    batch = wait(client, f"/proposals/batches/{client.post('/proposals', json={'report_id': first['id']}).json()['id']}")
    spec = next(p for p in batch["proposals"] if p["kind"] == "spec")
    client.post(f"/proposals/{spec['id']}/reject", json={"note": "AI 재실행 비용 대비 효과 불명"})

    memory = client.get("/memory", params={"domain": "dispatch"}).json()
    assert len(memory["judgments"]) == 1 and len(memory["rejections"]) == 1

    # 2회차: 분석 입력에 오탐 판정, 개선안 입력에 반려 사유
    second = wait(client, f"/analysis/{client.post('/analysis', json={'run_id': run['run_id']}).json()['id']}")
    assert len(second["body"]["memory"]["item_ids"]) == 2 and second["body"]["memory"]["version"]
    analysis_input = llms[-1].calls[0]["messages"][0]["content"]
    assert "중심 지역 용량 부족" in analysis_input and "잘못 짚음" in analysis_input
    batch2 = wait(client, f"/proposals/batches/{client.post('/proposals', json={'report_id': second['id']}).json()['id']}")
    assert batch2["meta"]["memory"]["version"] == second["body"]["memory"]["version"]
    assert "AI 재실행 비용 대비 효과 불명" in llms[-1].calls[0]["messages"][0]["content"]

    # 기억 없이 (비교 실험용)
    plain = wait(client, f"/analysis/{client.post('/analysis', json={'run_id': run['run_id'], 'use_memory': False}).json()['id']}")
    assert plain["body"]["memory"] is None
    assert "잘못 짚음" not in llms[-1].calls[0]["messages"][0]["content"]


def test_llm_factory_receives_role(tmp_path, files):
    """분석과 개선안 에이전트는 역할별 모델로 만든다 (configs/llm.yaml의 roles)."""
    roles = []

    def factory(role=None):
        roles.append(role)
        return FakeLLM(analyst_or_proposer)

    client = TestClient(create_app(tmp_path / "h.db", serve_web=False, llm_factory=factory))
    ds = client.post("/domains/dispatch/datasets", json={"seed": 42, "faults": ["P4"]}).json()
    run = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()
    report = wait(client, f"/analysis/{client.post('/analysis', json={'run_id': run['run_id']}).json()['id']}")
    wait(client, f"/proposals/batches/{client.post('/proposals', json={'report_id': report['id']}).json()['id']}")
    assert roles == ["analysis", "proposals"]
    llm = client.get("/harness/levels").json()["llm"]
    assert set(llm["roles"]) >= {"analysis", "proposals"} and llm["roles"]["proposals"]["model"]


def test_perspective_analysis(tmp_path, files):
    """관점별 분석: 관점마다 클라이언트를 따로 만들고, 같은 발견은 하나로 합쳐 채점한다."""
    roles = []

    def factory(role=None):
        roles.append(role)
        return FakeLLM(analyst_or_proposer)

    client = TestClient(create_app(tmp_path / "h.db", serve_web=False, llm_factory=factory))
    ds = client.post("/domains/dispatch/datasets", json={"seed": 42, "faults": ["P4"]}).json()
    run = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()
    rep = client.post("/analysis", json={"run_id": run["run_id"], "perspectives": True, "use_memory": False})
    report = wait(client, f"/analysis/{rep.json()['id']}")
    assert report["status"] == "done", report.get("error")
    body = report["body"]
    assert roles == ["analysis"] * 3 and set(body["perspectives"]) == {"failure", "resource", "time"}
    assert [len(f["perspectives"]) for f in body["findings"]] == [3, 3]      # 세 관점이 같은 두 발견을 냄
    assert report["score"]["detected"] == 1 and body["usage"]["llm_calls"] == 6


def test_params_history_card_records_approver_and_values(client, files):
    """승인할 때 승인자와 바뀌기 전 값을 남기고, 이력 카드로 본다 (M13-E)."""
    ds = client.post("/domains/dispatch/datasets", json={"seed": 42, "faults": ["P4"]}).json()
    run = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()
    report = wait(client, f"/analysis/{client.post('/analysis', json={'run_id': run['run_id']}).json()['id']}")
    batch = wait(client, f"/proposals/batches/{client.post('/proposals', json={'report_id': report['id']}).json()['id']}")
    ok = batch["proposals"][0]
    client.post(f"/proposals/{ok['id']}/simulate", json={})
    approved = client.post(f"/proposals/{ok['id']}/approve", json={"note": "경계만 완화", "approver": " 김현장 "}).json()
    assert approved["decision"]["approver"] == "김현장"
    assert approved["decision"]["override_rules"][0]["before"] == {"matching.area_extension_km[2]": 3}
    cards = client.get("/params/history", params={"domain": "dispatch"}).json()
    assert len(cards) == 1
    card = cards[0]
    assert (card["version_before"], card["version_after"]) == (4, 5) and card["approver"] == "김현장"
    assert card["findings"][0]["id"] == "F1" and card["metrics_after"]["assignment_rate"] > card["metrics_before"]["assignment_rate"]


def test_stored_non_policy_proposal_is_rechecked_on_simulate_and_forced_approve(client, files, tmp_path):
    """분류 도입 전에 저장된 안처럼, 추정값을 바꾸는 개선안이 저장돼 있어도 시뮬레이션·강제 승인에서 다시 막힌다."""
    from core.storage.store import Store

    params_path, _spec = files
    ds = client.post("/domains/dispatch/datasets", json={"seed": 42, "faults": ["P4"]}).json()
    run = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()
    report = wait(client, f"/analysis/{client.post('/analysis', json={'run_id': run['run_id']}).json()['id']}")
    batch = wait(client, f"/proposals/batches/{client.post('/proposals', json={'report_id': report['id']}).json()['id']}")
    ok = batch["proposals"][0]
    Store(tmp_path / "h.db").update_proposal(ok["id"], body=json.dumps({**ok["body"], "override_rules": [],    # 저장 형식(JSON 문자열)
        "params_changes": [{"path": "duration.base_min", "value": {"install": 45, "repair": 45}}]}))
    before = params_path.read_text(encoding="utf-8")

    sim = client.post(f"/proposals/{ok['id']}/simulate", json={})
    assert sim.status_code == 400 and "추정값" in sim.json()["detail"]
    forced = client.post(f"/proposals/{ok['id']}/approve", json={"force": True, "note": "강제"})
    assert forced.status_code == 400 and "추정값" in forced.json()["detail"]
    assert params_path.read_text(encoding="utf-8") == before                    # 규칙 파일은 그대로


def test_legacy_source_dataset_runs_analysis_and_simulation(client, tmp_path, monkeypatch):
    """원천 파일로 만든 데이터셋도 같은 흐름: 규칙 agent → 분석(truth.json으로 채점) → 개선안 시뮬레이션."""
    from domains.dispatch import legacy
    from domains.dispatch.generator import generate
    inst, truth = generate(42, ["P4"])
    legacy.export(inst, truth, tmp_path / "legacy" / "p4")
    monkeypatch.setenv("LEGACY_DATA_DIR", str(tmp_path / "legacy"))
    ds = client.post("/domains/dispatch/datasets", json={"source": "p4"}).json()
    run = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()
    report = wait(client, f"/analysis/{client.post('/analysis', json={'run_id': run['run_id']}).json()['id']}")
    assert report["status"] == "done" and report["score"]["detected"] == 1      # truth.json의 P4로 채점
    batch = wait(client, f"/proposals/batches/{client.post('/proposals', json={'report_id': report['id']}).json()['id']}")
    ok = batch["proposals"][0]
    sim = client.post(f"/proposals/{ok['id']}/simulate", json={}).json()
    assert sim["simulation"]["after"]["assignment_rate"] > sim["simulation"]["before"]["assignment_rate"]


def test_legacy_source_without_truth_is_not_scored(client, tmp_path, monkeypatch):
    from domains.dispatch import legacy
    from domains.dispatch.generator import generate
    inst, _ = generate(42, ["P4"])
    legacy.export(inst, None, tmp_path / "legacy" / "plain")
    monkeypatch.setenv("LEGACY_DATA_DIR", str(tmp_path / "legacy"))
    ds = client.post("/domains/dispatch/datasets", json={"source": "plain"}).json()
    run = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()
    report = wait(client, f"/analysis/{client.post('/analysis', json={'run_id': run['run_id']}).json()['id']}")
    assert report["status"] == "done" and report["score"]["total"] == 0
