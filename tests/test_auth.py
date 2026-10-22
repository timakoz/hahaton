import pytest
from fastapi.testclient import TestClient

from api import auth
from api.main import app
from forestmerge import settings

PW = "Test-pass-12345"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(settings, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setenv("FM_AUTH", "1")
    auth._fails.clear()
    auth.create_user("alice", PW, "admin")
    auth.create_user("bob", PW, "user")
    with TestClient(app) as c:
        yield c


def login(c, user="alice", pw=PW):
    return c.post("/api/login", json={"login": user, "password": pw})


def test_password_hash_roundtrip():
    h = auth.hash_password(PW)
    assert h.startswith("pbkdf2_sha256$600000$")
    assert auth.verify_password(PW, h) and not auth.verify_password(PW + "x", h)


def test_password_policy():
    with pytest.raises(ValueError):
        auth.check_password_policy("short")
    with pytest.raises(ValueError):
        auth.check_password_policy("onlylowercaseletters")
    auth.check_password_policy(PW)


def test_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/api/projects").status_code == 401
    r = client.get("/static/app.js", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/login").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_login_logout(client):
    assert login(client, pw="wrong-password-1").status_code == 401
    r = login(client)
    assert r.status_code == 200 and r.json()["role"] == "admin"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/api/me").json()["user"] == "alice"
    assert client.get("/api/projects").status_code == 200
    client.post("/api/logout")
    assert client.get("/api/projects").status_code == 401


def test_bruteforce_lock(client):
    for _ in range(auth.MAX_FAILS):
        assert login(client, pw="wrong-password-1").status_code == 401
    assert login(client).status_code == 429          # даже верный пароль — до конца блокировки


def test_foreign_origin_rejected(client):
    login(client)
    r = client.post("/api/projects/sample", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_admin_only(client):
    login(client, "bob")
    assert client.get("/api/feedback").status_code == 403
    client.post("/api/logout")
    login(client, "alice")
    assert client.get("/api/feedback").status_code == 200


def test_password_change_drops_sessions(client):
    login(client, "bob")
    assert client.get("/api/me").status_code == 200
    auth.set_password("bob", "New-pass-67890")
    assert client.get("/api/me").status_code == 401


def test_disabled_user(client):
    auth.set_disabled("bob", True)
    assert login(client, "bob").status_code == 401
