import json

import pytest
from fastapi.testclient import TestClient

import server

LOCAL = {"Origin": "http://127.0.0.1:7380"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "BENCH", tmp_path / "benchmarks")
    return TestClient(server.app, base_url="http://127.0.0.1:7380"), tmp_path / "benchmarks"


def test_empty_then_save_and_load(client):
    c, bench = client
    assert c.get("/api/benchmarks").status_code == 404
    data = {"title": "Benchmarks", "chapters": [{"id": "a", "blocks": []}]}
    assert c.post("/api/benchmarks", json=data, headers=LOCAL).status_code == 200
    assert c.get("/api/benchmarks").json() == data
    data["title"] = "Changed"
    c.post("/api/benchmarks", json=data, headers=LOCAL)
    assert json.loads((bench / "data.json.bak").read_text())["title"] == "Benchmarks"   # previous version kept
    assert c.get("/api/benchmarks").json()["title"] == "Changed"


def test_rejects_other_data_and_foreign_origin(client):
    c, _ = client
    assert c.post("/api/benchmarks", json={"title": "x"}, headers=LOCAL).status_code == 400
    assert c.post("/api/benchmarks", json={"chapters": []}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_pictures_stay_inside_the_folder(client):
    c, bench = client
    (bench / "img").mkdir(parents=True)
    (bench / "img" / "a.jpg").write_bytes(b"JPG")
    (bench.parent / "secret.txt").write_text("no")
    assert c.get("/data/benchmarks/img/a.jpg").content == b"JPG"
    assert c.get("/data/benchmarks/..%2Fsecret.txt").status_code == 404
    assert c.get("/data/benchmarks/img/missing.jpg").status_code == 404
