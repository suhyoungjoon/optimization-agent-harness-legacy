"""실행 결과 저장 (SQLite). 데이터셋은 (도메인, seed, 결함)만 저장하고 인스턴스는 재생성한다."""

import dataclasses
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from core.interfaces import DecisionRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    seed INTEGER NOT NULL,
    faults TEXT NOT NULL,
    items INTEGER NOT NULL,
    created_at REAL NOT NULL,
    source TEXT                -- [4-a 추가] 원천 파일 이름. 없으면 생성기(seed, faults)로 만든 데이터셋
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    dataset_id TEXT NOT NULL REFERENCES datasets(id),
    agent TEXT NOT NULL,
    level TEXT,
    model TEXT,
    seed INTEGER NOT NULL,
    params_version INTEGER,
    params TEXT NOT NULL,
    status TEXT NOT NULL,
    metrics TEXT,
    violations TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS decisions (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    seq INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS traces (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    item_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    kind TEXT NOT NULL,
    input TEXT,
    output TEXT,
    ts REAL NOT NULL,
    PRIMARY KEY (run_id, item_id, step)
);
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    status TEXT NOT NULL,
    body TEXT,
    score TEXT,
    labels TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS proposal_batches (
    id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES reports(id),
    status TEXT NOT NULL,
    meta TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES proposal_batches(id),
    report_id TEXT NOT NULL REFERENCES reports(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,          -- proposed | invalid | simulating | simulated | approved | rejected | stale
    body TEXT NOT NULL,
    errors TEXT NOT NULL,
    simulation TEXT,
    decision TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

# M3에서 runs에 추가한 열 (기존 DB는 ALTER TABLE로 보강)
RUN_COLUMNS = {"group_id": "TEXT", "repeat": "INTEGER", "scope": "TEXT", "meta": "TEXT"}


def to_jsonable(obj: Any) -> Any:
    """dataclass·tuple 등을 JSON으로 바꿀 수 있는 값으로 변환."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    return obj


def dataset_id(domain: str, seed: int, faults: list[str], source: str | None = None) -> str:
    if source:
        return f"{domain}-src-{source}"
    return f"{domain}-s{seed}-{'-'.join(sorted(faults)) or 'clean'}"


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()   # 여러 run을 동시에 실행할 때 연결을 공유한다
        self.conn.executescript(SCHEMA)
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(runs)")}
        for col, kind in RUN_COLUMNS.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {kind}")
        if "source" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(datasets)")}:
            self.conn.execute("ALTER TABLE datasets ADD COLUMN source TEXT")

    # --- datasets ---
    def save_dataset(self, domain: str, seed: int, faults: list[str], items: int,
                     source: str | None = None) -> dict:
        """source가 있으면 원천 파일로 만든 데이터셋 (seed·faults는 쓰지 않는다)."""
        if source:
            seed, faults = 0, []
        ds_id = dataset_id(domain, seed, faults, source)
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO datasets (id, domain, seed, faults, items, created_at, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ds_id, domain, seed, json.dumps(sorted(faults)), items, time.time(), source))
        return self.get_dataset(ds_id)

    def get_dataset(self, ds_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM datasets WHERE id = ?", (ds_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "faults": json.loads(row["faults"])}

    # --- runs ---
    def create_run(self, *, domain: str, dataset: dict, agent: str, params: dict,
                   level: str | None = None, model: str | None = None, group_id: str | None = None,
                   repeat: int = 0, scope: list[str] | None = None) -> str:
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO runs (run_id, domain, dataset_id, agent, level, model, seed, params_version,"
                " params, status, created_at, group_id, repeat, scope)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                (run_id, domain, dataset["id"], agent, level, model, dataset["seed"],
                 params.get("version"), json.dumps(params), time.time(), group_id, repeat,
                 json.dumps(scope) if scope is not None else None))
        return run_id

    def finish_run(self, run_id: str, decisions: list[DecisionRecord], metrics: dict,
                   violations: list, meta: dict | None = None) -> None:
        with self.lock, self.conn:
            self.conn.executemany(
                "INSERT INTO decisions VALUES (?, ?, ?, ?)",
                [(run_id, i, d.item_id, json.dumps(to_jsonable(d), ensure_ascii=False))
                 for i, d in enumerate(decisions)])
            self.conn.execute(
                "UPDATE runs SET status = 'done', metrics = ?, violations = ?, meta = ?, finished_at = ?"
                " WHERE run_id = ?",
                (json.dumps(metrics), json.dumps(to_jsonable(violations), ensure_ascii=False),
                 json.dumps(meta or {}, ensure_ascii=False), time.time(), run_id))

    def fail_run(self, run_id: str, error: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE runs SET status = 'error', meta = ?, finished_at = ? WHERE run_id = ?",
                (json.dumps({"error": error}, ensure_ascii=False), time.time(), run_id))

    def save_traces(self, traces: list) -> None:
        with self.lock, self.conn:
            self.conn.executemany(
                "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(t.run_id, t.item_id, t.step, t.kind, json.dumps(to_jsonable(t.input), ensure_ascii=False),
                  json.dumps(to_jsonable(t.output), ensure_ascii=False), t.ts) for t in traces])

    def get_traces(self, run_id: str, item_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM traces WHERE run_id = ? AND item_id = ? ORDER BY step", (run_id, item_id)).fetchall()
        return [{**dict(r), "input": json.loads(r["input"]), "output": json.loads(r["output"])} for r in rows]

    def list_runs(self, group_id: str | None = None, dataset_id: str | None = None) -> list[dict]:
        query, args = "SELECT run_id FROM runs WHERE 1 = 1", []
        if group_id:
            query, args = query + " AND group_id = ?", args + [group_id]
        if dataset_id:
            query, args = query + " AND dataset_id = ?", args + [dataset_id]
        with self.lock:
            ids = [r["run_id"] for r in self.conn.execute(query + " ORDER BY created_at", args).fetchall()]
        return [self.get_run(i) for i in ids]

    def get_run(self, run_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        run = dict(row)
        for key in ("params", "metrics", "violations", "scope", "meta"):
            run[key] = json.loads(run[key]) if run[key] else None
        return run

    def get_decisions(self, run_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT record FROM decisions WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
        return [json.loads(r["record"]) for r in rows]

    # --- 분석 리포트 ---
    def create_report(self, run_id: str) -> str:
        report_id = f"rep-{uuid.uuid4().hex[:8]}"
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO reports (id, run_id, status, created_at) VALUES (?, ?, 'running', ?)",
                              (report_id, run_id, time.time()))
        return report_id

    def finish_report(self, report_id: str, body: dict, score: dict) -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE reports SET status = 'done', body = ?, score = ?, finished_at = ? WHERE id = ?",
                              (json.dumps(to_jsonable(body), ensure_ascii=False), json.dumps(score, ensure_ascii=False),
                               time.time(), report_id))

    def fail_report(self, report_id: str, error: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE reports SET status = 'error', error = ?, finished_at = ? WHERE id = ?",
                              (error, time.time(), report_id))

    def set_label(self, report_id: str, finding_id: str, label: str | None) -> dict:
        report = self.get_report(report_id)
        labels = dict(report["labels"])
        if label is None:
            labels.pop(finding_id, None)
        else:
            labels[finding_id] = label
        with self.lock, self.conn:
            self.conn.execute("UPDATE reports SET labels = ? WHERE id = ?", (json.dumps(labels), report_id))
        return labels

    def get_report(self, report_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        for key in ("body", "score", "labels"):
            out[key] = json.loads(out[key]) if out[key] else None
        return out

    # --- 개선안 ---
    def create_batch(self, report_id: str) -> str:
        batch_id = f"bat-{uuid.uuid4().hex[:8]}"
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO proposal_batches (id, report_id, status, created_at) VALUES (?, ?, 'running', ?)",
                              (batch_id, report_id, time.time()))
        return batch_id

    def finish_batch(self, batch_id: str, report_id: str, proposals: list[dict], meta: dict) -> list[str]:
        now = time.time()
        ids = []
        with self.lock, self.conn:
            for p in proposals:
                pid = f"prop-{uuid.uuid4().hex[:8]}"
                ids.append(pid)
                self.conn.execute(
                    "INSERT INTO proposals (id, batch_id, report_id, kind, status, body, errors, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (pid, batch_id, report_id, str(p["proposal"].get("kind")),
                     "invalid" if p["errors"] else "proposed",
                     json.dumps(p["proposal"], ensure_ascii=False), json.dumps(p["errors"], ensure_ascii=False), now, now))
            self.conn.execute("UPDATE proposal_batches SET status = 'done', meta = ?, finished_at = ? WHERE id = ?",
                              (json.dumps(to_jsonable(meta), ensure_ascii=False), now, batch_id))
        return ids

    def fail_batch(self, batch_id: str, error: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE proposal_batches SET status = 'error', error = ?, finished_at = ? WHERE id = ?",
                              (error, time.time(), batch_id))

    def get_batch(self, batch_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM proposal_batches WHERE id = ?", (batch_id,)).fetchone()
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM proposals WHERE batch_id = ? ORDER BY created_at, rowid", (batch_id,))]
        if row is None:
            return None
        return {**dict(row), "meta": json.loads(row["meta"]) if row["meta"] else None,
                "proposals": [self.get_proposal(i) for i in ids]}

    def get_proposal(self, proposal_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        for key in ("body", "errors", "simulation", "decision"):
            out[key] = json.loads(out[key]) if out[key] else None
        return out

    def update_proposal(self, proposal_id: str, **fields) -> dict:
        sets, args = [], []
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            args.append(json.dumps(to_jsonable(value), ensure_ascii=False)
                        if key in ("simulation", "decision") and value is not None else value)
        with self.lock, self.conn:
            self.conn.execute(f"UPDATE proposals SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                              (*args, time.time(), proposal_id))
        return self.get_proposal(proposal_id)

    def mark_stale(self, kind: str, except_id: str) -> None:
        """승인으로 기준 파일이 바뀌면, 같은 종류의 다른 미결 개선안은 옛 기준으로 만든 것이 된다."""
        with self.lock, self.conn:
            self.conn.execute("UPDATE proposals SET status = 'stale', updated_at = ? WHERE kind = ? AND id != ?"
                              " AND status IN ('proposed', 'simulated')", (time.time(), kind, except_id))

    def decided_proposals(self) -> list[dict]:
        with self.lock:
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM proposals WHERE status IN ('approved', 'rejected') ORDER BY updated_at")]
        return [self.get_proposal(i) for i in ids]


    # --- 장기 기억(M12-c)용 조회: 사람이 내린 판단 ---
    def rejected_proposals(self, domain: str) -> list[dict]:
        with self.lock:
            ids = [r["id"] for r in self.conn.execute(
                "SELECT p.id FROM proposals p JOIN reports r ON p.report_id = r.id JOIN runs u ON r.run_id = u.run_id"
                " WHERE p.status = 'rejected' AND u.domain = ? ORDER BY p.updated_at", (domain,))]
        return [self.get_proposal(i) for i in ids]

    def labeled_reports(self, domain: str) -> list[dict]:
        """판정이 하나라도 있는 끝난 리포트. params_version은 분석한 실행의 규칙 버전."""
        with self.lock:
            rows = [tuple(r) for r in self.conn.execute(
                "SELECT r.id, u.params_version FROM reports r JOIN runs u ON r.run_id = u.run_id"
                " WHERE r.status = 'done' AND r.labels != '{}' AND u.domain = ? ORDER BY r.created_at", (domain,))]
        return [{**self.get_report(rid), "params_version": version} for rid, version in rows]

    # --- 재생(시연 모드, cached=true)용 조회 ---
    def done_runs(self, dataset_id: str, agent: str, level: str | None, scope: list[str] | None) -> list[dict]:
        """같은 데이터셋·agent·레벨·범위로 끝난 실행 (명세 시뮬레이션용 실행은 제외), 오래된 순."""
        want = json.dumps(scope) if scope is not None else None
        with self.lock:
            ids = [r["run_id"] for r in self.conn.execute(
                "SELECT run_id FROM runs WHERE dataset_id = ? AND agent = ? AND status = 'done'"
                " AND level IS ? AND scope IS ? AND (group_id IS NULL OR group_id NOT LIKE 'spec-%')"
                " ORDER BY created_at, rowid", (dataset_id, agent, level, want))]
        return [self.get_run(i) for i in ids]

    def latest_report(self, run_ids: list[str]) -> dict | None:
        if not run_ids:
            return None
        marks = ",".join("?" for _ in run_ids)
        with self.lock:
            row = self.conn.execute(f"SELECT id FROM reports WHERE status = 'done' AND run_id IN ({marks})"
                                    " ORDER BY created_at DESC LIMIT 1", run_ids).fetchone()
        return self.get_report(row["id"]) if row else None

    def latest_batch(self, report_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT id FROM proposal_batches WHERE status = 'done' AND report_id = ?"
                                    " ORDER BY created_at DESC LIMIT 1", (report_id,)).fetchone()
        return self.get_batch(row["id"]) if row else None
