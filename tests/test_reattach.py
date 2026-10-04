"""Runs left unfinished by an earlier server process: reattach, fail or retry."""
import asyncio
import json

from fastapi.testclient import TestClient

import server


def stored_run(tmp_path, run_id, status="running", prompt_id="p1", job=True):
    d = tmp_path / run_id
    d.mkdir()
    (d / "run.json").write_text(json.dumps({"id": run_id, "status": status, "created": 1, "prompt_id": prompt_id,
                                            "params": {"prompt": "x"}, "frames": []}))
    if job:
        (d / "job.json").write_text(json.dumps({"params": {"prefix": f"InpaintStudio/{run_id}", "task": "edit",
                                                           "image": f"inpaint-studio/{run_id}_crop.png"},
                                                "graph": {"out_result": {"inputs": {"filename_prefix": f"InpaintStudio/{run_id}"}}}}))
    return d


def fake_comfy(monkeypatch, history=None, running=(), pending=()):
    async def comfy_json(method, path, **kw):
        if path.startswith("/history/"):
            pid = path.rsplit("/", 1)[1]
            return {pid: history[pid]} if history and pid in history else {}
        if path == "/queue":
            return {"queue_running": [[0, p] for p in running], "queue_pending": [[0, p] for p in pending]}
        return {}

    async def up():
        return True
    monkeypatch.setattr(server, "comfy_json", comfy_json)
    monkeypatch.setattr(server, "comfy_up", up)


def read(d):
    return json.loads((d / "run.json").read_text())


async def reattach_and_wait():
    await server.reattach_runs()
    tasks = [j["task"] for j in server.JOBS.values()]
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)


def test_finished_while_away_is_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS", tmp_path)
    monkeypatch.setattr(server, "drop_counter", lambda img: img)
    d = stored_run(tmp_path, "r1")
    fake_comfy(monkeypatch, history={"p1": {"outputs": {"out_result": {"images": [
        {"filename": "r1.png", "subfolder": "InpaintStudio", "type": "output"}]}}}})
    asyncio.run(reattach_and_wait())
    run = read(d)
    assert run["status"] == "done" and "r1.png" in run["result_url"]


def test_still_queued_is_followed_until_it_disappears(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS", tmp_path)
    monkeypatch.setattr(server.asyncio, "sleep", _no_sleep())
    d = stored_run(tmp_path, "r2", status="queued")
    fake_comfy(monkeypatch, pending=["p1"])
    calls = {"n": 0}
    orig = server.comfy_json

    async def comfy_json(method, path, **kw):   # pending twice, then gone without history
        if path == "/queue":
            calls["n"] += 1
            if calls["n"] > 2:
                return {"queue_running": [], "queue_pending": []}
        return await orig(method, path, **kw)
    monkeypatch.setattr(server, "comfy_json", comfy_json)

    async def go():
        await server.reattach_runs()
        assert server.JOBS["r2"]["reattached"] and server.job_summary(server.JOBS["r2"])["reattached"]
        await asyncio.wait_for(server.JOBS["r2"]["task"], 5)
    asyncio.run(go())
    assert read(d)["status"] == "error" and calls["n"] == 3


def test_unknown_or_without_job_file_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS", tmp_path)
    a = stored_run(tmp_path, "r3", job=False)
    b = stored_run(tmp_path, "r4", status="done")
    fake_comfy(monkeypatch)
    asyncio.run(server.reattach_runs())
    assert read(a)["status"] == "error" and "restarted" in read(a)["error"]
    assert read(b)["status"] == "done"


def test_retry_queues_a_copy_with_a_new_id(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS", tmp_path)
    stored_run(tmp_path, "20260101-000000-abcd", status="error")
    started = {}

    def start_job(run, params, graph):
        started.update(run=run, params=params, graph=graph)
        return {"job_id": run["id"]}
    monkeypatch.setattr(server, "start_job", start_job)
    monkeypatch.setattr(server, "save_run_config", lambda *a: None)
    c = TestClient(server.app)
    new_id = c.post("/api/runs/20260101-000000-abcd/retry").json()["job_id"]
    assert new_id != "20260101-000000-abcd"
    assert started["params"]["prefix"] == f"InpaintStudio/{new_id}"
    assert started["graph"]["out_result"]["inputs"]["filename_prefix"] == f"InpaintStudio/{new_id}"
    # crop & stitch: the crop uploaded for the old run keeps its name (nothing uploads one for the new id)
    assert started["params"]["image"] == "inpaint-studio/20260101-000000-abcd_crop.png"
    assert c.post("/api/runs/nope/retry").status_code == 404


def _no_sleep():
    real = asyncio.sleep

    async def sleep(_):
        await real(0)
    return sleep
