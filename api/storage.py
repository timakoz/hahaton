"""Хранилище: проекты на диске (входные файлы + параметры) и правки специалистов в SQLite.

Правки — это метки для дообучения ранжировщика M3 (ml/ranker/train.py --feedback data/app.db).
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from forestmerge.settings import DB_PATH, PROJECTS_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    project_id TEXT NOT NULL,
    fragment TEXT NOT NULL,
    action TEXT NOT NULL,          -- accept | merge | keep | reset
    chosen TEXT,                   -- uid выбранного соседа (для merge/accept)
    model_choice TEXT,             -- что предлагал решатель
    candidates TEXT,               -- JSON: [{uid, features, cost}] на момент решения
    params TEXT
);
"""


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute(SCHEMA)
    return con


def add_feedback(project_id: str, fragment: str, action: str, chosen: str | None, model_choice: str | None,
                 candidates: list[dict], params: dict) -> None:
    with closing(_db()) as con, con:
        con.execute("INSERT INTO feedback(ts, project_id, fragment, action, chosen, model_choice, candidates, params) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (time.time(), project_id, fragment, action, chosen, model_choice,
                     json.dumps(candidates, ensure_ascii=False), json.dumps(params, ensure_ascii=False)))


def feedback_rows() -> list[dict]:
    with closing(_db()) as con:
        cur = con.execute("SELECT id, ts, project_id, fragment, action, chosen, model_choice, candidates FROM feedback "
                          "ORDER BY id")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def feedback_count() -> int:
    with closing(_db()) as con:
        return con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]


# --------------------------- проекты ---------------------------
def new_project_dir() -> tuple[str, Path]:
    pid = uuid.uuid4().hex[:10]
    d = PROJECTS_DIR / pid
    (d / "inputs").mkdir(parents=True, exist_ok=True)
    return pid, d


def project_dir(pid: str) -> Path:
    return PROJECTS_DIR / pid


def save_meta(pid: str, meta: dict) -> None:
    (project_dir(pid) / "project.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")


def load_meta(pid: str) -> dict | None:
    p = project_dir(pid) / "project.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else None


def list_projects() -> list[dict]:
    if not PROJECTS_DIR.exists():
        return []
    out = []
    for d in sorted(PROJECTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        m = load_meta(d.name)
        if m:
            out.append({"id": d.name, "name": m.get("name"), "created": m.get("created")})
    return out
