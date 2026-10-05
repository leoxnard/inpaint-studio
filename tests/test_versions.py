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
    (runs / "r").mkdir(parents=True)
    monkeypatch.setattr(server, "COMFY_OUTPUT", tmp_path / "output")
    monkeypatch.setattr(server, "RUNS", runs)
    for name in ("r.png", "r_raw.png", "r_fixed.png"):
        (out / name).write_bytes(b"x")
    (runs / "r" / "raw.png").write_bytes(b"x")

    def make(**run):
        (runs / "r" / "run.json").write_text(json.dumps({"id": "r", "status": "done", "filename": "r.png", **run}))
        return server.run_versions(json.loads((runs / "r" / "run.json").read_text()))
    return TestClient(server.app), out, runs / "r", make


def delete(c, kind):
    return c.post("/api/runs/r/delete-version", json={"kind": kind})


def test_paste_has_three_images(env):
    c, out, d, make = env
    # a saved paste: corrected pasted result, corrected whole image, untouched raw in the run dir
    assert make(result_url=view("r.png"), whole_url=view("r_raw.png"), raw_url="/data/runs/r/raw.png") == ["result", "whole", "raw"]
    run = delete(c, "whole").json()
    assert "whole_url" not in run and not (out / "r_raw.png").exists()
    run = delete(c, "result").json()   # the untouched raw image is left and becomes the result
    assert run["result_url"] == "/data/runs/r/raw.png" and run["raw_url"] is None and run["filename"] == "r.png"
    assert delete(c, "result").status_code == 400   # the last image goes only with the run


def test_delete_result_promotes_whole(env):
    c, out, d, make = env
    make(result_url=view("r.png"), whole_url=view("r_raw.png"), raw_url="/data/runs/r/raw.png")
    run = delete(c, "result").json()
    assert run["result_url"] == view("r_raw.png") and "whole_url" not in run and run["filename"] == "r_raw.png"
    assert not (out / "r.png").exists()
    run = delete(c, "raw").json()
    assert run["raw_url"] is None and not (d / "raw.png").exists()


def test_post_processed_result_keeps_source(env):
    c, out, d, make = env
    (d / "source.png").write_bytes(b"x")
    assert make(result_url=view("r.png"), source_url="/data/runs/r/source.png") == ["result", "raw"]
    run = delete(c, "raw").json()
    assert run["source_url"] is None and not (d / "source.png").exists() and (out / "r.png").exists()


def test_older_runs(env):
    c, out, d, make = env
    # an older upscale with grain: the clean upscale is result and raw, the grain is in _fixed
    assert make(result_url=view("r.png"), raw_url=view("r.png"), fixed_url=view("r_fixed.png")) == ["result", "raw"]
    run = delete(c, "raw").json()
    assert run["result_url"] == view("r_fixed.png") and run["raw_url"] is None and "fixed_url" not in run
    # an older paste: deleting the post-processed file shows the plain result again
    (out / "r_fixed.png").write_bytes(b"x")
    make(result_url=view("r_raw.png"), raw_url="/data/runs/r/raw.png", fixed_url=view("r_fixed.png"))
    run = delete(c, "result").json()
    assert "fixed_url" not in run and run["result_url"] == view("r_raw.png") and not (out / "r_fixed.png").exists()
    assert delete(c, "whole").status_code == 400


def test_save_post_files_keeps_two_output_files(env):
    import asyncio
    from PIL import Image
    _, out, d, _ = env
    (out / "r_fixed.png").write_bytes(b"x")
    img = lambda v: Image.new("RGB", (4, 4), (v, v, v))
    run = {"id": "r", "filename": "r.png", "result_url": view("r.png"), "raw_url": view("r_raw.png"),
           "fixed_url": view("r_fixed.png"), "aligned": {"dx": 1, "url": "/data/runs/r/aligned.png"}}
    asyncio.run(server.save_post_files(run, "paste", img(1), img(2), img(3), 7))
    assert sorted(p.name for p in out.iterdir()) == ["r.png", "r_raw.png"]   # corrected pasted + corrected whole
    assert run["raw_url"] == "/data/runs/r/raw.png" and Image.open(d / "raw.png").getpixel((0, 0)) == (1, 1, 1)
    assert Image.open(out / "r.png").getpixel((0, 0)) == (2, 2, 2) and Image.open(out / "r_raw.png").getpixel((0, 0)) == (3, 3, 3)
    assert run["whole_url"].endswith("&t=7") and "fixed_url" not in run and "url" not in run["aligned"]
    # a later save keeps the untouched raw image where it is
    asyncio.run(server.save_post_files(run, "paste", img(9), img(4), img(5), 8))
    assert Image.open(d / "raw.png").getpixel((0, 0)) == (1, 1, 1) and Image.open(out / "r.png").getpixel((0, 0)) == (4, 4, 4)
