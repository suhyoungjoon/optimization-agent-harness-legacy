import L from "leaflet";
import { useEffect, useMemo, useRef, useState } from "react";
import type { DecisionRecord } from "../../types";
import type { ResultViewProps } from "../types";
import { branchColor, hhmm, slot, useDistrictMap, valueLabel, windowsOf, type Instance, type Order, type Worker } from "./districtMap";

type Hover =
  | { kind: "order"; order: Order; record?: DecisionRecord; x: number; y: number }
  | { kind: "worker"; worker: Worker; jobs: number; x: number; y: number };

// 매칭 단계 → 쉬운 말 (params.yaml의 matching: 단계가 올라갈수록 시간·지역 조건을 완화)
const STAGE_NAMES: Record<string, string> = { "1": "조건 그대로", "2": "조금 완화", "3": "많이 완화" };
export const stageName = (stage: unknown) => STAGE_NAMES[String(stage)] ?? `${String(stage)}단계`;

export default function DispatchMap({ instance, decisions, reasonLabels, reasonDetails = {}, selected, onSelect, highlight }: ResultViewProps) {
  const inst = instance as Instance;
  const records = useMemo(() => new Map(decisions.map((d) => [d.item_id, d])), [decisions]);
  // 실행 범위에 들어 있는 날짜만 고를 수 있다
  // 배정일: 결정에 day가 있으면 그날(희망일보다 늦게 잡은 건), 없으면 희망일
  const dayOf = useMemo(() => {
    return (o: Order) => {
      const d = records.get(o.id)?.decision;
      return d && typeof d.day === "number" ? d.day : o.day;
    };
  }, [records]);
  const days = useMemo(() => {
    const inRun = new Set(inst.orders.filter((o) => records.has(o.id)).map(dayOf));
    return [...inRun].sort((a, b) => a - b);
  }, [inst, records, dayOf]);
  const lit = useMemo(() => (highlight ? new Set(highlight) : null), [highlight]);
  // 강조 항목이 있으면 강조 항목이 가장 많은 날을 기본으로 보여준다
  const litDay = useMemo(() => {
    if (!lit) return null;
    const counts = new Map<number, number>();
    for (const o of inst.orders) if (lit.has(o.id)) counts.set(dayOf(o), (counts.get(dayOf(o)) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? null;
  }, [inst, lit, dayOf]);
  const [dayChoice, setDay] = useState<number | null>(null);
  const day = dayChoice !== null && days.includes(dayChoice) ? dayChoice : (litDay ?? days[0] ?? 1);
  const [hover, setHover] = useState<Hover | null>(null);
  const [tiles, setTiles] = useState(true);

  const orders = useMemo(() => inst.orders.filter((o) => dayOf(o) === day && records.has(o.id)), [inst, day, records, dayOf]);
  const branchName = (b: string) => `${inst.branches[b]?.name ?? b}지점`;

  // 작업자별 당일 경로: 작업자 좌표 → 배정된 지시서를 시작 시각 순으로
  const routes = useMemo(() => {
    const byWorker = new Map<string, { start: string; order: Order }[]>();
    for (const o of orders) {
      const d = records.get(o.id)?.decision;
      if (!d || records.get(o.id)?.status !== "success") continue;
      const wid = String(d.worker_id);
      byWorker.set(wid, [...(byWorker.get(wid) ?? []), { start: String(d.start_time), order: o }]);
    }
    for (const jobs of byWorker.values()) jobs.sort((a, b) => a.start.localeCompare(b.start));
    return byWorker;
  }, [orders, records]);

  const failed = orders.filter((o) => records.get(o.id)?.status !== "success").length;
  const held = orders.filter((o) => {
    const st = records.get(o.id)?.status;
    return st === "blocked" || st === "pending_approval";
  }).length;
  const selectedRecord = selected ? records.get(selected) : undefined;
  const selectedOrder = selected ? inst.orders.find((o) => o.id === selected) : undefined;

  // --- Leaflet ---------------------------------------------------------------
  const box = useRef<HTMLDivElement>(null);
  const { district, toLatLng } = useDistrictMap(box, inst, tiles);

  // 지시서·작업자·동선
  useEffect(() => {
    const layer = district?.data;
    if (!layer) return;
    layer.clearLayers();
    const hoverAt = (e: L.LeafletMouseEvent) => ({ x: e.containerPoint.x, y: e.containerPoint.y });

    for (const [wid, jobs] of routes) {
      const w = inst.workers.find((x) => x.id === wid);
      if (!w) continue;
      const pts = [toLatLng(w.x, w.y), ...jobs.map((j) => toLatLng(j.order.x, j.order.y))];
      L.polyline(pts, { className: `route ${slot(w.branch)}`, interactive: false }).addTo(layer);
    }

    for (const o of orders) {
      const r = records.get(o.id);
      const dim = lit !== null && !lit.has(o.id) ? " dim" : "";
      const isSel = selected === o.id;
      const at = toLatLng(o.x, o.y);
      let marker: L.CircleMarker | L.Marker;
      if (r?.status === "success") {
        marker = L.circleMarker(at, {
          radius: isSel ? 7 : 5,
          className: `order-dot ${slot(o.branch)}${dim}${lit?.has(o.id) ? " lit" : ""}${isSel ? " selected" : ""}`,
        });
      } else if (r?.status === "pending_approval") {
        marker = L.circleMarker(at, { radius: isSel ? 7 : 5.5, className: `order-pending${dim}` });
      } else {
        const size = isSel ? 18 : 14;
        marker = L.marker(at, {
          icon: L.divIcon({
            className: `order-fail${dim}`,
            iconSize: [size, size],
            html: `<svg viewBox="0 0 10 10" width="${size}" height="${size}"><path d="M2,2L8,8M8,2L2,8" stroke-width="${isSel ? 2.4 : 1.8}"/></svg>`,
          }),
        });
      }
      marker
        .on("mouseover", (e: L.LeafletMouseEvent) => setHover({ kind: "order", order: o, record: r, ...hoverAt(e) }))
        .on("mouseout", () => setHover(null))
        .on("click", () => onSelect?.(o.id))
        .addTo(layer);
    }

    for (const w of inst.workers) {
      L.marker(toLatLng(w.x, w.y), {
        icon: L.divIcon({ className: `worker ${slot(w.branch)}`, iconSize: [11, 11] }),
        keyboard: false,
      })
        .on("mouseover", (e: L.LeafletMouseEvent) =>
          setHover({ kind: "worker", worker: w, jobs: routes.get(w.id)?.length ?? 0, ...hoverAt(e) }))
        .on("mouseout", () => setHover(null))
        .addTo(layer);
    }
  }, [district, inst, orders, records, routes, lit, selected, onSelect, toLatLng]);

  return (
    <div className="dispatch-map">
      <div className="map-toolbar">
        <label>
          날짜{" "}
          <select value={day} onChange={(e) => setDay(Number(e.target.value))}>
            {days.map((d) => (
              <option key={d} value={d}>
                {d}일차
              </option>
            ))}
          </select>
        </label>
        <span className="muted">
          지시서 {orders.length}건 · 배정 {orders.length - failed}건 · <span className="critical-text">미배정 {failed}건</span>
          {lit && <> · 강조 {orders.filter((o) => lit.has(o.id)).length}건</>}
          {held > 0 && <> (차단·승인 대기 {held}건 포함)</>}
        </span>
        <label className="small muted">
          <input type="checkbox" checked={tiles} onChange={(e) => setTiles(e.target.checked)} /> 배경 지도
        </label>
      </div>

      <div className="map-frame" onMouseLeave={() => setHover(null)}>
        <div ref={box} className="leaflet-box" role="img" aria-label={`${day}일차 배정 지도 (서울 강남3구)`} />
        {hover && (
          <div className="tooltip" style={{ left: hover.x + 12, top: hover.y + 12 }}>
            {hover.kind === "order" ? (
              <OrderSummary order={hover.order} record={hover.record} reasons={reasonLabels} details={reasonDetails} branchName={branchName} />
            ) : (
              <>
                <strong>
                  {hover.worker.id} · {branchName(hover.worker.branch)}
                  {hover.worker.group ? ` · 그룹 ${hover.worker.group}` : ""}
                </strong>
                <div>
                  기술 {hover.worker.skills.join(", ")} · 역량{" "}
                  {(hover.worker.work_types ?? ["install", "repair"]).map((t) => valueLabel(t)).join("·")}
                </div>
                <div>자격 {hover.worker.certs.join(", ") || "없음"} · CEI {hover.worker.cei}</div>
                {hover.worker.std_min && (
                  <div>
                    표준시간{" "}
                    {Object.entries(hover.worker.std_min).map(([t, m]) => `${valueLabel(t)} ${m}분`).join(" · ")}
                  </div>
                )}
                <div>
                  근무 {hhmm(hover.worker.available[0])}~{hhmm(hover.worker.available[1])} · 시간역량{" "}
                  {windowsOf(hover.worker, day).map(([s, e]) => `${hhmm(s)}~${hhmm(e)}`).join(", ") || "없음"} · 당일 {hover.jobs}건
                </div>
              </>
            )}
          </div>
        )}
      </div>

      <div className="legend">
        {Object.keys(inst.branches).map((b) => (
          <span key={b}>
            <i className="swatch" style={{ background: branchColor(b) }} />
            {branchName(b)}
          </span>
        ))}
        <span>▪ 작업자</span>
        <span>● 배정된 지시서</span>
        <span className="critical-text">✕ 미배정·차단</span>
        <span className="warning-text">◯ 승인 대기</span>
        <span>— 작업자 동선</span>
      </div>

      <div className="detail">
        {selectedOrder ? (
          <>
            <OrderSummary order={selectedOrder} record={selectedRecord} reasons={reasonLabels} details={reasonDetails}
              branchName={branchName} />
            <p className="evidence">{selectedRecord?.evidence}</p>
          </>
        ) : (
          <p className="muted">지시서를 클릭하면 판단 근거가 표시됩니다.</p>
        )}
      </div>
    </div>
  );
}

function OrderSummary({
  order,
  record,
  reasons,
  details,
  branchName,
}: {
  order: Order;
  record?: DecisionRecord;
  reasons: Record<string, string>;
  details: Record<string, string>;
  branchName: (b: string) => string;
}) {
  const d = record?.decision;
  return (
    <>
      <strong>
        {order.id} · {branchName(order.branch)}
      </strong>
      <div>
        {valueLabel(order.work_type)} · {order.media} · {valueLabel(order.difficulty)} · {valueLabel(order.building_type)}
      </div>
      <div>희망 {hhmm(order.desired)}</div>
      {record?.status === "success" && d ? (
        <div>
          → {String(d.worker_id)} {String(d.start_time)} <span title={`매칭 ${String(d.matching_stage)}단계`}>({stageName(d.matching_stage)})</span>
        </div>
      ) : (record?.status === "blocked" || record?.status === "pending_approval") && d ? (
        <div className="critical-text">
          {record.status === "blocked" ? "✕ 차단" : "⏸ 승인 대기"}: {String(d.worker_id)} {String(d.start_time)}
        </div>
      ) : (
        <div className="critical-text">
          ✕ 미배정
          {record?.reason_code && (
            <span title={`${details[record.reason_code] ?? ""} (${record.reason_code})`}>: {reasons[record.reason_code] ?? record.reason_code}</span>
          )}
        </div>
      )}
    </>
  );
}
