"""파라미터 탐색 (M13-B): sweep_params, 1차원 민감도, 조합 상한, 분류 표시, API 캐시. 규칙 엔진만 쓴다 (AI 비용 0)."""

import pytest

from core.improvement.sweep import load_sweep_config, sensitivity, sweep_params
from domains.dispatch.generator import generate
from domains.dispatch.pack import get_pack


@pytest.fixture(scope="module")
def setup():
    pack = get_pack()
    inst, _ = generate(42, ["P1", "P2", "P3", "P4"])
    return (lambda p: get_pack(p)), inst, pack.params


def count(point, metric):
    return round(point["metrics"][metric] * point["items"])


def test_one_axis_reproduces_area_extension_numbers(setup):
    factory, inst, params = setup
    out = sweep_params(factory, inst, params, [{"path": "matching.area_extension_km[2]", "values": [3, 4, 5]}])
    assert [p["values"]["matching.area_extension_km[2]"] for p in out["points"]] == [3, 4, 5]
    by = {p["values"]["matching.area_extension_km[2]"]: p for p in out["points"]}
    assert count(by[3], "assignment_rate") == 1357 and count(by[3], "on_time_rate") == 961
    assert abs(count(by[4], "assignment_rate") - 1525) <= 1 and abs(count(by[4], "on_time_rate") - 821) <= 1
    assert by[4]["metrics"] == by[5]["metrics"]                       # 4km 이상은 같다
    assert all(p["violations"] == 0 for p in out["points"])
    assert out["axes"][0]["kind"] == "policy" and not out["warnings"]
    assert out["base"]["values"] == {"matching.area_extension_km[2]": 3}


def test_two_axes_make_a_grid(setup):
    factory, inst, params = setup
    out = sweep_params(factory, inst, params, [{"path": "matching.area_extension_km[2]", "values": [3, 4]},
                                               {"path": "matching.time_window_min[2]", "values": [60, 120]}])
    assert len(out["points"]) == 4
    assert {tuple(sorted(p["values"].items())) for p in out["points"]} == {
        (("matching.area_extension_km[2]", a), ("matching.time_window_min[2]", t)) for a in (3, 4) for t in (60, 120)}
    # 두 축이 실제로 적용됐는지: 조합마다 결과가 다르다
    got = {(p["values"]["matching.area_extension_km[2]"], p["values"]["matching.time_window_min[2]"]):
           (count(p, "assignment_rate"), count(p, "on_time_rate")) for p in out["points"]}
    assert got[(3, 120)] == (1357, 961) and abs(got[(4, 120)][0] - 1525) <= 1
    assert len(set(got.values())) == 4


def test_estimate_axis_is_allowed_but_marked(setup):
    """추정값 탐색은 허용하되 표시한다: 지표가 좋아져도 '가정만 바꾼 가짜 개선'이다."""
    factory, inst, params = setup
    out = sweep_params(factory, inst, params, [{"path": "duration.base_min", "values": [
        {"install": 60, "repair": 45}, {"install": 45, "repair": 45}]}])
    assert out["axes"][0]["kind"] == "estimate" and out["warnings"]
    fast = out["points"][1]
    assert abs(count(fast, "assignment_rate") - 1402) <= 1 and abs(count(fast, "on_time_rate") - 1074) <= 1
    assert fast["violations"] == 0                                     # validate()도 같은 추정값으로 판정해서 막지 못한다


@pytest.mark.parametrize("axes, word", [
    ([], "축"),
    ([{"path": "a.b", "values": [1]}] * 3, "축"),
    ([{"path": "matching.area_extension_km[2]", "values": []}], "values"),
    ([{"path": "matching.area_extension_km[2]", "values": [9]}], "허용 범위"),
    ([{"path": "matching.area_extension_km[2]", "values": list(range(6))},
      {"path": "matching.time_window_min[2]", "values": list(range(0, 120, 10))}], "상한"),
    ([{"path": "duration.bounds", "values": [1]}], "바꿀 수 없는 경로"),
    ([{"path": "matching.area_extension_km[2]", "values": ["4"]}], "수치"),                 # 모양이 다른 값
    ([{"path": "matching.area_extension_km", "values": [[0, 1]]}], "길이 3"),
    ([{"path": "duration.base_min", "values": [{"install": 45}]}], "키가"),
    ([{"path": "matching.area_extension_km[2]", "values": [3]}] * 2, "같은 파라미터"),       # 같은 축 두 번
])
def test_axis_errors(setup, axes, word):
    factory, inst, params = setup
    with pytest.raises(ValueError, match=word):
        sweep_params(factory, inst, params, axes, max_points=50)


def test_limit_comes_from_config():
    cfg = load_sweep_config()
    assert cfg["max_points"] == 50 and cfg["sensitivity_steps"] >= 3


def test_sensitivity_shows_flat_knobs(setup):
    factory, inst, params = setup
    out = sensitivity(factory, inst, params, steps=4)
    rows = {r["path"]: r for r in out["rows"]}
    assert set(rows) == {f"matching.{k}[{i}]" for k in ("time_window_min", "area_extension_km") for i in range(3)} | {
        "cei.master_threshold"}                                         # policy만
    # 2단계 관할은 3단계 값(3km) 이하에서는 결과가 같다 (의미 없는 손잡이). 3단계보다 넓히면 그때부터 달라진다
    stage2 = rows["matching.area_extension_km[1]"]
    within = [pt["metrics"] for pt in stage2["points"] if pt["values"]["matching.area_extension_km[1]"] <= 3]
    assert len(within) >= 2 and all(m == within[0] for m in within)
    assert stage2["flat_around_current"] == [0, 3]                                          # 화면: "0~3 변화 없음"
    # 이 데이터에서는 관할 확장이 어느 단계든 0~3km에서 효과가 없고 4km부터 달라진다 (경계 지역 고객이 3km 밖에 있음)
    stage3 = rows["matching.area_extension_km[2]"]
    assert stage3["flat_around_current"] == [0, 3] and stage3["ranges"]["assignment_rate"]["spread"] > 0
    assert rows["matching.area_extension_km[2]"]["ranges"]["assignment_rate"]["spread"] > 0
    r = rows["matching.area_extension_km[2]"]
    assert r["values"] == [0, 1, 2, 3, 4, 5] and r["current"] == 3 and len(r["points"]) == len(r["values"])   # 좁은 정수 범위는 전부


def test_sweep_api_caches_by_dataset_version_and_axes(tmp_path):
    from fastapi.testclient import TestClient

    from api.main import create_app

    client = TestClient(create_app(tmp_path / "h.db", serve_web=False))
    ds = client.post("/domains/dispatch/datasets", json={"seed": 42, "faults": ["P1", "P2", "P3", "P4"]}).json()
    body = {"dataset_id": ds["id"], "axes": [{"path": "matching.area_extension_km[2]", "values": [3, 4]}],
            "metrics": ["assignment_rate", "on_time_rate"]}
    first = client.post("/params/sweep", json=body).json()
    assert first["cached"] is False and len(first["points"]) == 2 and first["params_version"] == 2
    assert set(first["points"][0]["metrics"]) == {"assignment_rate", "on_time_rate"}
    assert client.post("/params/sweep", json=body).json()["cached"] is True
    bad = client.post("/params/sweep", json={**body, "axes": [{"path": "matching.area_extension_km[2]", "values": [9]}]})
    assert bad.status_code == 400 and "허용 범위" in bad.json()["detail"]
    assert client.post("/params/sweep", json={**body, "dataset_id": "nope"}).status_code == 404
    sens = client.get("/params/sensitivity", params={"dataset_id": ds["id"]}).json()
    assert sens["rows"] and sens["cached"] is False


def test_sweep_cache_is_bounded(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import api.improvement
    from api.main import create_app

    monkeypatch.setattr(api.improvement, "SWEEP_CACHE_MAX", 1)
    client = TestClient(create_app(tmp_path / "h.db", serve_web=False))
    ds = client.post("/domains/dispatch/datasets", json={"seed": 42, "faults": ["P4"]}).json()
    a = {"dataset_id": ds["id"], "axes": [{"path": "matching.area_extension_km[2]", "values": [3]}]}
    b = {"dataset_id": ds["id"], "axes": [{"path": "matching.area_extension_km[2]", "values": [4]}]}
    client.post("/params/sweep", json=a)
    client.post("/params/sweep", json=b)                                  # a는 밀려난다
    assert client.post("/params/sweep", json=a).json()["cached"] is False
