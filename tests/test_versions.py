import json

import pytest
from fastapi.testclient import TestClient

import server


def view(name: str) -> str:
    return f"/api/view?filename={name}&subfolder=InpaintStudio&type=output"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    out = tmp_path / "output" / "InpaintStudio"
    out.mkdir(parents=True)
    runs = tmp_path / "runs"
    monkeypatch.setattr(server, "COMFY_OUTPUT", tmp_path / "output")
    monkeypatch.setattr(server, "RUNS", runs)
    for name in ("r.png", "r_raw.png", "r_fixed.png"):
        (out / name).write_bytes(b"x")

    def make(**run):
        (runs / "r").mkdir(parents=True, exist_ok=True)
        (runs / "r" / "run.json").write_text(json.dumps({"id": "r", "status": "done", **run}))
    return TestClient(server.app), out, make


def test_delete_raw_keeps_the_rest(env):
    c, out, make = env
    make(result_url=view("r.png"), raw_url=view("r_raw.png"), fixed_url=view("r_fixed.png") + "&t=1")
    r = c.post("/api/runs/r/delete-version", json={"kind": "raw"})
    assert r.status_code == 200 and r.json()["raw_url"] is None
    assert not (out / "r_raw.png").exists() and (out / "r.png").exists() and (out / "r_fixed.png").exists()


def test_delete_result_promotes_raw(env):
    c, out, make = env
    make(result_url=view("r.png"), raw_url=view("r_raw.png"), filename="r.png")
    run = c.post("/api/runs/r/delete-version", json={"kind": "result"}).json()
    assert run["result_url"] == view("r_raw.png") and run["raw_url"] is None and run["filename"] == "r_raw.png"
    assert not (out / "r.png").exists()
    # the last version only goes with the whole run
    assert c.post("/api/runs/r/delete-version", json={"kind": "result"}).status_code == 400


def test_delete_post_and_clean_upscale(env):
    c, out, make = env
    # an upscale with grain: the clean upscale is both result and raw, the grain is in _fixed
    make(result_url=view("r.png"), raw_url=view("r.png"), fixed_url=view("r_fixed.png"), grain=True)
    assert server.run_versions(json.loads((server.RUNS / "r" / "run.json").read_text())) == ["post", "result"]
    run = c.post("/api/runs/r/delete-version", json={"kind": "result"}).json()
    assert run["result_url"] == view("r_fixed.png") and run["raw_url"] is None and "fixed_url" not in run
    make(result_url=view("r.png"), fixed_url=view("r_fixed.png"))
    (out / "r.png").write_bytes(b"x")
    run = c.post("/api/runs/r/delete-version", json={"kind": "post"}).json()
    assert "fixed_url" not in run and not (out / "r_fixed.png").exists()
    assert c.post("/api/runs/r/delete-version", json={"kind": "raw"}).status_code == 400
