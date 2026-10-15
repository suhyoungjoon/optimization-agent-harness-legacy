"""dispatch 설정 파일 중 도메인 고유 규칙 테스트 (공통 계약은 test_domain_contract.py)."""

from pathlib import Path

import yaml

DISPATCH_DIR = Path(__file__).resolve().parent.parent / "domains" / "dispatch"


def load(filename: str) -> dict:
    return yaml.safe_load((DISPATCH_DIR / filename).read_text(encoding="utf-8"))


def test_matching_has_three_stages_relaxing_monotonically():
    matching = load("params.yaml")["matching"]
    for key in ("time_window_min", "area_extension_km"):
        stages = matching[key]
        assert len(stages) == 3, f"{key}는 3단계여야 한다"
        assert stages == sorted(stages), f"{key}는 단계가 올라갈수록 완화되어야 한다"


def test_faults_p1_to_p5():
    assert list(load("faults.yaml")) == ["P1", "P2", "P3", "P4", "P5"]


def test_approval_stage_within_matching_stages():
    params = load("params.yaml")
    assert 1 <= params["approval_required"]["matching_stage_gte"] <= len(params["matching"]["time_window_min"])


def test_difficulty_dimension_matches_weights():
    dim_values = load("dimensions.yaml")["dimensions"]["difficulty"]["values"]
    weight_keys = load("params.yaml")["weights"]["difficulty"]
    assert set(dim_values) == set(weight_keys)
