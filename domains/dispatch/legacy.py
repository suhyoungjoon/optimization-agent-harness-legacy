"""레가시 I/F 형식(I/F별 CSV) 내보내기와 읽기.

형식은 docs/legacy-if-impact.md의 "레가시 파일 형식"을 따른다. 실데이터는 쓰지 않는다: 생성기로 만든 가상 데이터를
이 형식으로 내보내고 다시 읽어, 실제 레가시 명세가 오면 칼럼 대응만 바꾸면 되게 한다.

    manifest.json   형식 이름·버전, 기준일(1일), 기간(일수)
    orders.csv      #1 접수: 지시서, 희망일자·시각, 지점, 작업유형, 매체, 난이도, 건물유형, 위경도, 긴급
    workers.csv     #10 구성원: 작업자, 지점, 작업그룹, CEI, 위경도, 기본 근무시간
    skills.csv      #5 기술역량: 작업자별 매체 기술·작업유형(개통/장애) 역량·자격
    capacity.csv    #2·#8·#9 일자별 시간역량: 작업자 × 일자 × 30분 칸마다 Y/N
    std_time.csv    #4 표준작업시간: 작업자 × 작업유형 → 분
    bookings.csv    #6 할당이력: 예약으로 이미 잡힌 지시서 → 작업자, 일자, 시작시각
    truth.json      결함 정답표 (가상 데이터에만, 없으면 분석 채점 없음)
"""

import csv
import json
import math
import os
import re
from datetime import date, timedelta
from pathlib import Path

import yaml

from core.registry import domain_file

from . import generator, rule_engine
from .models import Booking, Instance, Order, Worker, hhmm_to_min, min_to_hhmm

FORMAT = "dispatch-legacy"
VERSION = 1
DEFAULT_BASE_DATE = date(2026, 11, 2)
REPO_DIR = Path(__file__).resolve().parents[2]
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

COLUMNS = {
    "orders.csv": ["order_id", "desired_date", "desired_time", "branch", "work_type", "media", "difficulty",
                   "building_type", "lat", "lon", "urgent"],
    "workers.csv": ["worker_id", "branch", "group", "cei", "lat", "lon", "work_start", "work_end"],
    "skills.csv": ["worker_id", "kind", "value"],
    "capacity.csv": ["worker_id", "date", "slot", "available"],
    "std_time.csv": ["worker_id", "work_type", "minutes"],
    "bookings.csv": ["order_id", "worker_id", "date", "start_time"],
}
SKILL_KINDS = ("media", "work_type", "cert")


def resolve(source: str) -> Path:
    """데이터셋 이름 → 폴더. data/legacy(또는 LEGACY_DATA_DIR) 바로 아래 폴더 이름만 받는다."""
    if not isinstance(source, str) or not _NAME.match(source):
        raise ValueError(f"레가시 데이터 이름은 영문·숫자·_·-만: {source!r}")
    root = Path(os.environ.get("LEGACY_DATA_DIR", REPO_DIR / "data" / "legacy"))
    path = root / source
    if not path.is_dir():
        raise ValueError(f"레가시 데이터 폴더가 없음: {source}")
    return path


# --- 좌표: km 평면 ↔ 위경도 ---------------------------------------------------------

def _to_latlon(geo: dict, x: float, y: float) -> tuple[str, str]:
    (lon0, lat0), (kx, ky) = geo["origin"], geo["km_per_deg"]
    return f"{lat0 + y / ky:.7f}", f"{lon0 + x / kx:.7f}"


def _to_xy(geo: dict, lat: float, lon: float) -> tuple[float, float]:
    (lon0, lat0), (kx, ky) = geo["origin"], geo["km_per_deg"]
    return round((lon - lon0) * kx, 3), round((lat - lat0) * ky, 3)


# --- 내보내기 -----------------------------------------------------------------------

def export(inst: Instance, truth: dict | None, out: str | Path, base_date: date = DEFAULT_BASE_DATE) -> Path:
    cfg = generator.load_yaml("dataset.yaml")
    unit = cfg["capacity"]["unit_min"]
    day0, day1 = (hhmm_to_min(cfg["day"][k]) for k in ("start", "end"))
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    as_date = lambda day: (base_date + timedelta(days=day - 1)).isoformat()  # noqa: E731

    rows = {name: [] for name in COLUMNS}
    for o in inst.orders:
        lat, lon = _to_latlon(inst.geo, o.x, o.y)
        rows["orders.csv"].append([o.id, as_date(o.day), min_to_hhmm(o.desired), o.branch, o.work_type, o.media,
                                   o.difficulty, o.building_type, lat, lon, "Y" if o.urgent else "N"])
    for w in inst.workers:
        lat, lon = _to_latlon(inst.geo, w.x, w.y)
        rows["workers.csv"].append([w.id, w.branch, w.group, w.cei, lat, lon,
                                    min_to_hhmm(w.available[0]), min_to_hhmm(w.available[1])])
        for kind, values in (("media", w.skills), ("work_type", w.work_types), ("cert", w.certs)):
            rows["skills.csv"] += [[w.id, kind, v] for v in values]
        rows["std_time.csv"] += [[w.id, t, m] for t, m in w.std_min.items()]
        for day in range(1, inst.days + 1):
            windows = w.windows(day)
            if any(s < day0 or e > day1 for s, e in windows):
                raise ValueError(f"{w.id} {day}일 시간역량이 하루 범위 {cfg['day']} 밖")
            for slot in range(day0, day1, unit):
                ok = any(s <= slot and slot + unit <= e for s, e in windows)
                rows["capacity.csv"].append([w.id, as_date(day), min_to_hhmm(slot), "Y" if ok else "N"])
    for order_id, b in inst.fixed.items():
        rows["bookings.csv"].append([order_id, b.worker_id, as_date(b.day), min_to_hhmm(b.start)])

    for name, data in rows.items():
        with (out / name).open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(COLUMNS[name])
            writer.writerows(data)
    manifest = {"format": FORMAT, "version": VERSION, "base_date": base_date.isoformat(), "days": inst.days}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if truth:
        (out / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        (out / "truth.json").unlink(missing_ok=True)   # 같은 폴더에 남은 이전 정답표로 채점하지 않게
    return out


# --- 읽기 ---------------------------------------------------------------------------

class _Rows:
    """CSV 한 파일. 오류는 '파일 N행: ...'으로 알린다 (머리줄이 1행)."""

    def __init__(self, folder: Path, name: str):
        self.name = name
        path = folder / name
        if not path.is_file():
            raise ValueError(f"{name} 파일이 없음")
        with path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            missing = set(COLUMNS[name]) - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"{name}: 칼럼 없음 {sorted(missing)}")
            self.rows = list(reader)
        for line, row in self:                     # csv는 짧은 행의 빈칸을 None, 긴 행의 남는 칸을 None 키로 준다
            if None in row or any(v is None for v in row.values()):
                raise self.error(line, f"칸 수가 머리줄({len(COLUMNS[name])}칸)과 다름")

    def __iter__(self):
        return ((i + 2, row) for i, row in enumerate(self.rows))

    def error(self, line: int, message: str) -> ValueError:
        return ValueError(f"{self.name} {line}행: {message}")


def load_dir(folder: str | Path) -> tuple[Instance, dict]:
    folder = Path(folder)
    cfg = generator.load_yaml("dataset.yaml")
    dims = yaml.safe_load((generator.PACK_DIR / "dimensions.yaml").read_text(encoding="utf-8"))["dimensions"]
    allowed = {k: set(dims[k]["values"]) for k in ("work_type", "media", "difficulty", "building_type")}
    allowed["branch"] = set(cfg["branches"])
    certs_allowed = {"pole", "high_risk"}
    unit = cfg["capacity"]["unit_min"]
    day0, day1 = (hhmm_to_min(cfg["day"][k]) for k in ("start", "end"))
    branches, geo = generator.load_region(cfg)

    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        base = date.fromisoformat(manifest["base_date"])
        days = int(manifest["days"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"manifest.json을 읽을 수 없음: {exc}") from None
    if manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise ValueError(f"manifest.json: 형식 {manifest.get('format')} v{manifest.get('version')}는 지원하지 않음")

    def day_of(rows: _Rows, line: int, text: str) -> int:
        try:
            day = (date.fromisoformat(text) - base).days + 1
        except ValueError:
            raise rows.error(line, f"날짜 형식 오류 {text!r} (YYYY-MM-DD)") from None
        if not 1 <= day <= days:
            raise rows.error(line, f"날짜 {text}가 기간 {base.isoformat()}부터 {days}일 밖")
        return day

    def minute_of(rows: _Rows, line: int, column: str, text: str) -> int:
        try:
            h, m = text.split(":")
            value = int(h) * 60 + int(m)
        except ValueError:
            raise rows.error(line, f"{column} 시각 형식 오류 {text!r} (HH:MM)") from None
        if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise rows.error(line, f"{column} 시각 범위 오류 {text!r}")
        return value

    def number(rows: _Rows, line: int, column: str, text: str, kind=float):
        try:
            value = kind(text)
        except ValueError:
            raise rows.error(line, f"{column} 숫자 형식 오류 {text!r}") from None
        if not math.isfinite(value):
            raise rows.error(line, f"{column} 값이 유한한 수가 아님 {text!r}")
        return value

    def code(rows: _Rows, line: int, column: str, text: str) -> str:
        if text not in allowed[column]:
            raise rows.error(line, f"{column} 값 {text!r}는 {sorted(allowed[column])} 중 하나여야 함")
        return text

    # 작업자
    rows = _Rows(folder, "workers.csv")
    workers: dict[str, Worker] = {}
    for line, r in rows:
        if r["worker_id"] in workers:
            raise rows.error(line, f"작업자 ID 중복 {r['worker_id']}")
        x, y = _to_xy(geo, number(rows, line, "lat", r["lat"]), number(rows, line, "lon", r["lon"]))
        start, end = (minute_of(rows, line, c, r[c]) for c in ("work_start", "work_end"))
        if start >= end:
            raise rows.error(line, "근무시작이 근무종료보다 늦음")
        if start < day0 or end > day1:
            raise rows.error(line, f"근무시간 {r['work_start']}~{r['work_end']}이 하루 범위 {cfg['day']} 밖")
        workers[r["worker_id"]] = Worker(
            id=r["worker_id"], branch=code(rows, line, "branch", r["branch"]), skills=[], certs=[],
            cei=number(rows, line, "cei", r["cei"], int), x=x, y=y, available=(start, end), work_types=[],
            group=r["group"])

    def worker_of(rows: _Rows, line: int, worker_id: str) -> Worker:
        if worker_id not in workers:
            raise rows.error(line, f"작업자 {worker_id}가 workers.csv에 없음")
        return workers[worker_id]

    rows = _Rows(folder, "skills.csv")
    for line, r in rows:
        w = worker_of(rows, line, r["worker_id"])
        kind, value = r["kind"], r["value"]
        if kind == "media":
            w.skills.append(code(rows, line, "media", value))
        elif kind == "work_type":
            w.work_types.append(code(rows, line, "work_type", value))
        elif kind == "cert":
            if value not in certs_allowed:
                raise rows.error(line, f"자격 값 {value!r}는 {sorted(certs_allowed)} 중 하나여야 함")
            w.certs.append(value)
        else:
            raise rows.error(line, f"kind 값 {kind!r}는 {list(SKILL_KINDS)} 중 하나여야 함")
    for w in workers.values():
        if not w.work_types:                       # 작업유형 역량 등록이 없으면 개통·장애 모두
            w.work_types = ["install", "repair"]

    rows = _Rows(folder, "std_time.csv")
    for line, r in rows:
        w = worker_of(rows, line, r["worker_id"])
        minutes = number(rows, line, "minutes", r["minutes"], int)
        if minutes <= 0:
            raise rows.error(line, f"minutes는 양수여야 함: {minutes}")
        work_type = code(rows, line, "work_type", r["work_type"])
        if work_type in w.std_min:
            raise rows.error(line, f"{w.id} {work_type} 표준작업시간 중복")
        w.std_min[work_type] = minutes

    # 일자별 시간역량: Y 칸을 이어 구간으로. 기본 근무시간과 같은 날은 적지 않는다.
    # 모든 작업자·날짜가 있어야 한다 (빠진 날을 근무시간 전체로 읽지 않게)
    rows = _Rows(folder, "capacity.csv")
    slots: dict[tuple[str, int], list[int]] = {}
    seen_cells: set[tuple[str, int, int]] = set()
    for line, r in rows:
        w = worker_of(rows, line, r["worker_id"])
        day = day_of(rows, line, r["date"])
        slot = minute_of(rows, line, "slot", r["slot"])
        if slot % unit:
            raise rows.error(line, f"slot {r['slot']}이 {unit}분 칸 경계가 아님")
        if slot < day0 or slot + unit > day1:
            raise rows.error(line, f"slot {r['slot']}이 하루 범위 {cfg['day']} 밖")
        if r["available"] not in ("Y", "N"):
            raise rows.error(line, f"available 값 {r['available']!r}는 Y 또는 N")
        if (w.id, day, slot) in seen_cells:
            raise rows.error(line, f"{w.id} {r['date']} {r['slot']} 칸 중복")
        seen_cells.add((w.id, day, slot))
        bucket = slots.setdefault((w.id, day), [])
        if r["available"] == "Y":
            if slot < w.available[0] or slot + unit > w.available[1]:
                raise rows.error(line, f"{w.id} {r['slot']} 칸이 근무시간 {min_to_hhmm(w.available[0])}~"
                                       f"{min_to_hhmm(w.available[1])} 밖인데 Y")
            bucket.append(slot)
    for w in workers.values():
        for day in range(1, days + 1):
            if (w.id, day) not in slots:
                raise ValueError(f"capacity.csv: {w.id} {(base + timedelta(days=day - 1)).isoformat()} 행이 없음 "
                                 f"(모든 작업자·날짜의 칸이 있어야 함)")
    for (worker_id, day), ys in slots.items():
        windows: list[tuple[int, int]] = []
        for s in sorted(ys):
            if windows and windows[-1][1] == s:
                windows[-1] = (windows[-1][0], s + unit)
            else:
                windows.append((s, s + unit))
        w = workers[worker_id]
        if windows != [w.available]:
            w.capacity[day] = windows

    # 지시서
    rows = _Rows(folder, "orders.csv")
    orders: dict[str, Order] = {}
    for line, r in rows:
        if r["order_id"] in orders:
            raise rows.error(line, f"지시서 ID 중복 {r['order_id']}")
        if r["urgent"] not in ("Y", "N"):
            raise rows.error(line, f"urgent 값 {r['urgent']!r}는 Y 또는 N")
        x, y = _to_xy(geo, number(rows, line, "lat", r["lat"]), number(rows, line, "lon", r["lon"]))
        orders[r["order_id"]] = Order(
            id=r["order_id"], day=day_of(rows, line, r["desired_date"]),
            branch=code(rows, line, "branch", r["branch"]), work_type=code(rows, line, "work_type", r["work_type"]),
            media=code(rows, line, "media", r["media"]), difficulty=code(rows, line, "difficulty", r["difficulty"]),
            building_type=code(rows, line, "building_type", r["building_type"]),
            desired=minute_of(rows, line, "desired_time", r["desired_time"]), x=x, y=y, urgent=r["urgent"] == "Y")

    # 할당이력 (예약으로 이미 잡힌 일정)
    rows = _Rows(folder, "bookings.csv")
    fixed: dict[str, Booking] = {}
    booking_line: dict[str, int] = {}
    for line, r in rows:
        if r["order_id"] not in orders:
            raise rows.error(line, f"지시서 {r['order_id']}가 orders.csv에 없음")
        if r["order_id"] in fixed:
            raise rows.error(line, f"지시서 {r['order_id']}의 할당이 두 번 있음")
        w = worker_of(rows, line, r["worker_id"])
        fixed[r["order_id"]] = Booking(w.id, day_of(rows, line, r["date"]),
                                       minute_of(rows, line, "start_time", r["start_time"]))
        booking_line[r["order_id"]] = line

    # 같은 작업자·날짜의 예약 작업 시간이 겹치면 불가능한 일정이다 (작업소요는 커밋된 params와 표준작업시간 기준)
    params = yaml.safe_load(domain_file("dispatch", "params.yaml", generator.PACK_DIR).read_text(encoding="utf-8"))
    jobs: dict[tuple[str, int], list[tuple[int, int, str]]] = {}
    for order_id, b in fixed.items():
        end = b.start + rule_engine.duration_min(orders[order_id], params, workers[b.worker_id])
        jobs.setdefault((b.worker_id, b.day), []).append((b.start, end, order_id))
    for (worker_id, _day), items in jobs.items():
        items.sort()
        for (_s, prev_end, prev), (start, _e, nxt) in zip(items, items[1:]):
            if start < prev_end:
                raise rows.error(booking_line[nxt], f"{worker_id}의 {nxt} 예약이 {prev}(행 {booking_line[prev]})와 "
                                                    f"작업 시간이 겹침")

    truth_path = folder / "truth.json"
    truth = json.loads(truth_path.read_text(encoding="utf-8")) if truth_path.is_file() else {}
    inst = Instance(branches=branches, boundary_zone_km=cfg["boundary_zone_km"], workers=list(workers.values()),
                    orders=list(orders.values()), days=days, geo=geo, fixed=fixed)
    return inst, truth
