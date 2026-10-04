"""Job lifecycle edge cases: finish once, interrupted or empty history, flaky polls, delete while running."""
import asyncio

from fastapi.testclient import TestClient

import server
from test_reattach import _no_sleep, fake_comfy, read, reattach_and_wait, stored_run


def test_finish_job_runs_once(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS", tmp_path)
    stored_run(tmp_path, "r1")
    job = {"run": read(tmp_path / "r1"), "params": {}, "graph": {}}
    server.JOBS["r1"] = job

    async def go():
        await server.finish_job(job, "cancelled", error="Removed from queue")
        await server.finish_job(job, "done", result_url="/x.png")
    asyncio.run(go())
    run = read(tmp_path / "r1")
    assert run["status"] == "cancelled" and "result_url" not in run


def test_interrupted_history_is_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS", tmp_path)
    d = stored_run(tmp_path, "r1")
    fake_comfy(monkeypatch, history={"p1": {"outputs": {}, "status": {"messages": [["execution_interrupted", {}]]}}})
    asyncio.run(reattach_and_wait())
    assert read(d)["status"] == "cancelled"


def test_history_without_result_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS", tmp_path)
    d = stored_run(tmp_path, "r1")
    fake_comfy(monkeypatch, history={"p1": {"outputs": {}}})
    asyncio.run(reattach_and_wait())
    run = read(d)
    assert run["status"] == "error" and "without a result" in run["error"]


def test_follow_survives_a_few_failed_polls(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS", tmp_path)
    monkeypatch.setattr(server, "drop_counter", lambda img: img)
    monkeypatch.setattr(server.asyncio, "sleep", _no_sleep())
    d = stored_run(tmp_path, "r1")
    fake_comfy(monkeypatch, history={"p1": {"outputs": {"out_result": {"images": [
        {"filename": "r1.png", "subfolder": "InpaintStudio", "type": "output"}]}}}})
    orig, calls = server.comfy_json, {"n": 0}

    async def flaky(method, path, **kw):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise server.HTTPException(502, "ComfyUI busy")
        return await orig(method, path, **kw)
    monkeypatch.setattr(server, "comfy_json", flaky)
    asyncio.run(reattach_and_wait())
    assert read(d)["status"] == "done"


def test_delete_refuses_a_running_run(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS", tmp_path)
    d = stored_run(tmp_path, "r1")
    (d / "sub").mkdir()
    server.JOBS["r1"] = {"run": read(d)}
    c = TestClient(server.app)
    try:
        assert c.delete("/api/runs/r1").status_code == 409
    finally:
        server.JOBS.pop("r1", None)
    assert c.delete("/api/runs/r1").status_code == 200 and not d.exists()


def test_invalid_job_is_a_400():
    c = TestClient(server.app)
    r = c.post("/api/jobs", json={"prompt": "x", "steps": 0, "megapixels": 1, "resolution": 1008, "src_w": 10, "src_h": 10,
                                  "image": "a.png"})
    assert r.status_code == 400 and "steps" in r.json()["detail"]
    r = c.post("/api/jobs", json={"prompt": "x", "steps": 4, "megapixels": 1, "resolution": 1008})
    assert r.status_code == 400 and "src_w" in r.json()["detail"]
    r = c.post("/api/jobs", json={"prompt": "x", "steps": 4, "megapixels": 1, "resolution": 1008, "src_w": 10, "src_h": 10})
    assert r.status_code == 400 and "image" in r.json()["detail"]


def test_upload_of_a_non_image_is_a_400():
    c = TestClient(server.app)
    r = c.post("/api/upload", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400 and "notes.txt" in r.json()["detail"]
