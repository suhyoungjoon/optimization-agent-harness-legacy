// core/interfaces.py 표준 레코드와 API 응답 형태

export interface DecisionRecord {
  item_id: string;
  decision: Record<string, unknown> | null;
  status: "success" | "failed" | "blocked" | "pending_approval";
  reason_code: string | null;
  evidence: string;
  dims: Record<string, string>;
  metrics: Record<string, number>;
}

export interface Violation {
  item_id: string;
  rule: string;
  message: string;
}

export interface TraceRecord {
  run_id: string;
  item_id: string;
  step: number;
  kind: "llm" | "tool_call" | "validate" | "retry" | "guardrail" | "approval";
  input: unknown;
  output: unknown;
  ts: number;
}

export interface Dimensions {
  dimensions: Record<string, { label: string; values?: string[]; format?: string }>;
  // 자원 단위 발견 (M12-d): 발견의 resources에 적을 수 있는 자원 종류
  resources?: Record<string, { label: string; id_field: string; traits?: Record<string, { label: string }> }>;
  reason_codes: Record<string, string>;
  violation_rules: Record<string, string>;
}

export interface DomainFault {
  id: string;
  name: string;
  expected?: string;
  generation?: Record<string, unknown>; // answers=true일 때만
  answer?: Record<string, unknown>;     // answers=true일 때만
}

export interface DomainDefinition {
  domain: string;
  params: Record<string, unknown>;
  spec_sections: Record<string, string>;
  dimensions: Dimensions;
  core_reason_codes: Record<string, string>;
  faults: DomainFault[];
  tools: { name: string; description: string }[];   // AI agent 조회 도구 (L2부터 제공)
  files: Record<string, string>;
}

export interface DomainInfo {
  name: string;
  dimensions: Dimensions;
  faults: { id: string; name: string }[];
  core_reason_codes: Record<string, string>;
}

export interface Dataset {
  id: string;
  domain: string;
  seed: number;
  faults: string[];
  items: number;
  source?: string | null;   // 원천 파일 이름 (레가시 형식 폴더). 없으면 생성기(seed, faults)
  instance?: unknown;
  item_ids?: string[];
}

export interface Usage {
  calls?: number;
  cached_calls?: number;
  input_tokens?: number;
  output_tokens?: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
  cost_usd: number | null;
}

export interface RunProgress {
  done?: number;
  total?: number;
  state?: string;
  error?: string;
}

export interface Run {
  run_id: string;
  domain: string;
  dataset_id: string;
  agent: "rule" | "ai";
  level: string | null;
  model: string | null;
  seed: number;
  params_version: number | null;
  status: "running" | "done" | "error";
  metrics: Record<string, number> | null;
  violations: Violation[] | null;
  group_id?: string | null;
  repeat?: number | null;
  scope?: string[] | null;
  meta?: { items?: number; seconds?: number; usage?: Usage; error?: string; effort?: string } | null;
  progress?: RunProgress | null;
  replayed?: boolean;   // 저장된 실행을 재생 중 (시연 모드)
}

export interface HarnessLevel {
  name: string;
  spec: boolean;
  tools: boolean;
  validate_loop: boolean;
  guardrail: boolean;
  trace: "minimal" | "full";
  max_retries: number;
}

export interface HarnessInfo {
  levels: Record<string, HarnessLevel>;
  llm: { model: string; effort: string; cache: boolean; concurrency: number; max_llm_calls_per_item: number };
  demo?: DemoManifest | null;   // 시연 모드일 때 번들 정보
}

export interface DemoManifest {
  created_at: string;
  git_ref: string | null;
  git_commit: string | null;
  note: string;
  counts: Record<string, number>;
}

export interface DemoCatalogEntry {
  dataset_id: string;
  level: string;
  items: number;
  runs: number;
}

export interface CompareSummary {
  agent: "rule" | "ai";
  level: string | null;
  runs: number;
  metrics: Record<string, number | null>;
  violations: number | null;
  seconds_per_item: number | null;
  cost_per_item_usd: number | null;
  consistency: number | null;
}

export interface CompareResult {
  runs: unknown[];
  summary: CompareSummary[];
}

// --- M4: 분석·개선 ---

export interface Finding {
  id: string;
  title: string;
  description: string;
  slice?: Record<string, string[]>;
  reason_codes?: string[];
  metric?: { name: string; direction: "low" | "high" };
  hypothesis?: string;
  cited_calls: string[];
  // 관점별 분석 (M12-b): 이 발견을 찾은 관점과, 같은 발견에 대한 다른 관점의 해석
  perspectives?: string[];
  perspective_names?: string[];
  alternatives?: FindingAlternative[];
  related?: string[];   // 구간이 겹치는(한쪽이 더 좁은) 다른 발견 ID
  slice_removed?: Record<string, string[]>;   // 선언되지 않은 차원·값이라 구간에서 뺀 것
  resources?: FindingResources;               // 자원 단위 발견 (M12-d)
  resources_removed?: { kind?: string; ids?: string[]; traits?: Record<string, string[]> };   // 인용 결과에 없어 뺀 것
}

export interface FindingResources {
  kind: string;
  ids: string[];
  traits?: Record<string, string[]>;
}

export interface FindingAlternative {
  perspective: string;
  perspective_name: string;
  title: string;
  description: string;
  hypothesis?: string | null;
  cited_calls: string[];
}

export interface PerspectiveResult {
  name: string;
  error: string | null;
  findings: number;
  dropped: number;
  stop: string | null;
  usage?: Usage & { llm_calls?: number; seconds?: number };
}

export interface ToolCall {
  name: string;
  input: unknown;
  output: unknown;
  is_error: boolean;
  perspective?: string;
}

export interface FaultScore {
  name: string;
  detected: boolean;
  matched_findings: string[];
  status?: "detected" | "pending" | "missed";   // pending: 근거는 맞고 사람의 원인 확인 대기 (M12-a 이전 채점엔 없음)
  confirm_cause?: boolean;
}

// 발견 판정: 정답에 없는 발견은 valid/false_positive, 원인 확인 대상 발견은 cause_ok/cause_wrong
export type FindingLabel = "valid" | "false_positive" | "cause_ok" | "cause_wrong";

export interface Score {
  faults: Record<string, FaultScore>;
  detected: number;
  total: number;
  detection_rate: number | null;
  unmatched_findings: string[];
  pending?: number;
  labels: Record<string, FindingLabel>;
  false_positives: number;
  valid_unmatched: number;
  unlabeled: number;
}

// 회차 간 장기 기억 (M12-c): 사람이 내린 판단을 다음 회차 에이전트 입력으로
export interface MemoryItem {
  id: string;
  title: string;
  at?: number;
  params_version?: number | null;
  stale?: boolean;
  // 반려 개선안
  kind?: "params" | "spec";
  change?: string;
  reason?: string;
  // 발견 판정
  label?: FindingLabel;
  hypothesis?: string | null;
}

export interface Memory {
  rejections: MemoryItem[];
  judgments: MemoryItem[];
  item_ids: string[];
  version: string | null;
}

export interface Report {
  id: string;
  run_id: string;
  status: "running" | "done" | "error";
  error?: string | null;
  body: {
    summary: string;
    findings: Finding[];
    dropped: { finding: Finding; problems: string[] }[];
    calls: Record<string, ToolCall>;
    stop: string;
    feedback_rounds: number;
    usage: Usage & { llm_calls?: number; seconds?: number };
    memory?: Memory | null;
    perspectives?: Record<string, PerspectiveResult>;
  } | null;
  score: Score | null;
}

export interface ProposalBody {
  title: string;
  kind: "params" | "spec";
  rationale: string;
  expected_effect?: string;
  target_findings: string[];
  params_changes?: { path: string; value: unknown }[];
  override_rules?: { when: Record<string, string[]>; set: Record<string, unknown> }[];
  spec_edits?: { section: string; text: string }[];
}

export interface Simulation {
  kind?: "params" | "spec";
  replayed?: boolean;
  before?: Record<string, number>;
  after?: Record<string, number>;
  violations_after?: number;
  violations_before?: number;
  slices?: Record<string, { before: { items: number; failed: number; fail_rate: number }; after: { items: number; failed: number; fail_rate: number } }>;
  seconds?: number;
  cost_usd?: number;
  run_ids?: Record<string, string>;
  error?: string;
  constraint_violations?: { index: number; type: "param" | "metric"; message: string }[];
}

// 사람이 정한 한도 (M12-a). 입력 화면은 이 레포에 없고 API로 받는다
export interface Constraint {
  type: "param" | "metric";
  path?: string;
  metric?: string;
  min?: number;
  max?: number;
  max_drop?: number;
  max_rise?: number;
  source: string;
  note?: string;
}

export interface Proposal {
  id: string;
  batch_id: string;
  report_id: string;
  kind: "params" | "spec";
  status: "proposed" | "invalid" | "simulating" | "simulated" | "approved" | "rejected" | "stale";
  body: ProposalBody;
  errors: string[];
  simulation: Simulation | null;
  decision: { action: string; note: string; params_version_before?: number; params_version_after?: number; forced?: boolean } | null;
}

export interface ProposalBatch {
  id: string;
  report_id: string;
  status: "running" | "done" | "error";
  error?: string | null;
  meta: { usage: Usage & { seconds?: number }; trials: number; params_version?: number; constraints?: Constraint[]; memory?: Memory | null } | null;
  proposals: Proposal[];
}

export interface SpecEstimate {
  level: string;
  items: number;
  runs: number;
  estimate_usd: number | null;
  per_item_usd?: number;
  note?: string;
  replayed?: boolean;
}

export interface HistoryRow {
  proposal_id: string;
  round: number | null;
  status: "approved" | "rejected";
  kind: "params" | "spec";
  title: string;
  target_findings: string[];
  decision: Proposal["decision"];
  before: Record<string, number> | null;
  after: Record<string, number> | null;
  cycle: { analysis_seconds?: number; proposal_seconds?: number; simulation_seconds?: number; llm_cost_usd: number | null };
}

// --- M13: 파라미터 가시화 ---
export type ParamKind = "policy" | "estimate" | "fixed" | "governance";

export interface SweepAxis {
  path: string;
  values: unknown[];
}

export interface SweepPoint {
  values: Record<string, unknown>;
  metrics: Record<string, number>;
  violations: number;
  items: number;
}

export interface SweepResult {
  axes: (SweepAxis & { kind: ParamKind })[];
  points: SweepPoint[];
  base: SweepPoint;
  warnings: string[];
  seconds: number;
  cached: boolean;
  params_version: number;
}

export interface SensitivityRow {
  path: string;
  kind: ParamKind;
  bounds: [number, number];
  current: number;
  values: number[];
  points: SweepPoint[];
  ranges: Record<string, { min: number; max: number; spread: number }>;
  flat_around_current: [number, number] | null;   // 현재 값을 포함해 결과가 같은 구간
  items: number;
}

export interface Sensitivity {
  rows: SensitivityRow[];
  metrics: string[];
  steps: number;
  seconds: number;
  cached: boolean;
}

// 변경 이력 카드: 승인으로 params 버전이 오를 때마다 하나 (before가 null이면 M13 이전 기록이라 "기록 없음")
export interface ChangeCard {
  proposal_id: string;
  report_id: string | null;
  title: string | null;
  version_before: number | null;
  version_after: number;
  changes: { path: string; before: unknown; after: unknown }[];
  override_rules: { when: Record<string, unknown>; set: Record<string, unknown>; before: Record<string, unknown> | null }[];
  findings: { id: string; title: string | null }[];
  metrics_before: Record<string, number> | null;
  metrics_after: Record<string, number> | null;
  violations_after: number | null;
  approver: string | null;
  note: string | null;
  forced: boolean;
  at: number | null;
}

// --- Agent workflow (LangGraph, M9) ---
export interface WorkflowNode {
  id: string;
  label?: string;
  kind?: "rule" | "ai" | "human";
  tab?: string;
  description?: string;
}

export interface WorkflowEdge {
  source: string;
  target: string;
  label: string | null;
  conditional: boolean;
}

export interface WorkflowGraph {
  available: boolean;
  reason?: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
}

export interface WorkflowStep {
  node: string;
  lines: string[];
  data: Record<string, unknown>;
  at: number;
}

export interface WorkflowRun {
  id: string;
  status: "paused" | "running" | "done";
  current: string | null;
  error: string | null;
  next: string[];
  waiting: "next" | "approval" | null;
  approval: { proposal_id: string; question: string } | null;
  steps: WorkflowStep[];
  ids: Partial<Record<"dataset_id" | "rule_run_id" | "ai_run_id" | "full_rule_run_id" | "report_id" | "batch_id" | "proposal_id", string>>;
  inputs: { domain?: string; seed?: number; faults?: string[]; items?: number; level?: string; scope?: string[] };
}

// --- LangGraph agents (M10) ---
export interface AgentsGraph {
  available: boolean;
  reason?: string;
  demo?: boolean;
  level?: string;
  perspectives?: boolean;
  top: { nodes: (WorkflowNode & { agent?: boolean })[]; edges: WorkflowEdge[] };
  agents: Record<string, { nodes: WorkflowNode[]; edges: WorkflowEdge[] }>;
}

export interface AgentEvent {
  i: number;
  agent: string;
  node: string;
  text: string;
  at: number;
}

export interface AgentsRun extends Omit<WorkflowRun, "ids" | "inputs" | "approval"> {
  llm: "fake" | "claude";
  approval: { proposal: number; title: string; question: string } | null;
  events: AgentEvent[];
  counts: Record<string, Record<string, number>>;
  last_event: AgentEvent | null;
  proposals: { title: string; kind: string; status: string; errors: string[] }[];
  inputs: { domain?: string; seed?: number; faults?: string[]; items?: number; level?: string; perspectives?: boolean };
}
