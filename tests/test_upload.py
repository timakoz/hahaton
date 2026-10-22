import pytest
from fastapi.testclient import TestClient

from api.main import app
from forestmerge import settings
from forestmerge.settings import find_geo_extra, find_sample

S = find_sample()
pytestmark = pytest.mark.skipif(S["network"] is None, reason="нет файлов образца 5_x")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(settings, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setenv("FM_AUTH", "0")
    with TestClient(app) as c:
        yield c


def upload(c, network, lesoseki, taxation=None, volumes=None):
    files = {"network": (network.name, network.read_bytes()), "lesoseki": (lesoseki.name, lesoseki.read_bytes())}
    if taxation:
        files["taxation"] = (taxation.name, taxation.read_bytes())
    if volumes:
        files["volumes"] = (volumes.name, volumes.read_bytes())
    return c.post("/api/projects", data={"name": "t"}, files=files)


def test_sample_upload_ok(client):
    r = upload(client, S["network"], S["lesoseki"], S["taxation"], S["volumes"])
    assert r.status_code == 200, r.text


def test_swapped_network_and_lesoseki(client):
    r = upload(client, S["lesoseki"], S["network"], S["taxation"])
    assert r.status_code == 400 and "лесосек" in r.json()["detail"]


@pytest.mark.skipif(find_geo_extra()["lesoseki1"] is None, reason="нет Лесосеки-1.geojson")
def test_lesoseki1_as_network(client):
    r = upload(client, find_geo_extra()["lesoseki1"], S["lesoseki"], S["taxation"])
    assert r.status_code == 400
    assert "лесосек" in r.json()["detail"] or "не один квартал" in r.json()["detail"]
