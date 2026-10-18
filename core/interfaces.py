"""코어와 도메인 팩 사이의 계약.

두 개발자가 병렬 작업하기 위한 계약이므로 필드 추가만 허용한다.
이름 변경·삭제·타입 변경은 먼저 제안하고 합의한다 (CLAUDE.md 참고).
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass
class DecisionRecord:            # 표준 결과 레코드
    item_id: str                 # 도메인 항목 ID
    decision: dict[str, Any] | None   # 도메인별 결정 내용, 미결정이면 None
    status: Literal["success", "failed", "blocked", "pending_approval"]
    reason_code: str | None      # dimensions.yaml에 선언된 사유 코드
    evidence: str                # 판단 근거 (사람이 읽는 문장)
    dims: dict[str, str]         # 분석 차원 값
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass
class TraceRecord:               # 표준 트레이스 레코드
    run_id: str
    item_id: str
    step: int
    kind: Literal["llm", "tool_call", "validate", "retry", "guardrail", "approval"]
    input: Any
    output: Any
    ts: float


@dataclass
class Violation:
    item_id: str
    rule: str                    # dimensions.yaml의 violation_rules에 선언된 규칙 ID
    message: str


@dataclass
class ToolContext:              # [M3 추가] 도구 handler가 받는 실행 맥락
    item_id: str                 # 지금 결정 중인 항목
    decisions: list[DecisionRecord]   # 지금까지 배정된 결정 (success, pending_approval)


class DomainPack(Protocol):
    name: str

    def generate(self, seed: int, faults: list[str]) -> tuple[Any, dict]: ...
    # [4-a 추가] 원천 파일(도메인이 정한 형식의 폴더 이름)에서 인스턴스와 정답표를 읽는다. 정답표가 없으면 빈 dict
    def load(self, source: str) -> tuple[Any, dict]: ...
    def items(self, instance: Any) -> list[str]: ...          # 처리 순서대로
    def solve(self, instance: Any, params: dict) -> list[DecisionRecord]: ...
    def validate(self, instance: Any, decisions: list[DecisionRecord]) -> list[Violation]: ...
    def metrics(self, instance: Any, decisions: list[DecisionRecord]) -> dict[str, float]: ...
    # 각 항목: {"name", "description", "input_schema", "handler": fn(args: dict, ctx: ToolContext) -> Any}
    # handler는 [M3 추가]. 코어는 LLM에 보낼 때 handler를 뺀다.
    def tools(self, instance: Any) -> list[dict]: ...
    def spec_path(self) -> str: ...
    def params_path(self) -> str: ...
    def dimensions(self) -> dict: ...

    # --- [M3 추가] AI agent 하네스용 ---
    def decision_schema(self) -> dict: ...                     # DecisionRecord.decision의 JSON Schema
    def item_dims(self, instance: Any, item_id: str) -> dict[str, str]: ...   # 항목의 분석 차원 값
    def approval_reasons(self, instance: Any, record: DecisionRecord,
                         decisions: list[DecisionRecord]) -> list[str]: ...    # 비면 승인 불필요
    def subset(self, instance: Any, item_ids: list[str]) -> Any: ...          # 항목 일부만 남긴 인스턴스

    # --- [M4 추가] 분석 agent용 ---
    # 차원 집계로 드러나지 않는 도메인 고유 통계 (예: 자원별 활용률).
    # 각 항목: {"name", "description", "input_schema", "handler": fn(args: dict) -> Any}
    def analysis_tools(self, instance: Any, decisions: list[DecisionRecord]) -> list[dict]: ...
