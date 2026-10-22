"""Авторизация: пользователи и сессии в SQLite (data/app.db), вход по логину и паролю.

* пароли — PBKDF2-HMAC-SHA256, 600 000 итераций, соль 16 байт (только стандартная библиотека);
* сессия — случайный токен 256 бит в cookie (HttpOnly, SameSite=Lax, Secure при HTTPS);
  в БД хранится только SHA-256 токена, сессию можно отозвать;
* перебор паролей: после 8 неудач за 15 минут вход для пары «IP + логин» блокируется на 15 минут;
* изменяющие запросы с чужим Origin отклоняются (дополнительно к SameSite).

Переменные окружения: FM_AUTH=0 — выключить (только для локальной разработки),
FM_SESSION_HOURS (12), FM_COOKIE_SECURE=auto|1|0.
Пользователи создаются из консоли: python3 -m api.users add <логин>
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from urllib.parse import quote, urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from forestmerge import settings

COOKIE = "fm_session"
PBKDF2_ITERS = 600_000
MIN_PASSWORD = 10
MAX_FAILS, FAIL_WINDOW, LOCK_SECONDS = 8, 15 * 60, 15 * 60
PUBLIC_PATHS = {"/login", "/api/login", "/api/logout", "/api/health", "/favicon.ico"}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    login TEXT PRIMARY KEY,
    pw_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',      -- user | admin
    created REAL NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    login TEXT NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    ip TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS sessions_login ON sessions(login);
"""


def enabled() -> bool:
    return os.environ.get("FM_AUTH", "1") != "0"


def session_ttl() -> int:
    return int(float(os.environ.get("FM_SESSION_HOURS", "12")) * 3600)


def _db() -> sqlite3.Connection:
    settings.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(settings.DB_PATH)
    con.executescript(SCHEMA)
    return con


# ------------------------------------------------------------------ пароли
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(got.hex(), dk)
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_password(secrets.token_hex(8))   # чтобы время ответа не выдавало, есть ли такой логин


def check_password_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"пароль короче {MIN_PASSWORD} символов")
    if password.isdigit() or password.lower() == password and password.isalpha():
        raise ValueError("пароль слишком простой: добавьте цифры/заглавные буквы/символы")


# ------------------------------------------------------------------ пользователи
def create_user(login: str, password: str, role: str = "user") -> None:
    login = login.strip()
    if not login or len(login) > 64:
        raise ValueError("логин: 1–64 символа")
    check_password_policy(password)
    with closing(_db()) as con, con:
        if con.execute("SELECT 1 FROM users WHERE login=?", (login,)).fetchone():
            raise ValueError(f"пользователь {login} уже есть")
        con.execute("INSERT INTO users(login, pw_hash, role, created) VALUES (?,?,?,?)",
                    (login, hash_password(password), role, time.time()))


def set_password(login: str, password: str) -> None:
    check_password_policy(password)
    with closing(_db()) as con, con:
        n = con.execute("UPDATE users SET pw_hash=? WHERE login=?", (hash_password(password), login)).rowcount
        con.execute("DELETE FROM sessions WHERE login=?", (login,))      # смена пароля завершает все сессии
    if not n:
        raise ValueError(f"нет пользователя {login}")


def delete_user(login: str) -> None:
    with closing(_db()) as con, con:
        n = con.execute("DELETE FROM users WHERE login=?", (login,)).rowcount
        con.execute("DELETE FROM sessions WHERE login=?", (login,))
    if not n:
        raise ValueError(f"нет пользователя {login}")


def set_disabled(login: str, disabled: bool) -> None:
    with closing(_db()) as con, con:
        con.execute("UPDATE users SET disabled=? WHERE login=?", (1 if disabled else 0, login))
        if disabled:
            con.execute("DELETE FROM sessions WHERE login=?", (login,))


def list_users() -> list[dict]:
    with closing(_db()) as con:
        rows = con.execute("SELECT u.login, u.role, u.created, u.disabled, "
                           "(SELECT COUNT(*) FROM sessions s WHERE s.login=u.login AND s.expires>?) "
                           "FROM users u ORDER BY u.login", (time.time(),)).fetchall()
    return [{"login": r[0], "role": r[1], "created": r[2], "disabled": bool(r[3]), "sessions": r[4]} for r in rows]


def user_count() -> int:
    with closing(_db()) as con:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def authenticate(login: str, password: str) -> dict | None:
    with closing(_db()) as con:
        row = con.execute("SELECT login, pw_hash, role, disabled FROM users WHERE login=?", (login,)).fetchone()
    if row is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, row[1]) or row[3]:
        return None
    return {"login": row[0], "role": row[2]}


# ------------------------------------------------------------------ сессии
def _th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(login: str, ip: str | None, ua: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with closing(_db()) as con, con:
        con.execute("DELETE FROM sessions WHERE expires<?", (now,))
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                    (_th(token), login, now, now + session_ttl(), ip, (ua or "")[:200]))
    return token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    with closing(_db()) as con:
        row = con.execute("SELECT s.login, u.role, s.expires FROM sessions s JOIN users u ON u.login=s.login "
                          "WHERE s.token_hash=? AND u.disabled=0", (_th(token),)).fetchone()
    if row is None or row[2] < time.time():
        return None
    return {"login": row[0], "role": row[1]}


def drop_session(token: str | None) -> None:
    if token:
        with closing(_db()) as con, con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (_th(token),))


def revoke_sessions(login: str | None = None) -> int:
    with closing(_db()) as con, con:
        if login:
            return con.execute("DELETE FROM sessions WHERE login=?", (login,)).rowcount
        return con.execute("DELETE FROM sessions").rowcount


# ------------------------------------------------------------------ защита от перебора
_fails: dict[tuple[str, str], list[float]] = {}
_flock = threading.Lock()


def client_ip(request: Request) -> str:
    # за nginx реальный адрес приходит в X-Real-IP / X-Forwarded-For (uvicorn --proxy-headers доверяет 127.0.0.1)
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


def locked(ip: str, login: str) -> int:
    """Сколько секунд ещё действует блокировка (0 — нет)."""
    now = time.time()
    with _flock:
        ts = [t for t in _fails.get((ip, login), []) if now - t < FAIL_WINDOW]
        _fails[(ip, login)] = ts
        if len(ts) >= MAX_FAILS:
            return int(LOCK_SECONDS - (now - ts[-1])) + 1 if now - ts[-1] < LOCK_SECONDS else 0
    return 0


def register_fail(ip: str, login: str) -> None:
    with _flock:
        _fails.setdefault((ip, login), []).append(time.time())


def clear_fails(ip: str, login: str) -> None:
    with _flock:
        _fails.pop((ip, login), None)


# ------------------------------------------------------------------ cookie и middleware
def is_https(request: Request) -> bool:
    mode = os.environ.get("FM_COOKIE_SECURE", "auto")
    if mode in ("0", "1"):
        return mode == "1"
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"


def set_cookie(resp: Response, request: Request, token: str) -> None:
    resp.set_cookie(COOKIE, token, max_age=session_ttl(), httponly=True, samesite="lax", secure=is_https(request),
                    path="/")


def clear_cookie(resp: Response) -> None:
    resp.delete_cookie(COOKIE, path="/")


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True                     # не браузерный клиент (curl, скрипт) — cookie ему всё равно нужна
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return urlparse(origin).netloc == host


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not enabled():
            request.state.user = {"login": "local", "role": "admin"}
            return await call_next(request)
        path = request.url.path
        if request.method in UNSAFE and not _same_origin(request):
            return JSONResponse({"detail": "запрос с чужого сайта отклонён"}, status_code=403)
        if path in PUBLIC_PATHS:
            return await call_next(request)
        user = session_user(request.cookies.get(COOKIE))
        if user is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "нужно войти"}, status_code=401)
            nxt = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
        request.state.user = user
        return await call_next(request)


def current_user(request: Request) -> dict:
    return getattr(request.state, "user", None) or {"login": "?", "role": "user"}


def require_admin(request: Request) -> None:
    from fastapi import HTTPException
    if current_user(request).get("role") != "admin":
        raise HTTPException(403, "только для администратора")
