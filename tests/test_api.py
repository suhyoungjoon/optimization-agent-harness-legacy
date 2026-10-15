"""API: 데이터셋 생성·조회, 규칙·AI agent 실행, 결정·트레이스 조회, 비교."""

import json

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from core.llm.client import load_config
from tests.fake_llm import FakeLLM, submit


def unassign_all(item, n, messages, tools):
    return submit(item, reason="CAPACITY")


@pytest.fixture
def client(tmp_path):
    # 테스트에서 실제 API를 호출하지 않도록 항상 가짜 LLM을 쓴다
    return TestClient(create_app(tmp_path / "harness.db", serve_web=False,
                                 llm_factory=lambda: FakeLLM(unassign_all)))


def test_list_domains(client):
    body = client.get("/domains").json()
    dispatch = next(d for d in body if d["name"] == "dispatch")
    assert [f["id"] for f in dispatch["faults"]] == ["P1", "P2", "P3", "P4", "P5"]
    assert "answer" not in dispatch["faults"][0]
    assert "branch" in dispatch["dimensions"]["dimensions"]


def test_dataset_create_and_get(client):
    res = client.post("/domains/dispatch/datasets", json={"seed": 42, "faults": ["P1"]})
    assert res.status_code == 200
    ds = res.json()
    assert ds["id"] == "dispatch-s42-P1" and ds["items"] == 1850

    body = client.get(f"/domains/dispatch/datasets/{ds['id']}").json()
    assert len(body["instance"]["orders"]) == 1850
    assert len(body["instance"]["workers"]) == 30
    assert "truth" not in body and "affected_items" not in str(body)[:10000]


def test_dataset_errors(client):
    assert client.post("/domains/nope/datasets", json={"seed": 1}).status_code == 404
    assert client.post("/domains/dispatch/datasets", json={"seed": 1, "faults": ["P9"]}).status_code == 400
    assert client.get("/domains/dispatch/datasets/missing").status_code == 404


def test_rule_run_and_decisions(client):
    ds = client.post("/domains/dispatch/datasets", json={"seed": 7}).json()
    run = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()
    assert run["status"] == "done" and run["violations"] == []
    assert run["seed"] == 7 and run["params_version"] == 2

    assert client.get(f"/runs/{run['run_id']}").json()["metrics"] == run["metrics"]
    decisions = client.get(f"/runs/{run['run_id']}/decisions").json()
    assert len(decisions) == 1850
    success = [d for d in decisions if d["status"] == "success"]
    assert len(success) / len(decisions) == pytest.approx(run["metrics"]["assignment_rate"])


def test_run_errors(client):
    assert client.post("/runs", json={"dataset_id": "missing"}).status_code == 404
    ds = client.post("/domains/dispatch/datasets", json={"seed": 1}).json()
    assert client.get("/runs/missing").status_code == 404
    assert client.get("/runs/missing/decisions").status_code == 404


# --- [M3] AI agent 실행, SSE, 트레이스, 비교 -------------------------------

@pytest.fixture
def ai_client(client):
    return client


def wait_done(client, run_id):
    """SSE 스트림을 끝까지 읽고 마지막 이벤트를 돌려준다."""
    with client.stream("GET", f"/runs/{run_id}/stream?interval=0.02") as res:
        events = [json.loads(line[6:]) for line in res.iter_lines() if line.startswith("data: ")]
    return events


def test_ai_run_lifecycle(ai_client):
    ds = ai_client.post("/domains/dispatch/datasets", json={"seed": 42}).json()
    scope = ai_client.get(f"/domains/dispatch/datasets/{ds['id']}").json()["item_ids"][:5]
    res = ai_client.post("/runs", json={"dataset_id": ds["id"], "agent": "ai", "level": "L5",
                                        "repeats": 2, "scope": scope})
    assert res.status_code == 202
    body = res.json()
    assert len(body["runs"]) == 2 and {r["repeat"] for r in body["runs"]} == {0, 1}
    run_id = body["runs"][0]["run_id"]

    events = wait_done(ai_client, run_id)
    assert events[-1]["status"] == "done" and events[-1]["done"] == events[-1]["total"] == 5
    wait_done(ai_client, body["runs"][1]["run_id"])

    run = ai_client.get(f"/runs/{run_id}").json()
    assert run["level"] == "L5" and run["model"] == "fake-model" and run["scope"] == scope
    assert run["metrics"]["assignment_rate"] == 0
    decisions = ai_client.get(f"/runs/{run_id}/decisions").json()
    assert {d["reason_code"] for d in decisions} == {"CAPACITY"}
    traces = ai_client.get(f"/runs/{run_id}/traces/{scope[0]}").json()
    assert [t["kind"] for t in traces] == ["llm", "validate", "guardrail"]

    rule = ai_client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule", "scope": scope,
                                         "group_id": body["group_id"]}).json()
    group = ai_client.get(f"/runs?group_id={body['group_id']}").json()
    assert len(group) == 3
    table = ai_client.get("/compare?runs=" + ",".join(r["run_id"] for r in group)).json()
    rule_row, ai_row = table["summary"]
    assert rule_row["agent"] == "rule" and rule_row["metrics"]["assignment_rate"] > 0
    assert ai_row["level"] == "L5" and ai_row["runs"] == 2 and ai_row["consistency"] == 1.0
    assert rule["scope"] == scope


def test_ai_run_errors(ai_client, tmp_path):
    ds = ai_client.post("/domains/dispatch/datasets", json={"seed": 1}).json()
    assert ai_client.post("/runs", json={"dataset_id": ds["id"], "agent": "ai", "level": "L9"}).status_code == 400
    assert ai_client.post("/runs", json={"dataset_id": ds["id"], "agent": "ai", "level": "L0",
                                         "scope": ["nope"]}).status_code == 400
    assert ai_client.get("/compare?runs=").status_code == 400

    broken = TestClient(create_app(tmp_path / "b.db", serve_web=False,
                                   llm_factory=lambda: (_ for _ in ()).throw(RuntimeError("no key"))))
    ds = broken.post("/domains/dispatch/datasets", json={"seed": 1}).json()
    res = broken.post("/runs", json={"dataset_id": ds["id"], "agent": "ai", "level": "L0"})
    assert res.status_code == 503 and "no key" in res.json()["detail"]


def test_ai_run_failure_reported_in_stream(tmp_path):
    client = TestClient(create_app(tmp_path / "f.db", serve_web=False,
                                   llm_factory=lambda: FakeLLM(lambda *a: RuntimeError("down"))))
    ds = client.post("/domains/dispatch/datasets", json={"seed": 1}).json()
    scope = client.get(f"/domains/dispatch/datasets/{ds['id']}").json()["item_ids"][:4]
    run_id = client.post("/runs", json={"dataset_id": ds["id"], "agent": "ai", "level": "L2",
                                        "scope": scope}).json()["runs"][0]["run_id"]
    assert wait_done(client, run_id)[-1]["status"] == "error"
    assert "down" in client.get(f"/runs/{run_id}").json()["meta"]["error"]


def test_levels_and_domain_codes(client):
    body = client.get("/harness/levels").json()
    assert list(body["levels"]) == ["L0", "L1", "L2", "L3", "L4", "L5"]
    assert body["llm"]["model"] == load_config()["model"]
    assert "LLM_REFUSAL" in client.get("/domains").json()[0]["core_reason_codes"]


def test_domain_params(client):
    body = client.get("/domains/dispatch/params").json()
    assert body["params"]["version"] >= 1 and "overrides" in body["params"]
    assert list(body["spec_sections"])[0] == "목적"
    assert client.get("/domains/nope/params").status_code == 404


def test_domain_definition_hides_answers_by_default(client):
    d = client.get("/domains/dispatch/definition").json()
    assert d["params"]["matching"]["docs"]["time_window_min"]
    assert list(d["spec_sections"]) == ["목적", "필수 조건", "선호 조건", "판단 순서", "예외 처리", "사용 도구"]
    assert "violation_rules" in d["dimensions"] and "LLM_ERROR" in d["core_reason_codes"]
    assert d["files"]["params.yaml"] == "domains/dispatch/params.yaml"
    assert [f["id"] for f in d["faults"]] == ["P1", "P2", "P3", "P4", "P5"]
    assert all(set(f) == {"id", "name", "expected"} for f in d["faults"])     # 주입 방식·정답 없음
    full = client.get("/domains/dispatch/definition?answers=true").json()
    assert all({"generation", "answer"} <= set(f) for f in full["faults"])
    assert client.get("/domains/nope/definition").status_code == 404


def test_domain_definition_lists_tools_without_handlers(client):
    tools = client.get("/domains/dispatch/definition").json()["tools"]
    assert tools and all(set(t) == {"name", "description"} for t in tools)   # handler·input_schema 없음
    assert all(t["name"] and t["description"] for t in tools)
    assert "submit_decision" not in {t["name"] for t in tools}               # 결정 제출은 코어 도구


def test_harness_levels_include_llm_call_limit(client):
    llm = client.get("/harness/levels").json()["llm"]
    assert isinstance(llm["max_llm_calls_per_item"], int) and llm["max_llm_calls_per_item"] > 0
