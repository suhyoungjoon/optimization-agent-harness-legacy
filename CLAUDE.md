# optimization-agent-harness

규칙 기반 최적화 agent를 기준선으로 두고, 같은 문제를 푸는 LLM agent를 하네스 엔지니어링 레벨(L0~L5)별로 구현·비교한 뒤,
결과 분석 agent와 개선 루프(제안 → 시뮬레이션 → 사람 승인)까지 수행하는 범용 프레임워크.
첫 도메인은 출동 스케줄링(dispatch)이며, 가상 데이터만 사용한다. 전체 기획은 `docs/plan.md` 참고.
이 레포는 레가시 I/F를 가상 데이터로 반영하는 파생 레포이며, 기준 문서는 `docs/legacy-if-impact.md`다.

## 핵심 원칙 (반드시 지킬 것)

1. **코어는 도메인을 모른다.** `core/` 안에 작업자, 지시서, 지점 같은 dispatch 용어가 나오면 안 된다. 도메인 접근은 `core/interfaces.py`의 `DomainPack`을 통해서만 한다.
2. **규칙·파라미터·프롬프트는 파일로 분리한다.** 가중치, 매칭 범위, 기준값을 코드에 하드코딩하지 말고 `params.yaml`, `domain-spec.md`에서 읽는다.
3. **필수조건 판정은 항상 `DomainPack.validate()`가 한다.** LLM의 자체 판단으로 위반 여부를 결정하지 않는다.
4. **모든 실행은 재현 가능해야 한다.** seed, 데이터셋 ID, 하네스 레벨, 모델명, params 버전을 run 메타데이터로 저장한다. 분석·개선 에이전트가 이전 회차의 기억(사람의 판단)을 읽으면 쓴 기억(항목과 해시)도 리포트·개선안 묶음에 저장한다.
5. **하네스 레벨은 코드 분기가 아니라 `configs/harness_levels.yaml` 플래그로 제어한다.**
6. **처음부터 과하게 범용화하지 않는다.** 현재 마일스톤에 필요한 것만 만든다. 두 번째 도메인이 요구할 때 인터페이스를 넓힌다.

## 인터페이스 변경 규칙

`core/interfaces.py`는 두 개발자가 병렬 작업하기 위한 계약이다.
- 필드 **추가**는 가능하되, 변경 사실을 작업 요약에 명시한다.
- 필드 **이름 변경·삭제·타입 변경**은 하지 말고, 필요하면 먼저 제안하고 확인을 받는다.

## 디렉터리 구조

```
core/            공통 코어 (interfaces, harness, llm, evaluation, analysis, improvement, storage)
domains/dispatch 출동 스케줄링 도메인 팩 (generator, rule_engine, tools, metrics, *.yaml, domain-spec.md)
configs/         harness_levels.yaml
api/             FastAPI
web/             React + Vite 프런트엔드
scripts/         데이터 생성, 일괄 실행 CLI
tests/           pytest
docs/            plan.md(기획서), ai-application-boundary.md(AI 적용 경계), domain-pack-guide.md
runs/            실행 결과 (git 제외)
```

## 하네스 레벨

| 레벨 | 추가 계층 | 동작 |
|---|---|---|
| L0 | 없음 | 인스턴스 데이터를 통째로 프롬프트에 넣고 결정 요청 |
| L1 | 컨텍스트 | domain-spec.md를 시스템 프롬프트에 주입 |
| L2 | 도구 | 데이터는 도구로만 조회 |
| L3 | 검증 루프 | validate() 위반 시 사유를 돌려주고 재시도 (max_retries) |
| L4 | 가드레일 | 위반 차단, 승인 조건은 pending_approval |
| L5 | 관측성 | 모든 LLM·도구·검증 단계를 TraceRecord로 기록 |

L0~L4에서도 최종 결정에 대한 validate()는 사후 채점용으로 항상 실행하되, 결과에는 반영하지 않는다.

## 기술 스택

Python 3.11+, FastAPI, Pydantic, SQLite, PyYAML, pytest / React + Vite + TypeScript, Recharts / LLM은 상용 API tool use (`core/llm/client.py`에서만 호출).

## 개발 규칙

- API 키는 `.env`에서 읽는다. `.env`, `runs/`는 커밋하지 않는다.
- LLM 호출은 `core/llm/client.py`만 사용한다. 개발 중에는 입력 해시 기반 캐시를 켠다.
- LLM이 없는 부분(generator, rule_engine, validate, 집계, 채점)은 테스트를 먼저 작성한다.
- LLM이 들어가는 부분은 mock 응답으로 흐름(재시도, 차단, 승인 대기)을 테스트한다.
- 새 도메인 팩은 `tests/test_domain_contract.py`의 계약 테스트를 통과해야 한다.
- 커밋은 작은 단위로, 메시지는 `[M1] core: add DecisionRecord` 형식.

## 명령어

```bash
pip install -e ".[dev]"          # 설치
pytest                           # 테스트
python -m scripts.generate --domain dispatch --seed 42 --faults P1,P2   # 데이터 생성
(cd web && npm ci && npm run build)   # 프런트엔드 빌드 (web/dist)
python -m api.main               # API + 빌드된 프런트엔드 서빙 (http://127.0.0.1:8000)
(cd web && npm run dev)          # 프런트엔드 개발 서버 (API는 :8000으로 프록시)
python -m scripts.snapshot export --out demo/bundle --note "..."   # 시연 번들 (runs/harness.db + 커밋된 도메인 파일)
python -m api.main --demo demo/bundle   # 시연 모드: 저장된 AI 결과만 재생, LLM·네트워크 없음
python -m scripts.rehearsal_bundle      # API 키 없이 쓰는 리허설 번들 (가짜 LLM) → demo/rehearsal
node scripts/record_demo.mjs --stills   # 시연 장면 녹화 (시연 모드 서버에 대해, runs/recordings)
python -m scripts.legacy export --seed 42 --faults P1,P4 --name mydata   # 가상 데이터를 레가시 형식(I/F별 CSV)으로 → data/legacy/mydata
python -m scripts.legacy load mydata    # 레가시 형식 폴더를 데이터셋(dispatch-src-mydata)으로 등록. API는 POST /domains/dispatch/datasets {"source": "mydata"}
```
AI agent 실행에는 API 키가 필요하다: 환경변수 또는 `.env`의 `OAH_ANTHROPIC_API_KEY`(우선) 또는 `ANTHROPIC_API_KEY`. 모델·effort·캐시는 `configs/llm.yaml`(배정은 기본값, 분석·개선안은 `roles`의 역할별 모델), 최종 측정 때는 `LLM_CACHE=0`으로 입력 해시 캐시를 끈다. 실험할 때만 모델을 바꾸려면 `LLM_MODEL`, `LLM_THINKING`(adaptive | off), `LLM_EFFORT`(low~max | none)로 덮어쓴다 (쓴 모델은 사용량 기록 `usage.model`에 남는다).
(명령어가 바뀌면 이 섹션을 갱신한다.)

## 작업 방식

- 한 번에 하나의 마일스톤(`docs/plan.md` 부록 A10)만 진행한다. 다음 마일스톤 기능을 미리 만들지 않는다.
- 작업 시작 전 계획을 제시하고, 끝나면 만든 파일·테스트 결과·남은 이슈를 요약한다.
- 기획서와 다르게 구현해야 할 이유가 생기면 임의로 바꾸지 말고 먼저 알린다.
