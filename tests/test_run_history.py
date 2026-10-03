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


def test_failed_runs_are_listed_but_cancelled_ones_are_not(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS", tmp_path)
    for name, status in (("a-done", "done"), ("b-error", "error"), ("c-cancelled", "cancelled"), ("d-queued", "queued")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "run.json").write_text(json.dumps({"id": name, "status": status, "created": 1}))
    c = TestClient(server.app)
    assert sorted(r["id"] for r in c.get("/api/runs").json()) == ["a-done", "b-error"]


def test_took_counts_from_the_start_of_execution(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr(server, "RUNS", tmp_path)
    run = {"id": "r1", "status": "running", "created": 100.0, "started": 160.0, "params": {}, "frames": []}
    job = {"run": run, "params": {}}
    monkeypatch.setattr(server.time, "time", lambda: 190.0)
    asyncio.run(server.finish_job(job, "done"))
    assert run["took"] == 30.0
    saved = json.loads((tmp_path / "r1" / "run.json").read_text())
    assert saved["took"] == 30.0 and saved["status"] == "done"
