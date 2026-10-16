"""리허설 번들: API 키 없이 화면 전체 흐름(비교·트레이스·분석·개선)을 볼 수 있는 시연 번들을 만든다.

python -m scripts.rehearsal_bundle [--out demo/rehearsal] [--seed 42] [--faults P1,P2,P3,P4]
python -m api.main --demo demo/rehearsal

LLM 대신 가짜 LLM(tests/fake_llm.py)을 쓴다. 수치는 AI 성능과 무관하다 (흐름·화면 확인용).
- AI agent: 규칙 엔진의 결정을 그대로 제출하되, 세 건 중 한 건은 첫 제출을 가능시간 밖(05:00)으로 틀리게 낸다.
  검증 루프가 없는 L0에는 위반이 남고, L3·L5에서는 재시도로 교정된다. 범위는 1일차 앞 10건(화면의 "시범").
- 분석 agent: P1·P2·P4 구간을 집계 도구로 짚고, 정답표에 없는 발견 하나를 더 낸다 (P3은 놓침, 사람 판정 대상 1건).
- 개선 agent: 경계 지역 구간 조건(적용 가능), 허용 범위 밖 변경(검증 실패), 명세 수정(AI 전후 실행) 세 건을 제안한다.
실행에는 개발 의존성이 필요하다 (pip install -e ".[dev]").
"""

import argparse
import json
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from api.main import ROOT, create_app
from core.registry import load_pack, load_params
from scripts.snapshot import export_bundle
from tests.fake_llm import FakeLLM, submit, tool_use

LEVELS = ("L0", "L3", "L5")
SCOPE_SIZE = 10
BOUNDARY_RULE = {"when": {"area_zone": ["boundary"]}, "set": {"matching.area_extension_km[2]": 4}}
ANALYST_QUERIES = [
    ("aggregate", {"group_by": ["branch", "hour"], "filters": {"branch": ["B"], "hour": ["09", "10"]}}),
    ("aggregate", {"group_by": ["branch", "difficulty"], "filters": {"branch": ["C"], "difficulty": ["pole"]}}),
    ("aggregate", {"group_by": ["area_zone"]}),
]


def _tool_calls(messages):
    return [b for m in messages if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]


def _tool_outputs(messages):
    return [json.loads(b["content"]) for m in messages if m["role"] == "user" and isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "tool_result" and not b.get("is_error")]


def _analyst(n, messages):
    if n < len(ANALYST_QUERIES):
        return tool_use(*ANALYST_QUERIES[n])
    ids = [c["id"] for c in _tool_calls(messages)]
    b_am, c_pole, zones = _tool_outputs(messages)
    b_am, c_pole = b_am["rows"][0], c_pole["rows"][0]
    zone = {r["area_zone"]: r for r in zones["rows"]}
    return tool_use("submit_report", {"summary": "리허설: 가짜 분석 agent의 리포트", "findings": [
        {"title": "B지점 오전 실패 집중", "slice": {"branch": ["B"], "hour": ["09", "10"]},
         "reason_codes": ["CAPACITY"], "description": f"{b_am['items']}건 중 {b_am['failed']}건 실패",
         "cited_calls": [ids[0]]},
        {"title": "C지점 승주 작업 미할당", "slice": {"branch": ["C"], "difficulty": ["pole"]},
         "reason_codes": ["NO_CERT"], "description": f"{c_pole['items']}건 중 {c_pole['failed']}건 실패",
         "cited_calls": [ids[1]]},
        {"title": "경계 지역 실패", "slice": {"area_zone": ["boundary"]}, "reason_codes": ["OUT_OF_AREA"],
         "description": f"{zone['boundary']['items']}건 중 {zone['boundary']['failed']}건 실패",
         "cited_calls": [ids[2]]},
        {"title": "중심 지역 용량 부족", "slice": {"area_zone": ["core"]}, "reason_codes": ["CAPACITY"],
         "description": f"{zone['core']['items']}건 중 {zone['core']['failed']}건 실패", "cited_calls": [ids[2]]},
    ]})


def _proposer(n):
    if n == 0:
        return tool_use("simulate_params", {"override_rules": [BOUNDARY_RULE]})
    return tool_use("submit_proposals", {"proposals": [
        {"title": "경계 지역만 3단계 지역 범위 +1km", "kind": "params", "target_findings": ["F3"],
         "rationale": "경계 지역 실패는 대부분 OUT_OF_AREA. 전역 완화 대신 경계 구간에만 적용한다",
         "expected_effect": "경계 지역 할당 증가, 다른 구간 영향 최소", "override_rules": [BOUNDARY_RULE]},
        {"title": "전역 3단계 지역 범위 대폭 완화", "kind": "params", "target_findings": ["F3"],
         "rationale": "허용 범위 검사를 보여주기 위한 과도한 제안", "params_changes": [
             {"path": "matching.area_extension_km[2]", "value": 9}]},
        {"title": "예외 처리: 경계 지역은 3단계까지 시도", "kind": "spec", "target_findings": ["F3"],
         "rationale": "AI agent가 경계 지역에서 너무 일찍 미배정으로 포기하지 않게 한다",
         "spec_edits": [{"section": "예외 처리",
                         "text": "- 관할 경계 지역 지시서는 3단계 매칭까지 모두 시도한 뒤에만 미배정으로 남긴다"}]},
    ]})


def build_policy(pack, instance):
    rule = {d.item_id: d for d in pack.solve(instance, pack.params)}
    order = pack.items(instance)

    def policy(item, n, messages, tools):
        names = {t["name"] for t in tools}
        if "submit_report" in names:
            return _analyst(n, messages)
        if "submit_proposals" in names:
            return _proposer(n)
        d = rule[item]
        if d.status != "success":
            return submit(item, reason=d.reason_code)
        wrong_first = order.index(item) % 3 == 1 and n == 0
        return submit(item, d.decision["worker_id"], "05:00" if wrong_first else d.decision["start_time"],
                      d.decision["matching_stage"], day=d.decision.get("day"))

    return policy


def _wait(client, url, pending=("running", "simulating")):
    for _ in range(1500):
        body = client.get(url).json()
        if body["status"] not in pending:
            return body
        time.sleep(0.02)
    raise TimeoutError(url)


def build(out: str | Path, seed: int = 42, faults: list[str] | None = None) -> dict:
    faults = faults if faults is not None else ["P1", "P2", "P3", "P4"]
    pack = load_pack("dispatch")
    pack = load_pack("dispatch", load_params(pack))
    instance, _ = pack.generate(seed, faults)
    policy = build_policy(pack, instance)
    # 윈도우는 열린 SQLite 파일을 지우지 못하므로 임시 폴더 정리 실패는 무시한다
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "session.db"
        client = TestClient(create_app(db, serve_web=False, llm_factory=lambda: FakeLLM(policy)))
        ds = client.post("/domains/dispatch/datasets", json={"seed": seed, "faults": faults}).json()
        day = pack.items(instance)[0].split("-")[0]
        scope = [i for i in pack.items(instance) if i.startswith(f"{day}-")][:SCOPE_SIZE]
        full = client.post("/runs", json={"dataset_id": ds["id"], "agent": "rule"}).json()
        for level in LEVELS:
            res = client.post("/runs", json={"dataset_id": ds["id"], "agent": "ai", "level": level, "scope": scope})
            for run in res.json()["runs"]:
                _wait(client, f"/runs/{run['run_id']}")
        report = _wait(client, f"/analysis/{client.post('/analysis', json={'run_id': full['run_id']}).json()['id']}")
        batch = _wait(client, f"/proposals/batches/"
                              f"{client.post('/proposals', json={'report_id': report['id']}).json()['id']}")
        spec = next(p for p in batch["proposals"] if p["kind"] == "spec")
        client.post(f"/proposals/{spec['id']}/simulate", json={"confirm": True, "level": "L3", "scope": scope})
        _wait(client, f"/proposals/{spec['id']}")
        client.close()
        return export_bundle(db, out, git_ref="HEAD", note="리허설 (가짜 LLM, 수치는 AI 성능과 무관)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="API 키 없이 쓰는 리허설 시연 번들")
    parser.add_argument("--out", default=str(ROOT / "demo" / "rehearsal"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--faults", default="P1,P2,P3,P4")
    args = parser.parse_args(argv)
    manifest = build(args.out, args.seed, [f for f in args.faults.split(",") if f])
    print(json.dumps(manifest["counts"], ensure_ascii=False))
    print(f"\n시연 모드로 열기: python -m api.main --demo {args.out}")


if __name__ == "__main__":
    main()
