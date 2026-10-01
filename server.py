"""Inpaint Studio - local two-step mask + Qwen-Image 2.1 edit UI for ComfyUI.

Step 1 runs only SAM3 and returns the mask, so it can be tuned (and painted) quickly.
Step 2 runs the edit; ComfyUI's per-step latent previews are relayed to the browser.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import struct
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import websockets
from PIL import Image
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import graphs

ROOT = Path(__file__).parent
WEB = ROOT / "web"
COMFY = os.environ.get("COMFY_URL") or "http://127.0.0.1:8188"
COMFY_WS = COMFY.replace("http", "ws", 1) + "/ws"
SUBFOLDER = "inpaint-studio"
RUNS = Path(os.environ.get("INPAINT_STUDIO_DATA") or ROOT / "data") / "runs"
RUNS.mkdir(parents=True, exist_ok=True)
HISTORY_PARAMS = ("prompt", "negative", "mode", "use_mask", "steps", "denoise", "seed", "cfg", "sampler",
                  "scheduler", "feather", "megapixels", "resolution", "save_every", "unet", "keep_identical")

app = FastAPI(title="Inpaint Studio")
client = httpx.AsyncClient(base_url=COMFY, timeout=60)
current_prompt: dict[str, str | None] = {"id": None}


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


async def comfy_json(method: str, path: str, **kw) -> Any:
    try:
        r = await client.request(method, path, **kw)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"ComfyUI not reachable at {COMFY}: {e}") from e
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:2000])
    return r.json() if r.content else {}


async def submit(graph: dict, client_id: str, extra: dict | None = None) -> str:
    body = {"prompt": graph, "client_id": client_id}
    if extra:
        body["extra_data"] = extra
    res = await comfy_json("POST", "/prompt", json=body)
    if res.get("node_errors"):
        raise HTTPException(400, json.dumps(res["node_errors"])[:2000])
    return res["prompt_id"]


async def wait_history(prompt_id: str, timeout: float = 300) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        h = await comfy_json("GET", f"/history/{prompt_id}")
        if prompt_id in h:
            entry = h[prompt_id]
            status = entry.get("status", {})
            for kind, data in status.get("messages", []):
                if kind == "execution_error":
                    raise HTTPException(500, f"{data.get('node_type')}: {data.get('exception_message')}")
            return entry
        await asyncio.sleep(0.5)
    raise HTTPException(504, "ComfyUI job timed out")


def view_url(img: dict) -> str:
    q = httpx.QueryParams({"filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")})
    return f"/api/view?{q}"


# ---------------------------------------------------------------- basic endpoints

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/status")
async def status():
    try:
        q = await comfy_json("GET", "/queue")
    except HTTPException as e:
        return {"comfy": False, "error": e.detail}
    return {"comfy": True, "running": len(q["queue_running"]), "pending": len(q["queue_pending"])}


@app.get("/api/models")
async def models():
    info = await comfy_json("GET", "/object_info")

    def options(node: str, field: str) -> list[str]:
        try:
            spec = info[node]["input"]["required"][field]
        except KeyError:
            return []
        opts = spec[0] if isinstance(spec[0], list) else spec[1].get("options", [])
        return list(opts)

    unets = [u for u in options("UnetLoaderGGUF", "unet_name") + options("UNETLoader", "unet_name") if "qwen" in u.lower() and "image" in u.lower()]
    clips = [c for c in options("CLIPLoader", "clip_name") if "qwen3vl" in c.lower()]
    vaes = [v for v in options("VAELoader", "vae_name") if "qwen_image" in v.lower()]
    return {
        "unets": unets, "clips": clips, "vaes": vaes,
        "samplers": options("KSampler", "sampler_name"), "schedulers": options("KSampler", "scheduler"),
        "token_limit": graphs.TOKEN_LIMIT,
    }


@app.get("/api/view")
async def view(filename: str, subfolder: str = "", type: str = "output"):
    try:
        r = await client.get("/view", params={"filename": filename, "subfolder": subfolder, "type": type})
    except httpx.HTTPError as e:
        raise HTTPException(502, str(e)) from e
    if r.status_code != 200:
        raise HTTPException(r.status_code, "image not found")
    return Response(r.content, media_type=r.headers.get("content-type", "image/png"))


async def upload_to_comfy(data: bytes, name: str, subfolder: str) -> str:
    files = {"image": (name, data, "image/png")}
    res = await comfy_json("POST", "/upload/image", files=files, data={"subfolder": subfolder, "overwrite": "true", "type": "input"})
    return f"{res['subfolder']}/{res['name']}" if res.get("subfolder") else res["name"]


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    with Image.open(io.BytesIO(data)) as im:
        width, height = im.size
    safe_name = f"{uuid.uuid4().hex[:8]}_{Path(file.filename or 'image.png').name}"
    name = await upload_to_comfy(data, safe_name, SUBFOLDER)
    return {"name": name, "width": width, "height": height}


@app.post("/api/upload-mask")
async def upload_mask(file: UploadFile = File(...)):
    data = await file.read()
    name = await upload_to_comfy(data, f"mask_{uuid.uuid4().hex[:8]}.png", f"{SUBFOLDER}/masks")
    return {"name": name}


class SizeReq(BaseModel):
    width: int
    height: int
    megapixels: float = 0.95
    resolution: int = 1024


@app.post("/api/size")
async def size(req: SizeReq):
    report = graphs.size_report(req.width, req.height, req.megapixels, req.resolution)
    report["suggested"] = graphs.safe_settings(req.width, req.height, req.megapixels, req.resolution)
    return report


class MaskReq(BaseModel):
    image: str
    megapixels: float
    text: str
    threshold: float = 0.5
    refine: int = 2
    expand: int = 24
    invert: bool = False


@app.post("/api/mask")
async def mask(req: MaskReq):
    graph = graphs.build_mask_graph(req.image, req.megapixels, req.text, req.threshold, req.refine, req.expand, req.invert)
    t0 = time.time()
    pid = await submit(graph, f"inpaint-studio-{uuid.uuid4().hex[:6]}")
    entry = await wait_history(pid, timeout=180)
    imgs = entry.get("outputs", {}).get("out_mask", {}).get("images", [])
    if not imgs:
        raise HTTPException(500, "mask output missing")
    return {"mask_url": view_url(imgs[0]), "seconds": round(time.time() - t0, 1)}


@app.post("/api/cancel")
async def cancel():
    q = await comfy_json("GET", "/queue")
    running = [item[1] for item in q["queue_running"]]
    if current_prompt["id"] and current_prompt["id"] in running:
        await client.post("/interrupt")
        return {"cancelled": True}
    return {"cancelled": False, "reason": "no own job running"}


# ---------------------------------------------------------------- run history (persisted on disk)

def save_run(run: dict) -> None:
    d = RUNS / run["id"]
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "run.json.tmp"
    tmp.write_text(json.dumps(run, indent=1))
    tmp.replace(d / "run.json")


@app.get("/api/runs")
async def list_runs():
    runs = []
    for f in RUNS.glob("*/run.json"):
        try:
            runs.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    runs = [r for r in runs if r.get("status") == "done"]
    return sorted(runs, key=lambda r: r.get("created", 0), reverse=True)


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str):
    d = (RUNS / run_id).resolve()
    if d.parent != RUNS.resolve() or not d.is_dir():
        raise HTTPException(404, "run not found")
    for f in d.iterdir():
        f.unlink()
    d.rmdir()
    return {"deleted": run_id}


# ---------------------------------------------------------------- live edit over websocket

def parse_preview(msg: bytes) -> tuple[str, bytes] | None:
    if len(msg) < 8:
        return None
    event = struct.unpack(">I", msg[:4])[0]
    if event == 1:  # PREVIEW_IMAGE: [event][image type][bytes]
        kind = struct.unpack(">I", msg[4:8])[0]
        return ("image/png" if kind == 2 else "image/jpeg", msg[8:])
    if event == 4:  # PREVIEW_IMAGE_WITH_METADATA: [event][meta len][json][bytes]
        n = struct.unpack(">I", msg[4:8])[0]
        meta = json.loads(msg[8:8 + n] or b"{}")
        return (meta.get("image_type", "image/jpeg"), msg[8 + n:])
    return None


@app.websocket("/ws/edit")
async def ws_edit(ws: WebSocket):
    await ws.accept()
    try:
        params = await ws.receive_json()
        rep = graphs.size_report(params["src_w"], params["src_h"], params["megapixels"], params["resolution"])
        params["work_w"], params["work_h"] = rep["work_w"], rep["work_h"]
        params.setdefault("prefix", f"InpaintStudio/{time.strftime('%Y%m%d-%H%M%S')}")
        client_id = f"inpaint-studio-{uuid.uuid4().hex}"
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        run: dict | None = None
        live_n = 0
        graph = graphs.build_edit_graph(params)
        every = int(params.get("save_every") or 0)
        chunk_starts = [a for a, _ in graphs.step_chunks(int(params["steps"]), every)] if every > 0 else []
        async with websockets.connect(f"{COMFY_WS}?clientId={client_id}", max_size=64 * 1024 * 1024) as cws:
            pid = await submit(graph, client_id, {"preview_method": params.get("preview_method", "auto")})
            current_prompt["id"] = pid
            run = {"id": run_id, "created": time.time(), "status": "running", "prompt_id": pid,
                   "params": {k: params.get(k) for k in HISTORY_PARAMS}, "size": rep, "frames": []}
            save_run(run)
            await ws.send_json({"type": "queued", "prompt_id": pid, "size": rep, "run_id": run_id})
            step = 0
            while True:
                msg = await cws.recv()
                if isinstance(msg, bytes):
                    parsed = parse_preview(msg)
                    if parsed:
                        mime, data = parsed
                        if run and not any(f["kind"] == "saved" and f["step"] == step for f in run["frames"]):
                            live_n += 1
                            name = f"live_{live_n:03d}.{'png' if 'png' in mime else 'jpg'}"
                            (RUNS / run_id / name).write_bytes(data)
                            run["frames"].append({"kind": "live", "step": step, "mime": mime, "url": f"/data/runs/{run_id}/{name}"})
                        await ws.send_json({"type": "preview", "step": step, "mime": mime, "data": base64.b64encode(data).decode()})
                    continue
                ev = json.loads(msg)
                kind, data = ev.get("type"), ev.get("data", {})
                if data.get("prompt_id") not in (None, pid):
                    continue
                node = str(data.get("node") or "")
                if kind == "progress" and (node == "sampler" or node.startswith("chunk_")):
                    # chunked runs report per chunk; convert to overall step numbers
                    offset = chunk_starts[int(node.split("_")[1])] if node.startswith("chunk_") else 0
                    step = offset + data["value"]
                    await ws.send_json({"type": "progress", "value": step, "max": int(params["steps"])})
                elif kind == "executed" and str(data.get("node", "")).startswith(("stepsave_", "stepraw_")):
                    imgs = (data.get("output") or {}).get("images", [])
                    variant = "raw" if data["node"].startswith("stepraw_") else "result"
                    if imgs:
                        sstep = int(data["node"].split("_")[1])
                        if run:  # the saved render replaces the live preview of the same step
                            for f in [f for f in run["frames"] if f["kind"] == "live" and f["step"] == sstep]:
                                (RUNS / run_id / Path(f["url"]).name).unlink(missing_ok=True)
                                run["frames"].remove(f)
                            run["frames"].append({"kind": "saved", "variant": variant, "step": sstep, "mime": "image/png",
                                                  "url": view_url(imgs[0]), "filename": imgs[0]["filename"]})
                        await ws.send_json({"type": "step_image", "step": sstep, "variant": variant,
                                            "url": view_url(imgs[0]), "filename": imgs[0]["filename"]})
                elif kind == "executing" and data.get("node"):
                    await ws.send_json({"type": "node", "node": data["node"]})
                elif kind == "execution_error":
                    await ws.send_json({"type": "error", "message": f"{data.get('node_type')}: {data.get('exception_message')}"})
                    break
                elif kind == "execution_interrupted":
                    await ws.send_json({"type": "error", "message": "Cancelled"})
                    break
                elif kind == "execution_success" or (kind == "executing" and data.get("node") is None and data.get("prompt_id") == pid):
                    entry = await wait_history(pid, timeout=30)
                    outs = entry.get("outputs", {})
                    res = outs.get("out_result", {}).get("images", [])
                    before = outs.get("out_before", {}).get("images", [])
                    raw = outs.get("out_raw", {}).get("images", [])
                    pmask = outs.get("out_mask", {}).get("images", [])
                    if run:
                        run.update({"status": "done", "finished": time.time(),
                                    "result_url": view_url(res[0]) if res else None,
                                    "before_url": view_url(before[0]) if before else None,
                                    "raw_url": view_url(raw[0]) if raw else None,
                                    "mask_url": view_url(pmask[0]) if pmask else None,
                                    "filename": res[0]["filename"] if res else None})
                        save_run(run)
                    await ws.send_json({"type": "done", "run_id": run_id,
                                        "result_url": view_url(res[0]) if res else None,
                                        "before_url": view_url(before[0]) if before else None,
                                        "raw_url": view_url(raw[0]) if raw else None,
                                        "mask_url": view_url(pmask[0]) if pmask else None,
                                        "filename": res[0]["filename"] if res else None})
                    break
    except WebSocketDisconnect:
        return
    except HTTPException as e:
        await ws.send_json({"type": "error", "message": str(e.detail)})
    except Exception as e:  # surface anything unexpected to the UI
        await ws.send_json({"type": "error", "message": repr(e)})
    finally:
        current_prompt["id"] = None
        try:
            await ws.close()
        except RuntimeError:
            pass


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


app.mount("/data/runs", StaticFiles(directory=RUNS), name="runs")
app.mount("/", StaticFiles(directory=WEB), name="web")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT") or 7380))
