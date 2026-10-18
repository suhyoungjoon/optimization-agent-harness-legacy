"""도메인 팩 로딩. 코어는 도메인 이름을 문자열로만 다룬다.

규약: domains/<name>/pack.py 가 get_pack(params=None) 을 제공한다.
"""

import importlib
from pathlib import Path
from typing import Any

import yaml

from core.interfaces import DomainPack

DOMAINS_DIR = Path(__file__).resolve().parent.parent / "domains"

# 도메인 파일(params.yaml, domain-spec.md 등)을 읽고 쓸 위치를 바꾼다 (시연 번들의 작업 복사본).
# 설정하면 <root>/<도메인>/<파일>이 있을 때 그 파일을 쓴다.
_domain_files_root: Path | None = None


def set_domain_files_root(root: str | Path | None) -> None:
    global _domain_files_root
    _domain_files_root = Path(root) if root is not None else None


def domain_file(domain: str, filename: str, default_dir: str | Path) -> Path:
    if _domain_files_root is not None:
        candidate = _domain_files_root / domain / filename
        if candidate.is_file():
            return candidate
    return Path(default_dir) / filename


def list_domains() -> list[str]:
    return sorted(p.name for p in DOMAINS_DIR.iterdir() if (p / "pack.py").is_file())


def load_pack(domain: str, params: dict | None = None) -> DomainPack:
    if domain not in list_domains():
        raise KeyError(f"unknown domain: {domain}")
    module = importlib.import_module(f"domains.{domain}.pack")
    return module.get_pack(params)


def load_params(pack: DomainPack) -> dict:
    return yaml.safe_load(Path(pack.params_path()).read_text(encoding="utf-8"))


def load_faults(pack: DomainPack) -> dict:
    """faults.yaml 전체 (정답 포함). 채점과 사람용 화면에서만 쓰고 분석 agent에는 주지 않는다."""
    path = Path(pack.params_path()).parent / "faults.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_perspectives(pack: DomainPack) -> list[dict]:
    """analysis_perspectives.yaml의 관점 목록 (관점별 분석, core.analysis.perspectives). 파일이 없으면 빈 목록."""
    path = Path(pack.params_path()).parent / "analysis_perspectives.yaml"
    if not path.is_file():
        return []
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("perspectives") or []


def list_faults(pack: DomainPack) -> list[dict]:
    """심을 수 있는 결함의 ID와 이름. 정답(answer)은 노출하지 않는다."""
    return [{"id": fid, "name": spec.get("name", fid)} for fid, spec in load_faults(pack).items()]


def build_instance(pack: DomainPack, dataset: dict) -> tuple[Any, dict]:
    """데이터셋의 인스턴스와 정답표. 원천 파일이 있으면 pack.load, 없으면 pack.generate(seed, faults)."""
    if dataset.get("source"):
        return pack.load(dataset["source"])
    return pack.generate(dataset["seed"], dataset["faults"])
