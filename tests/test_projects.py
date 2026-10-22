import pytest
from fastapi.testclient import TestClient

from api.main import app
from forestmerge import settings
from forestmerge.settings import find_sample

S = find_sample()
pytestmark = pytest.mark.skipif(S["network"] is None, reason="нет файлов образца 5_x")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(settings, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(settings, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setenv("FM_AUTH", "0")
    with TestClient(app) as c:
        yield c


def put(c, pid, slot, path, name=None):
    return c.put(f"/api/projects/{pid}/files/{slot}", files={"file": (name or path.name, path.read_bytes())})


def test_files_one_by_one(client):
    st = client.post("/api/projects/new", json={"name": "по одному"}).json()
    pid = st["id"]
    assert set(st["missing"]) == {"network", "lesoseki"} and not st["ready"]

    r = put(client, pid, "network", S["volumes"])                     # не то расширение
    assert r.status_code == 400 and "нужен файл" in r.json()["detail"]
    r = put(client, pid, "network", S["lesoseki"])                    # лесосеки вместо сети
    assert r.status_code == 400 and "лесосек" in r.json()["detail"]

    assert put(client, pid, "network", S["network"]).json()["missing"] == ["lesoseki"]
    st = put(client, pid, "lesoseki", S["lesoseki"]).json()
    assert st["ready"] and st["files"]["lesoseki"]["name"] == S["lesoseki"].name
    st = put(client, pid, "taxation", S["taxation"]).json()
    assert st["ready"] and not st["problems"]
    assert put(client, pid, "volumes", S["volumes"]).json()["ready"]

    r = client.post(f"/api/projects/{pid}/run", json={"params": {}})
    assert r.status_code == 200 and r.json()["metrics"]["n_small_after"] == 0

    st = client.delete(f"/api/projects/{pid}/files/lesoseki").json()
    assert st["missing"] == ["lesoseki"] and not st["ready"]
    r = client.get(f"/api/projects/{pid}/result")
    assert r.status_code == 400 and "не хватает файлов" in r.json()["detail"]

    assert client.patch(f"/api/projects/{pid}", json={"name": "новое имя"}).json()["name"] == "новое имя"
    assert client.delete(f"/api/projects/{pid}").json()["ok"]
    assert client.get(f"/api/projects/{pid}/files").status_code == 404


def test_bad_project_id(client):
    assert client.get("/api/projects/..%2F..%2Fetc/files").status_code == 404
    assert client.delete("/api/projects/notavalidid").status_code == 404


def test_param_profiles(client):
    lst = client.get("/api/profiles").json()
    assert lst[0]["builtin"]
    assert client.post("/api/profiles", json={"name": "ЛИ-2022 копия", "params": {}}).status_code == 400
    p = client.post("/api/profiles", json={"name": "Разряд I, строго",
                                           "params": {"rank": 1, "tolerances": {"method": "глазомерно-измерительный"}},
                                           "use_models": True}).json()
    assert p["name"] == "Разряд I, строго" and p["params"]["rank"] == 1
    # то же имя — обновление, а не дубль
    p2 = client.post("/api/profiles", json={"name": "Разряд I, строго", "params": {"rank": 1, "scoring": "hybrid"}}).json()
    assert p2["id"] == p["id"] and p2["params"]["scoring"] == "hybrid"
    assert len(client.get("/api/profiles").json()) == 2
    assert client.get(f"/api/profiles/{p['id']}").json()["params"]["rank"] == 1
    assert client.post("/api/profiles", json={"name": "битый", "params": {"rank": "abc"}}).status_code == 400
    assert client.delete(f"/api/profiles/{p['id']}").json()["ok"]
    assert len(client.get("/api/profiles").json()) == 1
