"""ForestMerge API.  Запуск:  uvicorn api.main:app --port 8765   (из корня проекта)

Сценарий: POST /api/login → POST /api/projects (или /api/projects/sample) → POST /run → карта и карточки
решений → POST /decisions (правка специалиста, сохраняется как метка с автором) → GET /export.
Все страницы и /api/* (кроме входа и /api/health) требуют сессию — см. api/auth.py.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from forestmerge import schema as S
from forestmerge.config import list_profiles, load_norms
from forestmerge.io import readers as RD
from forestmerge.ml_hooks import get_models
from forestmerge.pipeline import Inputs, check_inputs, export_geojson, export_xlsx, load_inputs, result_json, solve
from forestmerge.settings import ROOT, find_sample

from . import auth, storage

app = FastAPI(title="ForestMerge — объединение фрагментированных выделов", version="0.2.0",
              docs_url=None, redoc_url=None, openapi_url=None)     # схему API наружу не показываем
app.add_middleware(auth.AuthMiddleware)
WEB = ROOT / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")

_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}     # pid → {"inputs", "result", "json"}


# --------------------------------------------------------------------------
# Файлы проекта: слот → подпись, допустимые расширения, обязательность
SLOTS = {
    "network": {"label": "выдельная сеть (GeoJSON)", "ext": [".geojson", ".json"], "required": True},
    "lesoseki": {"label": "лесосеки (GeoJSON)", "ext": [".geojson", ".json"], "required": True},
    "taxation": {"label": "таксационное описание (XLSX)", "ext": [".xlsx"], "required": False},
    "vydel_csv": {"label": "ТО: выдел (CSV)", "ext": [".csv"], "required": False},
    "yarus_csv": {"label": "ТО: ярус (CSV)", "ext": [".csv"], "required": False},
    "podrost_csv": {"label": "ТО: подрост (CSV)", "ext": [".csv"], "required": False},
    "volumes": {"label": "объём заготовленной древесины (CSV)", "ext": [".csv"], "required": False},
}


def _meta_or_404(pid: str) -> dict:
    meta = storage.load_meta(pid)
    if meta is None:
        raise HTTPException(404, "проект не найден")
    return meta


def _inputs(pid: str) -> Inputs:
    c = _CACHE.setdefault(pid, {})
    if "inputs" not in c:
        meta = _meta_or_404(pid)
        f = meta["files"]
        missing = [SLOTS[k]["label"] for k in SLOTS if SLOTS[k]["required"] and not f.get(k)]
        if missing:
            raise HTTPException(400, "В проекте не хватает файлов: " + ", ".join(missing)
                                + ". Добавьте их в «Файлы проекта».")
        p = lambda k: (storage.project_dir(pid) / "inputs" / f[k]) if f.get(k) else None  # noqa: E731
        use_csv = not f.get("taxation") and bool(f.get("vydel_csv"))       # XLSX важнее CSV, если есть оба
        if use_csv and not f.get("yarus_csv"):
            raise HTTPException(400, "Для ТО в CSV нужны как минимум файлы «выдел» и «ярус».")
        csv = (p("vydel_csv"), p("yarus_csv"), p("podrost_csv")) if use_csv else None
        c["inputs"] = load_inputs(p("network"), None if use_csv else p("taxation"), p("lesoseki"), p("volumes"),
                                  taxation_csv=csv, name=meta.get("name", pid))
    return c["inputs"]


def _wants_models(meta: dict) -> bool:
    """Оценка «правила + ML» / «ML» без моделей не имеет смысла — подключаем их автоматически."""
    return bool(meta.get("use_models")) or (meta.get("params") or {}).get("scoring", "rules") != "rules"


def _run(pid: str) -> dict:
    meta = _meta_or_404(pid)
    inp = _inputs(pid)
    problems = check_inputs(inp)
    if problems:
        raise HTTPException(400, " ".join(problems))
    models = get_models() if _wants_models(meta) else None
    t0 = time.time()
    res = solve(inp, meta.get("params") or {}, meta.get("overrides") or {}, models=models, llm=meta.get("llm", False))
    js = result_json(res, inp)
    js["elapsed_s"] = round(time.time() - t0, 2)
    js["project"] = {"id": pid, **{k: meta.get(k) for k in ("name", "params", "overrides", "use_models", "llm",
                                                             "profile_name", "created_by")}}
    js["models"] = get_models().status() if _wants_models(meta) else None
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
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ вход / выход
@app.get("/login")
def login_page(request: Request):
    if auth.enabled() and auth.session_user(request.cookies.get(auth.COOKIE)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(WEB / "login.html", headers={"Cache-Control": "no-store"})


@app.post("/api/login")
def login(request: Request, body: dict = Body(...)):
    if not auth.enabled():
        return {"ok": True, "user": "local", "role": "admin"}
    login_ = str(body.get("login", "")).strip()[:64]
    password = str(body.get("password", ""))[:256]
    ip = auth.client_ip(request)
    wait = auth.locked(ip, login_)
    if wait:
        raise HTTPException(429, f"слишком много неудачных попыток, повторите через {wait // 60 + 1} мин")
    user = auth.authenticate(login_, password)
    if user is None:
        auth.register_fail(ip, login_)
        if auth.user_count() == 0:
            raise HTTPException(401, "пользователей ещё нет — создайте на сервере: ./scripts/users.sh add <логин> --admin")
        raise HTTPException(401, "неверный логин или пароль")
    auth.clear_fails(ip, login_)
    token = auth.create_session(user["login"], ip, request.headers.get("user-agent"))
    resp = JSONResponse({"ok": True, "user": user["login"], "role": user["role"]})
    auth.set_cookie(resp, request, token)
    return resp


@app.post("/api/logout")
def logout(request: Request):
    auth.drop_session(request.cookies.get(auth.COOKIE))
    resp = JSONResponse({"ok": True})
    auth.clear_cookie(resp)
    return resp


@app.get("/api/me")
def me(request: Request):
    u = auth.current_user(request)
    return {"user": u["login"], "role": u["role"], "auth": auth.enabled()}


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
def create_sample(request: Request):
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
                            "params": {}, "overrides": {}, "created_by": auth.current_user(request)["login"]})
    return {"id": pid}


@app.post("/api/projects")
async def create_project(request: Request, name: str = Form("Проект"), network: UploadFile = File(...),
                         lesoseki: UploadFile = File(...),
                         taxation: UploadFile | None = File(None), volumes: UploadFile | None = File(None),
                         vydel_csv: UploadFile | None = File(None), yarus_csv: UploadFile | None = File(None),
                         podrost_csv: UploadFile | None = File(None)):
    pid, d = storage.new_project_dir()
    files, orig = {}, {}
    for key, up in (("network", network), ("lesoseki", lesoseki), ("taxation", taxation), ("volumes", volumes),
                    ("vydel_csv", vydel_csv), ("yarus_csv", yarus_csv), ("podrost_csv", podrost_csv)):
        if up is not None and up.filename:
            fn = f"{key}{Path(up.filename).suffix.lower()}"
            (d / "inputs" / fn).write_bytes(await up.read())
            files[key] = fn
            orig[key] = Path(up.filename).name[:200]
    storage.save_meta(pid, {"name": name[:200], "created": time.time(), "files": files, "orig_names": orig,
                            "params": {}, "overrides": {}, "created_by": auth.current_user(request)["login"]})
    try:
        problems = check_inputs(_inputs(pid))
    except Exception as e:  # noqa: BLE001
        problems = [f"не удалось прочитать файлы: {e}"]
    if problems:
        shutil.rmtree(d, ignore_errors=True)
        _CACHE.pop(pid, None)
        raise HTTPException(400, " ".join(problems))
    return {"id": pid}


def _check_single(slot: str, path: Path) -> None:
    """Быстрая проверка одного файла сразу при загрузке — понятная ошибка вместо падения при расчёте."""
    try:
        if slot == "network":
            g = RD.read_network(path)
            if g.empty:
                raise ValueError("в файле нет полигонов")
            if any("ЛС" in str(v).upper() for v in list(g["vid"])[:500]):
                raise ValueError("похоже, это файл лесосек (номера вида …:ЛС1), а нужна сеть выделов квартала")
        elif slot == "lesoseki":
            if RD.read_lesoseki(path).empty:
                raise ValueError("в файле нет полигонов")
        elif slot == "taxation":
            tax = RD.read_taxation_xlsx(path)
            if tax.general.empty:
                raise ValueError("не найден лист «Общие» с выделами (формат как «Пример материалов ТО.xlsx»)")
            if S.REG_QUARTER in tax.general and tax.general[S.REG_QUARTER].nunique() > 1:
                raise ValueError("в файле несколько кварталов — это общая выгрузка для обучения, нужен ТО одного квартала")
        elif slot == "volumes":
            v = RD.read_volumes(path)
            if "ls_id" not in v or "volume_m3" not in v:
                raise ValueError("нет колонок «Номер_лесосеки» и «Объем_древесины_куб_м» (формат как образец 5_3)")
        else:
            df = RD._read_csv_any(path)
            need = {"vydel_csv": S.VID, "yarus_csv": S.S_CODE, "podrost_csv": S.U_CODE}[slot]
            if need not in df.columns:
                raise ValueError(f"нет колонки «{need}» — это не та таблица ТО")
            if S.REG_QUARTER in df.columns and df[S.REG_QUARTER].nunique() > 1:
                raise ValueError(f"в CSV {df[S.REG_QUARTER].nunique()} кварталов — нужна выгрузка одного квартала")
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"файл не читается: {e}") from e


def _files_status(pid: str) -> dict:
    meta = _meta_or_404(pid)
    f, orig = meta.get("files") or {}, meta.get("orig_names") or {}
    d = storage.project_dir(pid) / "inputs"
    files = {k: {"name": orig.get(k, v), "size": (d / v).stat().st_size if (d / v).exists() else 0} for k, v in f.items()}
    missing = [k for k in SLOTS if SLOTS[k]["required"] and k not in f]
    problems, notes = [], []
    if f.get("taxation") and f.get("vydel_csv"):
        notes.append("Загружены и XLSX, и CSV таксационного описания — используется XLSX.")
    if not f.get("taxation") and not f.get("vydel_csv"):
        notes.append("Таксационное описание не загружено: расчёт пойдёт без характеристик выделов (только геометрия).")
    if not missing:
        try:
            problems += check_inputs(_inputs(pid))
        except HTTPException as e:
            problems.append(str(e.detail))
        except Exception as e:  # noqa: BLE001
            problems.append(f"не удалось прочитать файлы: {e}")
    return {"id": pid, "name": meta.get("name"), "created_by": meta.get("created_by"), "files": files,
            "slots": SLOTS, "missing": missing, "problems": problems, "notes": notes,
            "ready": not missing and not problems}


@app.post("/api/projects/new")
def new_project(request: Request, body: dict = Body(default={})):
    """Пустой проект — файлы добавляются по одному (PUT /api/projects/{id}/files/{slot})."""
    name = str(body.get("name") or "Проект").strip()[:200] or "Проект"
    pid, _ = storage.new_project_dir()
    storage.save_meta(pid, {"name": name, "created": time.time(), "files": {}, "orig_names": {}, "params": {},
                            "overrides": {}, "created_by": auth.current_user(request)["login"]})
    return _files_status(pid)


@app.get("/api/projects/{pid}/files")
def project_files(pid: str):
    return _files_status(pid)


@app.put("/api/projects/{pid}/files/{slot}")
async def put_file(pid: str, slot: str, file: UploadFile = File(...)):
    meta = _meta_or_404(pid)
    if slot not in SLOTS:
        raise HTTPException(400, f"неизвестный тип файла: {slot}")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SLOTS[slot]["ext"]:
        raise HTTPException(400, f"для поля «{SLOTS[slot]['label']}» нужен файл {' / '.join(SLOTS[slot]['ext'])}, "
                                 f"а выбран «{file.filename}»")
    d = storage.project_dir(pid) / "inputs"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{slot}.upload{ext}"
    tmp.write_bytes(await file.read())
    try:
        _check_single(slot, tmp)
    except ValueError as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, f"«{file.filename}»: {e}") from e
    old = (meta.get("files") or {}).get(slot)
    if old and (d / old).exists():
        (d / old).unlink()
    fn = f"{slot}{ext}"
    tmp.replace(d / fn)
    meta.setdefault("files", {})[slot] = fn
    meta.setdefault("orig_names", {})[slot] = Path(file.filename).name[:200]
    storage.save_meta(pid, meta)
    _CACHE.pop(pid, None)                     # входные данные поменялись — старый результат недействителен
    return _files_status(pid)


@app.delete("/api/projects/{pid}/files/{slot}")
def delete_file(pid: str, slot: str):
    meta = _meta_or_404(pid)
    fn = (meta.get("files") or {}).pop(slot, None)
    (meta.get("orig_names") or {}).pop(slot, None)
    if fn:
        (storage.project_dir(pid) / "inputs" / fn).unlink(missing_ok=True)
    storage.save_meta(pid, meta)
    _CACHE.pop(pid, None)
    return _files_status(pid)


@app.patch("/api/projects/{pid}")
def rename_project(pid: str, body: dict = Body(...)):
    meta = _meta_or_404(pid)
    name = str(body.get("name") or "").strip()[:200]
    if not name:
        raise HTTPException(400, "пустое название")
    meta["name"] = name
    storage.save_meta(pid, meta)
    if pid in _CACHE and "json" in _CACHE[pid]:
        _CACHE[pid]["json"]["project"]["name"] = name
    return {"id": pid, "name": name}


@app.delete("/api/projects/{pid}")
def remove_project(pid: str, request: Request):
    meta = _meta_or_404(pid)
    u = auth.current_user(request)
    if u["role"] != "admin" and meta.get("created_by") not in (None, u["login"]):
        raise HTTPException(403, "удалить проект может его автор или администратор")
    storage.delete_project(pid)
    _CACHE.pop(pid, None)
    return {"ok": True}


# ------------------------------------------------------------------ профили параметров
@app.get("/api/profiles")
def profiles():
    return [{"id": "builtin", "name": "ЛИ-2022 (по умолчанию)", "builtin": True}] + storage.list_param_profiles()


@app.get("/api/profiles/{prof_id}")
def profile(prof_id: str):
    if prof_id == "builtin":
        return {"id": "builtin", "name": "ЛИ-2022 (по умолчанию)", "builtin": True, "params": {}, "use_models": False}
    p = storage.get_param_profile(prof_id)
    if p is None:
        raise HTTPException(404, "профиль не найден")
    return p


@app.post("/api/profiles")
def save_profile(request: Request, body: dict = Body(...)):
    name = str(body.get("name") or "").strip()[:60]
    if not name:
        raise HTTPException(400, "укажите название профиля")
    if name.lower().startswith("ли-2022"):
        raise HTTPException(400, "это имя занято встроенным профилем — выберите другое")
    params = body.get("params") or {}
    try:
        load_norms(overrides=params)          # профиль должен собираться в корректные нормы
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"некорректные параметры: {e}") from e
    return storage.save_param_profile(name, params, bool(body.get("use_models")), auth.current_user(request)["login"])


@app.delete("/api/profiles/{prof_id}")
def delete_profile(prof_id: str, request: Request):
    p = storage.get_param_profile(prof_id)
    if p is None:
        raise HTTPException(404, "профиль не найден")
    u = auth.current_user(request)
    if u["role"] != "admin" and p.get("created_by") != u["login"]:
        raise HTTPException(403, "удалить профиль может его автор или администратор")
    storage.delete_param_profile(prof_id)
    return {"ok": True}


@app.get("/api/projects/{pid}/result")
def get_result(pid: str):
    return JSONResponse(_result(pid))


@app.post("/api/projects/{pid}/run")
def run_project(pid: str, body: dict = Body(default={})):
    meta = storage.load_meta(pid)
    if meta is None:
        raise HTTPException(404, "проект не найден")
    for k in ("params", "use_models", "llm", "profile_name"):
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
def decide(pid: str, request: Request, body: dict = Body(...)):
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
    storage.add_feedback(pid, frag, action, chosen, dec.get("target"), cands, meta.get("params") or {},
                         auth.current_user(request)["login"])
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
def feedback(request: Request, limit: int = 200):
    auth.require_admin(request)
    rows = storage.feedback_rows()[-limit:]
    for r in rows:
        r["candidates"] = len(json.loads(r["candidates"] or "[]"))
    return rows


@app.post("/api/models/reload")
def reload_models(request: Request):
    auth.require_admin(request)
    return get_models(reload=True).status()
