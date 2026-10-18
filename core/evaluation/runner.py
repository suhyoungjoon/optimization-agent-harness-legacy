"""실행 러너. 규칙 agent와 AI agent(하네스 레벨별)를 같은 데이터셋·같은 범위로 실행하고 저장한다.

모든 run은 seed, 데이터셋 ID, 레벨, 모델명, params 버전, 범위(scope)를 메타데이터로 남긴다.
"""

import hashlib
import time
from collections.abc import Callable

from core.harness.levels import get_level
from core.harness.runner import HarnessRunner
from core.interfaces import DomainPack
from core.llm.client import LLMClient
from core.registry import build_instance, load_params
from core.storage.store import Store


def _instance(pack: DomainPack, dataset: dict, scope: list[str] | None):
    instance, _truth = build_instance(pack, dataset)
    return pack.subset(instance, scope) if scope is not None else instance


def run_rule_agent(store: Store, pack: DomainPack, dataset: dict, params: dict,
                   scope: list[str] | None = None, group_id: str | None = None) -> str:
    """데이터셋을 재생성해 규칙 agent로 풀고, 사후 검증·지표와 함께 저장한다."""
    run_id = store.create_run(domain=pack.name, dataset=dataset, agent="rule", params=params,
                              group_id=group_id, scope=scope)
    try:
        instance = _instance(pack, dataset, scope)
        started = time.time()
        decisions = pack.solve(instance, params)
        seconds = time.time() - started
        violations = pack.validate(instance, decisions)
        metrics = pack.metrics(instance, decisions)
    except Exception as exc:  # 실행 실패도 기록으로 남긴다
        store.fail_run(run_id, repr(exc))
        raise
    meta = {"items": len(decisions), "seconds": seconds, "usage": {"calls": 0, "cost_usd": 0.0}}
    store.finish_run(run_id, decisions, metrics, violations, meta)
    return run_id


def run_ai_agent(store: Store, pack: DomainPack, dataset: dict, level_name: str, llm: LLMClient,
                 llm_config: dict, scope: list[str] | None = None, repeat: int = 0,
                 group_id: str | None = None, progress: Callable[[int, int], None] | None = None,
                 run_id: str | None = None, spec_text: str | None = None) -> str:
    """AI agent를 하네스 레벨 하나로 실행한다. 사후 검증은 레벨과 무관하게 항상 한다."""
    level = get_level(level_name)
    params = load_params(pack)
    run_id = run_id or create_ai_run(store, pack, dataset, level_name, llm, scope, repeat, group_id)
    try:
        instance = _instance(pack, dataset, scope)
        runner = HarnessRunner(pack, llm, level, llm_config.get("max_llm_calls_per_item", 12), spec_text=spec_text)
        spec_salt = f":spec{hashlib.sha1(spec_text.encode()).hexdigest()[:10]}" if spec_text is not None else ""
        out = runner.run(instance, run_id=run_id, salt=f"{dataset['id']}:{level_name}:{repeat}{spec_salt}",
                         progress=progress)
        violations = pack.validate(instance, out.decisions)
        metrics = pack.metrics(instance, out.decisions)
    except Exception as exc:
        store.fail_run(run_id, repr(exc))
        raise
    store.save_traces(out.traces)
    meta = {"items": len(out.decisions), "seconds": out.seconds,
            "usage": out.usage.to_dict(llm.model, llm_config),
            "effort": llm_config.get("effort"), "cache": bool(llm_config.get("cache"))}
    store.finish_run(run_id, out.decisions, metrics, violations, meta)
    return run_id


def create_ai_run(store: Store, pack: DomainPack, dataset: dict, level_name: str, llm: LLMClient,
                  scope: list[str] | None, repeat: int, group_id: str | None) -> str:
    """실행 전에 run 레코드를 먼저 만든다 (진행 상황을 run_id로 조회할 수 있게)."""
    get_level(level_name)  # 알 수 없는 레벨이면 여기서 실패
    return store.create_run(domain=pack.name, dataset=dataset, agent="ai", params=load_params(pack),
                            level=level_name, model=llm.model, group_id=group_id, repeat=repeat, scope=scope)
