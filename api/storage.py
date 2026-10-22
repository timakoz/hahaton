"""Хранилище: проекты на диске (входные файлы + параметры) и правки специалистов в SQLite.

Правки — это метки для дообучения ранжировщика M3 (ml/ranker/train.py --feedback data/app.db).
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from forestmerge import settings

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
    params TEXT,
    user TEXT                      -- кто принял решение (логин)
);
"""


def _db() -> sqlite3.Connection:
    settings.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(settings.DB_PATH)
    con.execute(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(feedback)")}
    if "user" not in cols:                                   # база от прошлой версии
        con.execute("ALTER TABLE feedback ADD COLUMN user TEXT")
    return con


def add_feedback(project_id: str, fragment: str, action: str, chosen: str | None, model_choice: str | None,
                 candidates: list[dict], params: dict, user: str | None = None) -> None:
    with closing(_db()) as con, con:
        con.execute("INSERT INTO feedback(ts, project_id, fragment, action, chosen, model_choice, candidates, params, "
                    "user) VALUES (?,?,?,?,?,?,?,?,?)",
                    (time.time(), project_id, fragment, action, chosen, model_choice,
                     json.dumps(candidates, ensure_ascii=False), json.dumps(params, ensure_ascii=False), user))


def feedback_rows() -> list[dict]:
    with closing(_db()) as con:
        cur = con.execute("SELECT id, ts, project_id, fragment, action, chosen, model_choice, candidates, user "
                          "FROM feedback ORDER BY id")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def feedback_count() -> int:
    with closing(_db()) as con:
        return con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]


# --------------------------- проекты ---------------------------
def new_project_dir() -> tuple[str, Path]:
    pid = uuid.uuid4().hex[:10]
    d = settings.PROJECTS_DIR / pid
    (d / "inputs").mkdir(parents=True, exist_ok=True)
    return pid, d


_ID = re.compile(r"^[0-9a-f]{10}$")      # id проектов и профилей — только такие (защита от «../» в URL)


def project_dir(pid: str) -> Path:
    if not _ID.match(pid or ""):
        raise FileNotFoundError("неверный id проекта")
    return settings.PROJECTS_DIR / pid


def save_meta(pid: str, meta: dict) -> None:
    (project_dir(pid) / "project.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")


def load_meta(pid: str) -> dict | None:
    try:
        p = project_dir(pid) / "project.json"
    except FileNotFoundError:
        return None
    return json.loads(p.read_text("utf-8")) if p.exists() else None


def delete_project(pid: str) -> None:
    shutil.rmtree(project_dir(pid), ignore_errors=True)


def list_projects() -> list[dict]:
    if not settings.PROJECTS_DIR.exists():
        return []
    out = []
    for d in sorted(settings.PROJECTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        m = load_meta(d.name)
        if m:
            out.append({"id": d.name, "name": m.get("name"), "created": m.get("created"),
                        "created_by": m.get("created_by"), "n_files": len(m.get("files") or {})})
    return out


# --------------------------- профили параметров ---------------------------
# Профиль = именованный набор параметров расчёта (то, что в панели «Параметры»), поверх норм ЛИ-2022.
def _profile_path(prof_id: str) -> Path:
    if not _ID.match(prof_id or ""):
        raise FileNotFoundError("неверный id профиля")
    return settings.PROFILES_DIR / f"{prof_id}.json"


def list_param_profiles() -> list[dict]:
    if not settings.PROFILES_DIR.exists():
        return []
    out = []
    for p in settings.PROFILES_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text("utf-8"))
            out.append({k: d.get(k) for k in ("id", "name", "created_by", "updated")})
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda d: (d["name"] or "").lower())


def get_param_profile(prof_id: str) -> dict | None:
    try:
        p = _profile_path(prof_id)
    except FileNotFoundError:
        return None
    return json.loads(p.read_text("utf-8")) if p.exists() else None


def save_param_profile(name: str, params: dict, use_models: bool, user: str) -> dict:
    """Новый профиль или обновление существующего с тем же именем."""
    settings.PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    existing = next((d for d in list_param_profiles() if (d["name"] or "").lower() == name.lower()), None)
    prof_id = existing["id"] if existing else uuid.uuid4().hex[:10]
    doc = {"id": prof_id, "name": name, "params": params, "use_models": bool(use_models),
           "created_by": (get_param_profile(prof_id) or {}).get("created_by", user), "updated_by": user,
           "updated": time.time()}
    _profile_path(prof_id).write_text(json.dumps(doc, ensure_ascii=False, indent=1), "utf-8")
    return doc


def delete_param_profile(prof_id: str) -> bool:
    try:
        p = _profile_path(prof_id)
    except FileNotFoundError:
        return False
    if p.exists():
        p.unlink()
        return True
    return False
