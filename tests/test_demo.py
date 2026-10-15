"""시연 모드 (M5 완료 기준): 네트워크 없이 저장된 결과로 전체 흐름이 돈다.

1. 가짜 LLM으로 실험 세션을 만든다 (규칙 전체 실행, AI L3 2회, 분석, 개선안, 명세 시뮬레이션, 승인까지).
2. 번들로 내보낸다: 개선안 결정·사람 판정은 초기화, 도메인 파일은 스냅샷.
3. 시연 모드로 연다: 소켓 연결을 막고 LLM 생성 자체를 금지한 상태에서 같은 흐름을 재생한다.
"""

import json
import shutil
import socket
import sqlite3
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from api.main import create_app
from core.registry import set_domain_files_root
from domains.dispatch.pack import DispatchPack
from scripts.snapshot import export_bundle
from tests.fake_llm import FakeLLM, submit
from tests.test_api_improvement import analyst_or_proposer, wait

REPO_PARAMS = Path(DispatchPack.params_path(None))


def session_policy(item, n, messages, tools):
    names = {t["name"] for t in tools}
    if "submit_decision" in names:          # 하네스 러너(AI agent)
        return submit(item, reason="CAPACITY")
    return analyst_or_proposer(item, n, messages, tools)


@pytest.fixture
def source_files(tmp_path):
    """원본 세션이 쓸 도메인 파일 복사본 (레포 파일은 건드리지 않는다)."""
    root = tmp_path / "src-domains"
    target = root / "dispatch"
    target.mkdir(parents=True)
    for name in ("params.yaml", "domain-spec.md", "faults.yaml", "dimensions.yaml"):
        shutil.copy(REPO_PARAMS.parent / name, target / name)
    set_domain_files_root(root)
    yield target
    set_domain_files_root(None)


def build_session(tmp_path, files):
    client = TestClient(create_app(tmp_path / "session.db", serve_web=False,
                                   llm_factory=lambda: FakeLLM(session_policy)))
    ds = client.post("/domains/dispatch/datasets", json={"seed": 42, "faults": ["P4"]}).json()
    scope = client.get(f"/domains/dispatch/datasets/{ds['id']}").json()["item_ids"][:5]
    rule = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()
    ai = client.post("/runs", json={"dataset_id": ds["id"], "agent": "ai", "level": "L3", "repeats": 2,
                                    "scope": scope}).json()
    for r in ai["runs"]:
        wait(client, f"/runs/{r['run_id']}", pending=("running",))
    report = wait(client, f"/analysis/{client.post('/analysis', json={'run_id': rule['run_id']}).json()['id']}")
    client.post(f"/analysis/{report['id']}/labels", json={"finding_id": "F2", "label": "false_positive"})
    batch = wait(client, f"/proposals/batches/{client.post('/proposals', json={'report_id': report['id']}).json()['id']}")
    ok, _too_far, spec = batch["proposals"]
    client.post(f"/proposals/{ok['id']}/simulate", json={})
    client.post(f"/proposals/{spec['id']}/simulate", json={"confirm": True, "level": "L1", "scope": scope[:3]})
    wait(client, f"/proposals/{spec['id']}")
    client.post(f"/proposals/{ok['id']}/approve", json={"note": "원본 세션 승인"})
    assert yaml.safe_load((files / "params.yaml").read_text(encoding="utf-8"))["version"] == 3   # 세션의 작업 파일은 v3
    return ds, scope


@pytest.fixture
def bundle(tmp_path, source_files):
    ds, scope = build_session(tmp_path, source_files)
    out = tmp_path / "bundle"
    manifest = export_bundle(tmp_path / "session.db", out, git_ref="HEAD", note="test")
    set_domain_files_root(None)
    return out, manifest, ds, scope


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture
def offline(monkeypatch):
    """외부 연결은 막고 루프백만 허용한다 (윈도우 asyncio는 이벤트 루프를 만들 때 127.0.0.1 소켓쌍을 쓴다)."""
    connect, create_connection = socket.socket.connect, socket.create_connection

    def guarded_connect(sock, address, *args, **kwargs):
        if isinstance(address, tuple) and address[0] in LOOPBACK:
            return connect(sock, address, *args, **kwargs)
        raise AssertionError(f"시연 모드에서 네트워크 연결 시도: {address}")

    def guarded_create_connection(address, *args, **kwargs):
        if address[0] in LOOPBACK:
            return create_connection(address, *args, **kwargs)
        raise AssertionError(f"시연 모드에서 네트워크 연결 시도: {address}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    with pytest.raises(AssertionError):                       # 외부 주소는 실제로 막힌다
        socket.create_connection(("203.0.113.1", 443), timeout=0.1)


def forbidden_llm():
    raise AssertionError("시연 모드에서 LLM 생성 시도")


def open_demo(bundle_dir, tmp_path):
    return TestClient(create_app(serve_web=False, llm_factory=forbidden_llm, demo_bundle=bundle_dir,
                                 demo_work_root=tmp_path / "work", replay_seconds=0.2))


def test_export_resets_decisions_and_snapshots_committed_files(bundle):
    out, manifest, _ds, _scope = bundle
    assert manifest["counts"]["ai_runs"] >= 2 and manifest["counts"]["reports"] == 1
    # 세션에서 v3로 승인했지만, 번들은 git HEAD(커밋된 v2)에서 시작한다
    assert yaml.safe_load((out / "domains/dispatch/params.yaml").read_text(encoding="utf-8"))["version"] == 2
    assert manifest["domain_file_sources"]["dispatch/params.yaml"] == "git:HEAD"
    db = sqlite3.connect(out / "harness.db")
    statuses = sorted(r[0] for r in db.execute("SELECT status FROM proposals"))
    assert statuses == ["invalid", "proposed", "proposed"]
    assert db.execute("SELECT simulation FROM proposals WHERE kind = 'spec'").fetchone()[0] is not None
    assert db.execute("SELECT simulation FROM proposals WHERE kind = 'params'").fetchall() == [(None,), (None,)]
    assert db.execute("SELECT labels FROM reports").fetchone()[0] == "{}"


def test_demo_runs_full_flow_offline(bundle, tmp_path, offline):
    out, _manifest, ds, scope = bundle
    before_bundle = (out / "domains/dispatch/params.yaml").read_text(encoding="utf-8")
    before_repo = REPO_PARAMS.read_text(encoding="utf-8")
    try:
        client = open_demo(out, tmp_path)
        info = client.get("/demo").json()
        assert info["demo"] and info["catalog"] == [{"dataset_id": ds["id"], "level": "L3", "items": 5, "runs": 2}]
        assert client.get("/harness/levels").json()["demo"]["note"] == "test"

        # 데이터 생성과 규칙 agent는 실제로 계산한다
        assert client.post("/domains/dispatch/datasets", json={"seed": 42, "faults": ["P4"]}).json()["id"] == ds["id"]
        rule = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()
        assert rule["status"] == "done" and rule["params_version"] == 2

        # AI 실행은 저장된 결과를 재생 (진행률을 보여준 뒤 done)
        res = client.post("/runs", json={"dataset_id": ds["id"], "agent": "ai", "level": "L3", "repeats": 2,
                                         "scope": scope})
        assert res.status_code == 202 and res.json()["replayed"]
        runs = res.json()["runs"]
        assert len(runs) == 2 and runs[0]["status"] == "running"
        with client.stream("GET", f"/runs/{runs[0]['run_id']}/stream?interval=0.02") as s:
            events = [json.loads(l[6:]) for l in s.iter_lines() if l.startswith("data: ")]
        assert events[-1]["status"] == "done" and len(events) >= 2
        assert len(client.get(f"/runs/{runs[0]['run_id']}/decisions").json()) == 5
        missing = client.post("/runs", json={"dataset_id": ds["id"], "agent": "ai", "level": "L5", "scope": scope})
        assert missing.status_code == 404 and "저장된 AI 실행이 없음" in missing.json()["detail"]

        # 분석: 새로 계산한 규칙 실행과 같은 조건의 저장 리포트를 재생
        rep = client.post("/analysis", json={"run_id": rule["run_id"]}).json()
        report = client.get(f"/analysis/{rep['id']}").json()
        assert report["status"] == "done" and report["score"]["detected"] == 1 and report["score"]["unlabeled"] == 1
        assert report["run_id"] == rule["run_id"]      # 화면이 분석 대상 실행의 결과를 함께 그릴 수 있게

        # 개선: 저장된 개선안을 결정 전 상태로 재생, params 시뮬레이션은 실제 계산
        batch = client.get(f"/proposals/batches/{client.post('/proposals', json={'report_id': rep['id']}).json()['id']}").json()
        ok, too_far, spec = batch["proposals"]
        assert (ok["status"], too_far["status"], spec["status"]) == ("proposed", "invalid", "proposed")
        sim = client.post(f"/proposals/{ok['id']}/simulate", json={}).json()
        assert sim["simulation"]["after"]["assignment_rate"] > sim["simulation"]["before"]["assignment_rate"]
        est = client.post(f"/proposals/{spec['id']}/simulate", json={"scope": scope[:3]}).json()
        assert est["needs_confirmation"] and est["estimate"]["replayed"]
        spec_sim = client.post(f"/proposals/{spec['id']}/simulate", json={"confirm": True, "scope": scope[:3]}).json()
        assert spec_sim["status"] == "simulated" and spec_sim["simulation"]["replayed"]

        # 승인은 작업 복사본에만 반영된다
        approved = client.post(f"/proposals/{ok['id']}/approve", json={"note": "시연 승인"}).json()
        assert approved["decision"]["params_version_after"] == 3
        work_params = Path(info["work_dir"]) / "domains/dispatch/params.yaml"
        assert yaml.safe_load(work_params.read_text(encoding="utf-8"))["version"] == 3
        rule2 = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()
        assert rule2["params_version"] == 3 and rule2["metrics"]["assignment_rate"] > rule["metrics"]["assignment_rate"]
        assert [h["round"] for h in client.get("/history").json()] == [1]
    finally:
        set_domain_files_root(None)

    assert (out / "domains/dispatch/params.yaml").read_text(encoding="utf-8") == before_bundle   # 번들은 그대로
    assert REPO_PARAMS.read_text(encoding="utf-8") == before_repo                                # 레포 파일도 그대로

    # 다시 열면 처음 상태
    try:
        again = open_demo(out, tmp_path)
        assert again.get("/history").json() == []
        assert again.get("/domains/dispatch/params").json()["params"]["version"] == 2
    finally:
        set_domain_files_root(None)


def test_cached_flag_outside_demo(bundle, tmp_path):
    """시연 모드가 아니어도 cached=true면 저장된 결과를 재생하고, 없으면 404 (LLM을 부르지 않는다)."""
    out, _m, ds, scope = bundle
    db = tmp_path / "copy.db"
    shutil.copy(out / "harness.db", db)
    client = TestClient(create_app(db, serve_web=False, llm_factory=forbidden_llm, replay_seconds=0))
    res = client.post("/runs", json={"dataset_id": ds["id"], "agent": "ai", "level": "L3", "scope": scope,
                                     "cached": True})
    assert res.status_code == 202 and res.json()["runs"][0]["status"] == "done"
    assert client.post("/runs", json={"dataset_id": ds["id"], "agent": "ai", "level": "L0", "scope": scope,
                                      "cached": True}).status_code == 404
