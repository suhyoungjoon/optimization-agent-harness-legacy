import type { DomainAdapter, Scope } from "../types";
import DispatchDataMap from "./DispatchDataMap";
import DispatchMap, { stageName } from "./DispatchMap";
import { VALUE_LABELS as LABELS, hhmm } from "./districtMap";

export const dispatchAdapter: DomainAdapter = {
  ResultView: DispatchMap,
  data: {
    tables: [
      { key: "branches", label: "지점", idField: "id" },
      { key: "workers", label: "작업자", idField: "id" },
      { key: "orders", label: "지시서", idField: "id" },
    ],
    hidden: ["polygon"],
    fieldLabels: {
      id: "ID", name: "이름", day: "날짜", branch: "지점", work_type: "작업유형", media: "매체", difficulty: "난이도",
      building_type: "건물유형", desired: "희망시간", x: "x(km)", y: "y(km)", skills: "기술", certs: "자격", cei: "CEI",
      work_types: "개통/AS 역량", group: "작업그룹", std_min: "표준작업시간",
      available: "근무시간", capacity: "시간역량(일자별)",
    },
    format: (_table, field, value) => {
      if (field === "desired" && typeof value === "number") return hhmm(value);
      if (field === "available" && Array.isArray(value)) return `${hhmm(value[0])}~${hhmm(value[1])}`;
      if (field === "capacity" && value && typeof value === "object") {
        const days = Object.entries(value as Record<string, [number, number][]>);
        return days.length
          ? days.map(([d, ws]) => `${d}일 ${ws.map(([s, e]) => `${hhmm(s)}~${hhmm(e)}`).join(", ") || "N"}`).join(" / ")
          : "근무시간과 같음";
      }
      if (field === "std_min" && value && typeof value === "object" && !Array.isArray(value))
        return Object.entries(value as Record<string, number>).map(([t, m]) => `${LABELS[t] ?? t} ${m}분`).join(", ");
      if (field === "work_types" && Array.isArray(value)) return value.map((v) => LABELS[v] ?? v).join(", ");
      if (field === "certs" && Array.isArray(value)) return value.map((v) => LABELS[v] ?? v).join(", ") || "없음";
      if (["work_type", "difficulty", "building_type"].includes(field) && typeof value === "string")
        return `${LABELS[value] ?? value} (${value})`;
      return undefined;
    },
    View: DispatchDataMap,
  },
  tradeoff: { axes: ["matching.area_extension_km[2]", "matching.time_window_min[2]"], metrics: ["assignment_rate", "on_time_rate"] },
  metrics: [
    { key: "assignment_rate", label: "배정 성공률", format: "pct", headline: true, primary: true, better: "up",
      tech: "할당성공률: 배정된 지시서 ÷ 전체 지시서" },
    { key: "desired_time_match_rate", label: "희망시간 준수율", format: "pct", headline: true, better: "up",
      tech: "희망시간 일치율: 배정된 지시서 중 약속 시간대(실제 시작 시각이 속한 1시간)가 고객 희망 시간대와 같은 비율" },
    { key: "on_time_rate", label: "정시 배정 비율", format: "pct", better: "up",
      tech: "희망 시간대에 약속된 지시서 ÷ 전체 지시서 (배정이 늘어도 착시가 없는 정시 서비스 기준)" },
    { key: "avg_travel_min", label: "평균 이동시간", format: "min", headline: true, better: "down",
      tech: "직전 위치에서 지시서까지 이동시간 평균 (직선거리 × 우회계수 ÷ 평균 속도)" },
    { key: "worker_utilization", label: "작업자 가동률", format: "pct", better: "up",
      tech: "작업자 활용률: 작업 시간 합 ÷ 작업자 가능시간 합" },
    { key: "stage_1_share", label: "조건 그대로 배정", format: "pct", tech: "1단계 매칭 비율 (시간·지역 조건 완화 없음)" },
    { key: "stage_2_share", label: "조금 완화해 배정", format: "pct", tech: "2단계 매칭 비율 (시간·지역 조건을 한 단계 완화)" },
    { key: "stage_3_share", label: "많이 완화해 배정", format: "pct", tech: "3단계 매칭 비율 (시간·지역 조건을 최대로 완화)" },
  ],
  valueNames: LABELS,
  decisionText: (d) => `${String(d.worker_id)} ${String(d.start_time)} (${stageName(d.matching_stage)})`,
  reasonNames: {
    NO_SKILL: "기술 보유자 없음",
    NO_WORK_TYPE: "개통/장애 역량 보유자 없음",
    NO_CERT: "자격 보유자 없음",
    NO_TIME_MATCH: "희망시간에 가능한 사람 없음",
    OUT_OF_AREA: "갈 수 있는 거리 밖",
    CAPACITY: "일정이 꽉 참",
  },
  // 지시서 ID는 "D01-O015" 형식: 날짜별로 묶는다
  scopes: (_instance, itemIds) => {
    const days = new Map<string, string[]>();
    for (const id of itemIds) {
      const day = id.split("-")[0];
      days.set(day, [...(days.get(day) ?? []), id]);
    }
    const perDay: Scope[] = [...days.entries()].map(([day, items]) => ({
      id: day,
      label: `${Number(day.slice(1))}일차 (${items.length}건)`,
      items,
    }));
    const first = perDay[0];
    const sample: Scope[] = first
      ? [{ id: `${first.id}-10`, label: `${Number(first.id.slice(1))}일차 앞 10건 (시범)`, items: (first.items ?? []).slice(0, 10) }]
      : [];
    return [...sample, ...perDay, { id: "all", label: `전체 ${itemIds.length}건`, items: null }];
  },
};
