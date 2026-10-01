"""Queue mechanics against the real local ComfyUI, using a tiny CPU-only graph (no sampling).

Skipped when ComfyUI is not reachable. Uses a temporary data dir, so the real run history
is not touched.
"""
import os
import tempfile
import time

import httpx
import pytest

COMFY = os.environ.get("COMFY_URL") or "http://127.0.0.1:8188"
TEST_IMAGE = "B491A4F1-7B92-4FA5-AA43-681AE15CB30C_1_105_c.jpeg"


def comfy_up() -> bool:
    try:
        return httpx.get(f"{COMFY}/queue", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not comfy_up(), reason="ComfyUI not running")


@pytest.fixture()
def app_client(monkeypatch):
    os.environ["INPAINT_STUDIO_DATA"] = tempfile.mkdtemp(prefix="inpaint-studio-test-")
    import importlib

    import graphs
    import server
    importlib.reload(server)

    def tiny_graph(p):
        return {
            "load": {"class_type": "LoadImage", "inputs": {"image": TEST_IMAGE}},
            "scale": {"class_type": "ImageScale", "inputs": {"image": ["load", 0], "upscale_method": "area",
                                                             "width": 64, "height": 64, "crop": "disabled"}},
            "out_result": {"class_type": "SaveImage", "inputs": {"images": ["scale", 0], "filename_prefix": "InpaintStudioTest/q"}},
        }

    monkeypatch.setattr(graphs, "build_edit_graph", tiny_graph)
    from fastapi.testclient import TestClient
    with TestClient(server.app) as c:
        yield c, server


def params(seed):
    return {"src_w": 1182, "src_h": 665, "megapixels": 0.95, "resolution": 1008, "prompt": f"queue test {seed}",
            "steps": 1, "seed": seed, "image": TEST_IMAGE}


def test_jobs_queue_run_and_cancel(app_client):
    c, server = app_client
    ids = [c.post("/api/jobs", json=params(i)).json()["job_id"] for i in range(3)]
    assert len(set(ids)) == 3
    c.post(f"/api/jobs/{ids[2]}/cancel")  # may already be done (tiny graph is fast) -> 404 is fine
    deadline = time.time() + 60
    while c.get("/api/jobs").json() and time.time() < deadline:
        time.sleep(0.3)
    assert c.get("/api/jobs").json() == []
    runs = {r["id"]: r for r in c.get("/api/runs").json()}
    assert ids[0] in runs and ids[1] in runs
    assert runs[ids[0]]["result_url"] and runs[ids[0]]["status"] == "done"
