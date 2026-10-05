import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture()
def env(monkeypatch, tmp_path):
    out = tmp_path / "output"
    (out / "InpaintStudio").mkdir(parents=True)
    runs = tmp_path / "runs"
    (runs / "r1").mkdir(parents=True)
    monkeypatch.setattr(server, "COMFY_OUTPUT", out)
    monkeypatch.setattr(server, "RUNS", runs)
    (out / "InpaintStudio" / "a.png").write_bytes(b"A")
    (runs / "r1" / "aligned.png").write_bytes(b"B")
    return TestClient(server.app), out, runs, tmp_path


def test_local_file(env):
    _, out, runs, _ = env
    assert server.local_file("/api/view?filename=a.png&subfolder=InpaintStudio&type=output") == (out / "InpaintStudio" / "a.png").resolve()
    assert server.local_file("/data/runs/r1/aligned.png?t=1") == (runs / "r1" / "aligned.png").resolve()
    for bad in ("/api/view?filename=a.png&subfolder=InpaintStudio&type=input",   # not a result
                "/api/view?filename=..%2F..%2Fx.png&subfolder=InpaintStudio",     # outside the output folder
                "/api/view?filename=missing.png&subfolder=InpaintStudio",
                "/etc/passwd"):
        with pytest.raises(server.HTTPException):
            server.local_file(bad)


def test_export_copies_and_numbers(env):
    c, _, _, tmp = env
    dest = tmp / "dest"
    dest.mkdir()
    items = [{"url": "/api/view?filename=a.png&subfolder=InpaintStudio&type=output", "name": "result.png"},
             {"url": "/data/runs/r1/aligned.png", "name": "result.png"}]
    r = c.post("/api/export", json={"folder": str(dest), "items": items})
    assert r.status_code == 200 and r.json()["saved"] == ["result.png", "result (2).png"]
    assert (dest / "result.png").read_bytes() == b"A" and (dest / "result (2).png").read_bytes() == b"B"
    assert c.post("/api/export", json={"folder": str(tmp / "nope"), "items": items}).status_code == 400
