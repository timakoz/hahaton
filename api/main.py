"""ForestMerge API.  Запуск:  uvicorn api.main:app --port 8765   (из корня проекта)

Сценарий: POST /api/projects (или /api/projects/sample) → POST /run → карта и карточки решений →
POST /decisions (правка специалиста, сохраняется как метка) → GET /export.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from forestmerge.config import list_profiles, load_norms
from forestmerge.ml_hooks import get_models
from forestmerge.pipeline import Inputs, export_geojson, export_xlsx, load_inputs, result_json, solve
from forestmerge.settings import ROOT, find_sample

from . import storage

app = FastAPI(title="ForestMerge — объединение фрагментированных выделов", version="0.1.0")
WEB = ROOT / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")

_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}     # pid → {"inputs", "result", "json"}


# --------------------------------------------------------------------------
def _inputs(pid: str) -> Inputs:
    c = _CACHE.setdefault(pid, {})
    if "inputs" not in c:
        meta = storage.load_meta(pid)
        if meta is None:
            raise HTTPException(404, "проект не найден")
        f = meta["files"]
        p = lambda k: (storage.project_dir(pid) / "inputs" / f[k]) if f.get(k) else None  # noqa: E731
        csv = (p("vydel_csv"), p("yarus_csv"), p("podrost_csv")) if f.get("vydel_csv") else None
        c["inputs"] = load_inputs(p("network"), p("taxation"), p("lesoseki"), p("volumes"), taxation_csv=csv,
                                  name=meta.get("name", pid))
    return c["inputs"]


def _run(pid: str) -> dict:
    meta = storage.load_meta(pid)
    inp = _inputs(pid)
    models = get_models() if meta.get("use_models") else None
    t0 = time.time()
    res = solve(inp, meta.get("params") or {}, meta.get("overrides") or {}, models=models, llm=meta.get("llm", False))
    js = result_json(res, inp)
    js["elapsed_s"] = round(time.time() - t0, 2)
    js["project"] = {"id": pid, **{k: meta.get(k) for k in ("name", "params", "overrides", "use_models", "llm")}}
    js["models"] = get_models().status() if meta.get("use_models") else None
    _CACHE[pid].update({"result": res, "json": js})
    return js


def _result(pid: str) -> dict:
    c = _CACHE.get(pid) or {}
    if "json" not in c:
        with _LOCK:
            return _run(pid)
    return c["json"]


# --------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "sample": {k: bool(v) for k, v in find_sample().items()}}


@app.get("/api/config")
def config(profile: str = "norms_2022"):
    return {"profiles": list_profiles(), "norms": load_norms(profile), "models": get_models().status(),
            "feedback": storage.feedback_count()}


@app.get("/api/projects")
def projects():
    return storage.list_projects()


@app.post("/api/projects/sample")
def create_sample():
    s = find_sample()
    if not s["network"]:
        raise HTTPException(404, "файлы образца 5_1/5_2/5_3 не найдены")
    pid, d = storage.new_project_dir()
    files = {}
    for k in ("network", "taxation", "lesoseki", "volumes"):
        if s[k]:
            shutil.copy(s[k], d / "inputs" / s[k].name)
            files[k] = s[k].name
    storage.save_meta(pid, {"name": "Образец 5 (кв. 29:28:7:105)", "created": time.time(), "files": files,
                            "params": {}, "overrides": {}})
    return {"id": pid}


@app.post("/api/projects")
async def create_project(name: str = Form("Проект"), network: UploadFile = File(...), lesoseki: UploadFile = File(...),
                         taxation: UploadFile | None = File(None), volumes: UploadFile | None = File(None),
                         vydel_csv: UploadFile | None = File(None), yarus_csv: UploadFile | None = File(None),
                         podrost_csv: UploadFile | None = File(None)):
    pid, d = storage.new_project_dir()
    files = {}
    for key, up in (("network", network), ("lesoseki", lesoseki), ("taxation", taxation), ("volumes", volumes),
                    ("vydel_csv", vydel_csv), ("yarus_csv", yarus_csv), ("podrost_csv", podrost_csv)):
        if up is not None and up.filename:
            fn = f"{key}{Path(up.filename).suffix.lower()}"
            (d / "inputs" / fn).write_bytes(await up.read())
            files[key] = fn
    storage.save_meta(pid, {"name": name, "created": time.time(), "files": files, "params": {}, "overrides": {}})
    try:
        _inputs(pid)
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(d, ignore_errors=True)
        _CACHE.pop(pid, None)
        raise HTTPException(400, f"не удалось прочитать файлы: {e}") from e
    return {"id": pid}


@app.get("/api/projects/{pid}/result")
def get_result(pid: str):
    return JSONResponse(_result(pid))


@app.post("/api/projects/{pid}/run")
def run_project(pid: str, body: dict = Body(default={})):
    meta = storage.load_meta(pid)
    if meta is None:
        raise HTTPException(404, "проект не найден")
    for k in ("params", "use_models", "llm"):
        if k in body:
            meta[k] = body[k]
    if body.get("reset_overrides"):
        meta["overrides"] = {}
    storage.save_meta(pid, meta)
    with _LOCK:
        try:
            return JSONResponse(_run(pid))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"ошибка расчёта: {e}") from e


@app.post("/api/projects/{pid}/decisions")
def decide(pid: str, body: dict = Body(...)):
    """{"fragment": uid, "action": "accept"|"merge"|"keep"|"reset", "target": uid?}"""
    meta = storage.load_meta(pid)
    if meta is None:
        raise HTTPException(404, "проект не найден")
    frag, action = body.get("fragment"), body.get("action")
    res = (_CACHE.get(pid) or {}).get("result")
    dec = next((d for d in res.decisions if d.get("fragment") == frag), None) if res else None
    if dec is None:
        raise HTTPException(404, "решение не найдено (сначала выполните расчёт)")
    cands = [{"uid": c["uid"], "cost": c.get("cost"), "features": c.get("features")} for c in dec.get("candidates", [])
             if c.get("features")]
    ov = dict(meta.get("overrides") or {})
    chosen = None
    if action == "merge":
        chosen = body.get("target")
        ov[frag] = chosen
    elif action == "keep":
        ov[frag] = "keep"
    elif action == "reset":
        ov.pop(frag, None)
    elif action == "accept":
        chosen = dec.get("target")
    else:
        raise HTTPException(400, "action: accept | merge | keep | reset")
    storage.add_feedback(pid, frag, action, chosen, dec.get("target"), cands, meta.get("params") or {})
    meta["overrides"] = ov
    storage.save_meta(pid, meta)
    if action == "accept":
        return JSONResponse(_result(pid))
    with _LOCK:
        return JSONResponse(_run(pid))


@app.get("/api/projects/{pid}/export")
def export(pid: str, fmt: str = "xlsx"):
    _result(pid)
    res = _CACHE[pid]["result"]
    if fmt == "xlsx":
        return Response(export_xlsx(res), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": "attachment; filename=TO_after.xlsx"})
    if fmt == "geojson":
        return Response(export_geojson(res), media_type="application/geo+json",
                        headers={"Content-Disposition": "attachment; filename=vydely_after.geojson"})
    if fmt == "json":
        return Response(json.dumps(_CACHE[pid]["json"], ensure_ascii=False), media_type="application/json",
                        headers={"Content-Disposition": "attachment; filename=result.json"})
    raise HTTPException(400, "fmt: xlsx | geojson | json")


@app.get("/api/feedback")
def feedback(limit: int = 200):
    rows = storage.feedback_rows()[-limit:]
    for r in rows:
        r["candidates"] = len(json.loads(r["candidates"] or "[]"))
    return rows


@app.post("/api/models/reload")
def reload_models():
    return get_models(reload=True).status()
