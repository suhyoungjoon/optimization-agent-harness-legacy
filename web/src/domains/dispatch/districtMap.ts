import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { useEffect, useMemo, useState, type RefObject } from "react";

// domains/dispatch/models.py 의 JSON 형태. 좌표는 km 평면이고 geo로 위경도로 바꾼다.
export interface Worker {
  id: string;
  branch: string;
  skills: string[];
  certs: string[];
  cei: number;
  x: number;
  y: number;
  available: [number, number];
  // 일자별 시간역량: 날짜 → 일할 수 있는 구간 목록. 기본 근무시간과 다른 날만 있다 (빈 목록 = 하루 전체 N)
  capacity?: Record<string, [number, number][]>;
  work_types?: string[];   // 개통/AS 역량 (install, repair)
  group?: string;          // 작업그룹
  std_min?: Record<string, number>;   // 표준작업시간: 작업유형 → 기본 소요(분)
}
export interface Order {
  id: string;
  day: number;
  branch: string;
  work_type: string;
  media: string;
  difficulty: string;
  building_type: string;
  desired: number;
  x: number;
  y: number;
}
export interface Branch {
  name: string;
  polygon: [number, number][];
}
export interface Instance {
  branches: Record<string, Branch>;
  geo: { origin: [number, number]; km_per_deg: [number, number] };
  workers: Worker[];
  orders: Order[];
  days: number;
}

const BRANCH_SLOT: Record<string, number> = { A: 1, B: 2, C: 3 };
const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
const BOUNDARY_ATTRIBUTION = "행정구역 경계: 통계청(2013)";

// 값 코드 → 한글 이름 (데이터 표와 지도 설명에서 같이 쓴다)
export const VALUE_LABELS: Record<string, string> = {
  install: "개통", repair: "장애", none: "일반", pole: "승주", outdoor: "옥외", high_risk: "고위험",
  house: "주택", apartment: "아파트", core: "관할 안쪽", boundary: "경계 지역",
};
export const valueLabel = (v: string) => VALUE_LABELS[v] ?? v;

/** 그날 일할 수 있는 구간 (없는 날은 기본 근무시간). */
export const windowsOf = (w: Worker, day: number): [number, number][] => w.capacity?.[String(day)] ?? [w.available];

export const hhmm = (m: number) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
export const branchColor = (b: string) => `var(--series-${BRANCH_SLOT[b] ?? 1})`;
export const slot = (b: string) => `b${BRANCH_SLOT[b] ?? 1}`;

export interface DistrictMap {
  map: L.Map;
  data: L.LayerGroup;                    // 지시서·작업자 등 매번 다시 그리는 층
  zones: Record<string, L.Polygon>;      // 지점별 관할 구역
}

/** 강남3구 관할 구역과 배경 지도(선택)를 그린 Leaflet 지도. 인스턴스가 바뀌면 새로 만든다. */
export function useDistrictMap(box: RefObject<HTMLDivElement | null>, inst: Instance, tiles: boolean) {
  const [district, setDistrict] = useState<DistrictMap | null>(null);
  const toLatLng = useMemo(() => {
    const [lon0, lat0] = inst.geo.origin;
    const [kx, ky] = inst.geo.km_per_deg;
    return (x: number, y: number): L.LatLngTuple => [lat0 + y / ky, lon0 + x / kx];
  }, [inst.geo]);

  useEffect(() => {
    if (!box.current) return;
    const m = L.map(box.current, { scrollWheelZoom: false, zoomSnap: 0.25 });
    m.attributionControl.setPrefix(false).addAttribution(BOUNDARY_ATTRIBUTION);
    const zones = Object.fromEntries(
      Object.entries(inst.branches).map(([b, br]) => [
        b,
        L.polygon(br.polygon.map(([x, y]) => toLatLng(x, y)), { className: `branch-zone ${slot(b)}`, interactive: false })
          .bindTooltip(`${br.name}지점`, { permanent: true, direction: "center", className: "branch-label" })
          .addTo(m),
      ]),
    );
    m.fitBounds(L.featureGroup(Object.values(zones)).getBounds(), { padding: [8, 8] });
    const data = L.layerGroup().addTo(m);
    setDistrict({ map: m, data, zones });
    const resize = new ResizeObserver(() => m.invalidateSize());
    resize.observe(box.current);
    return () => {
      resize.disconnect();
      m.remove();
      setDistrict(null);
    };
  }, [box, inst, toLatLng]);

  // 배경 지도 (인터넷이 안 되면 타일만 비고 관할 구역은 그대로 보인다)
  useEffect(() => {
    if (!district || !tiles) return;
    const layer = L.tileLayer(TILES, { maxZoom: 18, attribution: TILE_ATTRIBUTION, className: "base-tiles" }).addTo(district.map);
    return () => {
      layer.remove();
    };
  }, [district, tiles]);

  return { district, toLatLng };
}
