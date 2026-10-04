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


def test_thumb_is_small_cached_and_refuses_other_urls(tmp_path, monkeypatch):
    from PIL import Image
    runs = tmp_path / "runs"
    (runs / "r1").mkdir(parents=True)
    Image.new("RGB", (2000, 1000), (200, 10, 10)).save(runs / "r1" / "aligned.png")
    monkeypatch.setattr(server, "RUNS", runs)
    monkeypatch.setattr(server, "THUMBS", tmp_path / "thumbs")
    c = TestClient(server.app)
    r = c.get("/api/thumb", params={"src": "/data/runs/r1/aligned.png?t=1"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and "immutable" in r.headers["cache-control"]
    assert len(list((tmp_path / "thumbs").iterdir())) == 1
    assert c.get("/api/thumb", params={"src": "/data/runs/../secret.png"}).status_code == 404
    assert c.get("/api/thumb", params={"src": "https://example.com/x.png"}).status_code == 400


def test_prompt_history_dedupes_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS", tmp_path / "runs")
    for t in ("a", "b", "a", "  ", "c"):
        server.remember_prompt("prompts", t)
    server.remember_prompt("masks", "person")
    h = TestClient(server.app).get("/api/prompt-history").json()
    assert h == {"prompts": ["c", "a", "b"], "masks": ["person"]}


def test_comfyui_png_carries_the_graph(tmp_path, monkeypatch):
    import io
    import json
    from PIL import Image
    monkeypatch.setattr(server, "RUNS", tmp_path)
    d = stored_run(tmp_path, "r1", status="done")
    run = read(d) | {"result_url": "/api/view?filename=r1.png&subfolder=InpaintStudio&type=output"}
    (d / "run.json").write_text(json.dumps(run))

    async def fetch(url):
        return Image.new("RGB", (8, 8))
    monkeypatch.setattr(server, "_fetch_view", fetch)
    r = TestClient(server.app).get("/api/runs/r1/comfyui.png")
    assert r.status_code == 200
    graph = json.loads(Image.open(io.BytesIO(r.content)).text["prompt"])
    assert "out_result" in graph


def test_edit_upscale_becomes_a_follow_up_run(monkeypatch):
    calls = []

    async def fetch(url):
        from PIL import Image
        return Image.new("RGB", (8, 8))

    async def upload(data, name, sub):
        return f"{sub}/{name}"

    async def upscale(req):
        calls.append(req)
    monkeypatch.setattr(server, "_fetch_view", fetch)
    monkeypatch.setattr(server, "upload_to_comfy", upload)
    monkeypatch.setattr(server, "upscale", upscale)
    run = {"id": "r1", "status": "done", "result_url": "/api/view?filename=r1.png", "fixed_url": "/api/view?filename=r1_fixed.png"}
    asyncio.run(server.follow_up_upscale(run, {"then_upscale": {"upscaler": "up_x", "factor": 2, "color_correction": "lab"}}))
    assert len(calls) == 1 and calls[0].upscale_of == "r1" and calls[0].image == "inpaint-studio/r1_for_upscale.png"
    asyncio.run(server.follow_up_upscale({**run, "status": "error"}, {"then_upscale": {"upscaler": "up_x", "factor": 2,
                                                                                        "color_correction": "lab"}}))
    assert len(calls) == 1
