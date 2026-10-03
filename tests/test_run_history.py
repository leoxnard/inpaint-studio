import json

from fastapi.testclient import TestClient

import server


def test_hide_and_restore_a_run_keeps_its_files(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS", tmp_path)
    d = tmp_path / "20260101-000000-abcd"
    d.mkdir()
    (d / "run.json").write_text(json.dumps({"id": d.name, "status": "done", "created": 1}))
    (d / "live_001.jpg").write_bytes(b"x")
    c = TestClient(server.app)   # no context manager: startup (ComfyUI) is not run

    assert [r["id"] for r in c.get("/api/runs").json()] == [d.name]
    assert c.post(f"/api/runs/{d.name}/hide").status_code == 200
    assert c.get("/api/runs").json() == []
    assert [r["id"] for r in c.get("/api/runs?hidden=1").json()] == [d.name]

    assert c.post(f"/api/runs/{d.name}/restore").status_code == 200
    assert [r["id"] for r in c.get("/api/runs").json()] == [d.name]
    assert c.get("/api/runs?hidden=1").json() == [] and (d / "live_001.jpg").exists()
    assert c.post("/api/runs/nope/restore").status_code == 404
