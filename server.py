"""Inpaint Studio - local two-step mask + Qwen-Image 2.1 edit UI for ComfyUI.

Step 1 runs only SAM3 and returns the mask, so it can be tuned (and painted) quickly.
Step 2 runs the edit; ComfyUI's per-step latent previews are relayed to the browser.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import struct
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import websockets
from PIL import Image, ImageFilter
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import align
import graphs
import installer
import presets

ROOT = Path(__file__).parent
WEB = ROOT / "web"
COMFY = os.environ.get("COMFY_URL") or "http://127.0.0.1:8188"
COMFY_WS = COMFY.replace("http", "ws", 1) + "/ws"
SUBFOLDER = "inpaint-studio"
# ComfyUI's output folder, used to drop the _00001_ counter from saved file names (default: setup config)
COMFY_OUTPUT = Path(os.environ["COMFY_OUTPUT_DIR"]).expanduser() if os.environ.get("COMFY_OUTPUT_DIR") else None
INSTALLER = installer.Installer()
COMFY_PROC = installer.ComfyProcess()
RUNS = Path(os.environ.get("INPAINT_STUDIO_DATA") or installer.APP_SUPPORT) / "runs"
RUNS.mkdir(parents=True, exist_ok=True)
HISTORY_PARAMS = ("prompt", "negative", "mode", "use_mask", "steps", "denoise", "seed", "cfg", "sampler",
                  "scheduler", "feather", "megapixels", "resolution", "save_every", "save_last", "unet",
                  "keep_identical", "preset", "quant", "task", "family",
                  "upscale", "upscaler", "post_colors", "post_warp", "post_poisson", "refs", "ref_takes", "ref_crops", "ref_note", "clean_overlays", "keep_note")

app = FastAPI(title="Inpaint Studio")
client = httpx.AsyncClient(base_url=COMFY, timeout=60)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if not request.url.path.startswith(("/api/", "/data/")):
        # UI files: always revalidate, so an app update never runs with stale cached modules
        response.headers["Cache-Control"] = "no-cache"
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


def drop_counter(img: dict) -> dict:
    """Rename ComfyUI's 'name_00001_.png' to 'name.png'. Every run has its own prefix, so the
    counter never matters; if the output folder is not reachable the name is kept."""
    new = re.sub(r"_\d{5}_(\.\w+)$", r"\1", img["filename"])
    src = (COMFY_OUTPUT or Path(installer.load_config()["output_dir"])) / img.get("subfolder", "") / img["filename"]
    dst = src.with_name(new)
    if new == img["filename"] or img.get("type", "output") != "output" or not src.is_file() or dst.exists():
        return img
    try:
        src.rename(dst)
    except OSError:
        return img
    return {**img, "filename": new}


def input_mask_url(name: str) -> str:
    sub, _, fname = name.rpartition("/")
    return view_url({"filename": fname, "subfolder": sub, "type": "input"})


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


# ---------------------------------------------------------------- setup (first run, optional masking)

def system_ram() -> int:
    try:
        return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2).stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 16 * 1024**3


async def comfy_up() -> bool:
    try:
        await comfy_json("GET", "/system_stats")
        return True
    except HTTPException:
        return False


async def ensure_comfy() -> None:
    """Start ComfyUI headless when nothing answers on the default local port yet."""
    cfg = installer.load_config()
    if COMFY.rstrip("/") == "http://127.0.0.1:8188" and installer.ready(installer.installed(cfg)) and not await comfy_up():
        COMFY_PROC.start(cfg)


@app.get("/api/setup")
async def setup_status():
    """Everything the setup page / download centre and the model picker need."""
    cfg = installer.load_config()
    have = installer.installed(cfg)
    base = [{"id": s, "title": t, "description": d, "installed": have[s]} for s, t, d in installer.BASE_STEPS]
    comps = [{"id": f"component:{cid}", "key": cid, "title": c["title"], "size": c["size"], "file": c["path"].rsplit("/", 1)[-1],
             "installed": have[f"component:{cid}"], "kind": c.get("kind"), "scale": c.get("scale"),
              "description": c.get("description", "")}
             for cid, c in presets.COMPONENTS.items()]
    ram = system_ram()
    models = []
    for pid, pr in presets.PRESETS.items():
        st = installer.preset_status(have, pid)
        models.append({
            "id": pid, "title": pr["title"], "family": pr["family"], "modes": pr["modes"], "note": pr["note"],
            "experimental": bool(pr.get("experimental")), "default_quant": pr["default_quant"], "defaults": pr["defaults"],
            "good_for": pr.get("good_for", ""), "recommended": bool(pr.get("recommended")),
            "recommended_quant": presets.recommended_quant(pid, ram),
            "text_encoder": pr["text_encoder"], "vae": pr["vae"], "nodes": pr.get("nodes", []), **st,
            "quants": [{"id": f"model:{pid}:{q}", "quant": q, "size": f["size"], "file": f["file"], "installed": have[f"model:{pid}:{q}"],
                        "memory": presets.memory_need(pid, q), "fit": presets.memory_fit(presets.memory_need(pid, q), ram)}
                       for q, f in pr["quants"].items()]})
    return {"ready": installer.ready(have), "mask_available": have["component:sam3"], "config": cfg,
            "system": {"ram": ram, "gpu_budget": int(ram * presets.GPU_SHARE)},
            "base": base, "components": comps, "presets": models, "default_preset": presets.DEFAULT_PRESET,
            "comfy": {"up": await comfy_up(), "managed": COMFY_PROC.managed}, "install": INSTALLER.state()}


@app.post("/api/setup/config")
async def setup_config(values: dict):
    if INSTALLER.running:
        raise HTTPException(409, "setup is running")
    try:
        installer.save_config(values)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return await setup_status()


class InstallReq(BaseModel):
    items: list[str]


@app.post("/api/setup/install")
async def setup_install(req: InstallReq):
    """Install base steps, components ("component:<id>") and models ("model:<preset>:<quant>");
    a model's missing text encoder / VAE are added automatically."""
    if INSTALLER.running:
        raise HTTPException(409, "setup is already running")
    if not req.items or not all(installer.valid_item(i) for i in req.items):
        raise HTTPException(400, "unknown or no items")
    cfg = installer.load_config()
    have = installer.installed(cfg)
    order = [i for i in installer.expand(req.items, have) if not have.get(i)]
    if not order:
        return await setup_status()

    async def done() -> None:
        new_code = any(i in ("comfyui", "gguf_node") or i.startswith("component:viggle_node") for i in order)
        if new_code and COMFY_PROC.managed:
            COMFY_PROC.stop()  # restart so new code / nodes are loaded
        elif new_code and await comfy_up():
            INSTALLER.restart_hint = True  # someone else's ComfyUI (Comfy Desktop) must be restarted by hand
        await ensure_comfy()

    INSTALLER.start(order, done)
    return await setup_status()


class DeleteReq(BaseModel):
    item: str


@app.post("/api/setup/delete")
async def setup_delete(req: DeleteReq):
    """Delete a downloaded model or component file (a symlink is removed, its target is kept)."""
    if INSTALLER.running:
        raise HTTPException(409, "setup is running")
    if ":" not in req.item or not installer.valid_item(req.item):
        raise HTTPException(400, "only models and components can be deleted")
    cfg = installer.load_config()
    path = installer.item_path(cfg, req.item)  # always <models_dir>/<folder>/<known file name>
    if path is None:
        raise HTTPException(404, "not installed")
    path.unlink()
    return await setup_status()


@app.post("/api/setup/cancel")
async def setup_cancel():
    INSTALLER.cancel()
    return {"ok": True}


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
    report["match_res"] = graphs.matching_resolution(report["work_w"], report["work_h"])
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


# ---------------------------------------------------------------- run history (persisted on disk)

def save_run(run: dict) -> None:
    d = RUNS / run["id"]
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "run.json.tmp"
    tmp.write_text(json.dumps(run, indent=1))
    tmp.replace(d / "run.json")


@app.get("/api/runs")
async def list_runs(hidden: bool = False):
    """Finished runs, newest first; hidden=1 lists the ones removed from the history instead."""
    runs = []
    for f in RUNS.glob("*/run.json"):
        try:
            runs.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    runs = [r for r in runs if r.get("status") == "done" and bool(r.get("hidden")) == hidden]
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


@app.post("/api/runs/{run_id}/hide")
async def hide_run(run_id: str):
    """Removes a run from the history without deleting any file (run.json gets hidden: true)."""
    f = (RUNS / run_id / "run.json").resolve()
    if f.parent.parent != RUNS.resolve() or not f.is_file():
        raise HTTPException(404, "run not found")
    run = json.loads(f.read_text())
    run["hidden"] = True
    save_run(run)
    return {"hidden": run_id}


@app.post("/api/runs/{run_id}/restore")
async def restore_run(run_id: str):
    """Brings a removed run back into the history."""
    f = (RUNS / run_id / "run.json").resolve()
    if f.parent.parent != RUNS.resolve() or not f.is_file():
        raise HTTPException(404, "run not found")
    run = json.loads(f.read_text())
    run.pop("hidden", None)
    save_run(run)
    return run


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


# ---------------------------------------------------------------- post-hoc alignment (advanced)

_align_cache: dict[str, tuple] = {}


async def _fetch_view(url: str) -> Image.Image:
    params = dict(httpx.URL(url).params)
    r = await client.get("/view", params=params)
    if r.status_code != 200:
        raise HTTPException(404, f"image not found: {params.get('filename')}")
    return Image.open(io.BytesIO(r.content))


class AlignReq(BaseModel):
    auto: bool = False
    dx: float = 0
    dy: float = 0
    scale: float = 1.0
    colors: bool = False
    warp: bool = False
    poisson: bool = False
    save: bool = False


async def adjust_run(run: dict, req: AlignReq) -> dict:
    """Align and fix the free edit (shift/scale, local warp, colours, seamless edge), paste it into the
    original and write a preview (or, with save, the adjusted result: run dir + ComfyUI output)."""
    run_id = run["id"]
    if not (run.get("before_url") and run.get("raw_url") and run.get("mask_url")):
        raise HTTPException(400, "adjusting needs a free edit + paste run (raw image and mask)")
    if run_id not in _align_cache:
        _align_cache.clear()  # keep only the run being adjusted in memory
        _align_cache[run_id] = tuple([await _fetch_view(run[k]) for k in ("before_url", "raw_url", "mask_url")])
    original, raw, mask = _align_cache[run_id]
    feather = int(run.get("params", {}).get("feather") or 0)
    if feather > 0 and "type=input" in run["mask_url"]:  # newer runs keep only the uploaded hard mask
        mask = mask.convert("L").filter(ImageFilter.GaussianBlur(max(1.0, feather / 3)))
    result: dict[str, Any] = {}
    dx, dy, scale = req.dx, req.dy, req.scale
    if req.auto:
        est = await asyncio.to_thread(align.estimate, original, raw, mask)
        dx, dy, scale = est["dx"], est["dy"], est["scale"]
        result["confidence"] = est["confidence"]
    if not 0.8 <= scale <= 1.25 or abs(dx) > 500 or abs(dy) > 500:
        raise HTTPException(400, "alignment values out of range")
    opts = {"colors": req.colors, "warp": req.warp, "poisson": req.poisson}
    composed, stats = await asyncio.to_thread(align.compose, original, raw, mask, dx, dy, scale, **opts)
    _, base = await asyncio.to_thread(align.compose, original, raw, mask, 0, 0, 1.0)
    name = "aligned.png" if req.save else "aligned_preview.jpg"
    composed.save(RUNS / run_id / name, quality=92)
    url = f"/data/runs/{run_id}/{name}?t={int(time.time() * 1000)}"
    result.update({"dx": dx, "dy": dy, "scale": scale, **opts, "outside_diff": stats["outside_diff"],
                   "unaligned_diff": base["outside_diff"], "url": url, "saved": req.save})
    if req.save:
        run["aligned"] = {"dx": dx, "dy": dy, "scale": scale, **opts, "outside_diff": stats["outside_diff"], "url": url}
        save_run(run)
        out_dir = COMFY_OUTPUT or Path(installer.load_config()["output_dir"])
        try:  # next to <run>.png, so the files on disk match what the app shows
            (out_dir / "InpaintStudio").mkdir(parents=True, exist_ok=True)
            composed.save(out_dir / "InpaintStudio" / f"{run_id}_fixed.png")
        except OSError:
            pass
    return result


@app.post("/api/runs/{run_id}/align")
async def align_run(run_id: str, req: AlignReq):
    f = RUNS / run_id / "run.json"
    if not f.is_file():
        raise HTTPException(404, "run not found")
    return await adjust_run(json.loads(f.read_text()), req)


# ---------------------------------------------------------------- job queue
# Every "Run edit" becomes a job: it is submitted to ComfyUI right away (ComfyUI queues it) and
# a background task follows it on its own ComfyUI websocket. Browsers only subscribe to
# /ws/jobs, so jobs keep running and get recorded when the page is reloaded or closed.

JOBS: dict[str, dict] = {}            # job_id -> job (job["run"] is what ends up in run.json)
SUBSCRIBERS: set[WebSocket] = set()


async def broadcast(event: dict) -> None:
    dead = []
    for sub in list(SUBSCRIBERS):
        try:
            await sub.send_json(event)
        except Exception:
            dead.append(sub)
    for sub in dead:
        SUBSCRIBERS.discard(sub)


def decode_steps(params: dict) -> list[int]:
    """Steps after which the VAE decodes an image (saved steps and the result), for the time estimate."""
    steps = int(params.get("steps") or 0)
    every, last = int(params.get("save_every") or 0), int(params.get("save_last") or 0)
    return [b for _, b in graphs.step_chunks(steps, every, last)] if every > 0 or last > 0 else [steps]


def job_summary(job: dict) -> dict:
    run = job["run"]
    return {"job_id": run["id"], "status": run["status"], "prompt": run["params"].get("prompt", ""),
            "seed": run["params"].get("seed"), "steps": run["params"].get("steps"), "value": job.get("value", 0),
            "created": run["created"], "size": run.get("size"), "frames": run["frames"], "error": run.get("error"),
            "phase": job.get("phase"), "decode_steps": decode_steps(job["params"])}


async def finish_job(job: dict, status: str, **extra) -> None:
    run = job["run"]
    run.update({"status": status, "finished": time.time(), **extra})
    save_run(run)
    await broadcast({"type": status, "job_id": run["id"], "run": run})
    JOBS.pop(run["id"], None)


async def run_job(job: dict) -> None:
    run, params, run_id = job["run"], job["params"], job["run"]["id"]
    every, last = int(params.get("save_every") or 0), int(params.get("save_last") or 0)
    chunks = graphs.step_chunks(int(params["steps"]), every, last) if every > 0 or last > 0 else []
    chunk_starts = [a for a, _ in chunks]
    client_id = f"inpaint-studio-{uuid.uuid4().hex}"
    live_n, step = 0, 0
    try:
        async with websockets.connect(f"{COMFY_WS}?clientId={client_id}", max_size=64 * 1024 * 1024) as cws:
            pid = await submit(job["graph"], client_id, {"preview_method": "auto"})
            run["prompt_id"] = pid
            save_run(run)
            await broadcast({"type": "queued", "job_id": run_id, "job": job_summary(job)})
            while True:
                msg = await cws.recv()
                if isinstance(msg, bytes):
                    parsed = parse_preview(msg)
                    if parsed and not any(f["kind"] == "saved" and f["step"] == step for f in run["frames"]):
                        mime, data = parsed
                        live_n += 1
                        name = f"live_{live_n:03d}.{'png' if 'png' in mime else 'jpg'}"
                        (RUNS / run_id / name).write_bytes(data)
                        frame = {"kind": "live", "step": step, "mime": mime, "url": f"/data/runs/{run_id}/{name}"}
                        run["frames"].append(frame)
                        await broadcast({"type": "frame", "job_id": run_id, "frame": frame})
                    continue
                ev = json.loads(msg)
                kind, data = ev.get("type"), ev.get("data", {})
                if data.get("prompt_id") not in (None, pid):
                    continue
                node = str(data.get("node") or "")
                if kind == "executing" and node in job["graph"]:
                    # which workflow node runs now, for the strip above the image in Runs
                    n_refs = min(len(params.get("refs") or []), graphs.MAX_REFS.get(params.get("family"), 0))
                    if ph := graphs.node_phase(node, job["graph"][node]["class_type"], chunks, n_refs):
                        job["phase"] = ph
                        await broadcast({"type": "node", "job_id": run_id, **ph})
                elif kind == "execution_start":
                    run["status"] = "running"
                    save_run(run)
                    await broadcast({"type": "running", "job_id": run_id})
                elif kind == "progress" and (node == "sampler" or node.startswith("chunk_")):
                    # chunked runs report per chunk; convert to overall step numbers
                    offset = chunk_starts[int(node.split("_")[1])] if node.startswith("chunk_") else 0
                    step = job["value"] = offset + data["value"]
                    await broadcast({"type": "progress", "job_id": run_id, "value": step, "max": int(params["steps"])})
                elif kind == "executed" and node.startswith(("stepsave_", "stepraw_")):
                    imgs = (data.get("output") or {}).get("images", [])
                    if not imgs:
                        continue
                    sstep = int(node.split("_")[1])
                    imgs = [drop_counter(imgs[0])]
                    for f in [f for f in run["frames"] if f["kind"] == "live" and f["step"] == sstep]:
                        (RUNS / run_id / Path(f["url"]).name).unlink(missing_ok=True)
                        run["frames"].remove(f)  # the saved render replaces the live preview
                    frame = {"kind": "saved", "variant": "raw" if node.startswith("stepraw_") else "result",
                             "step": sstep, "mime": "image/png", "url": view_url(imgs[0]), "filename": imgs[0]["filename"]}
                    run["frames"].append(frame)
                    await broadcast({"type": "frame", "job_id": run_id, "frame": frame})
                elif kind == "execution_error":
                    await finish_job(job, "error", error=f"{data.get('node_type')}: {data.get('exception_message')}")
                    return
                elif kind == "execution_interrupted":
                    await finish_job(job, "cancelled", error="Cancelled")
                    return
                elif kind == "execution_success" or (kind == "executing" and data.get("node") is None and data.get("prompt_id") == pid):
                    outs = (await wait_history(pid, timeout=30)).get("outputs", {})
                    def first(key: str) -> dict | None:
                        img = (outs.get(key, {}).get("images") or [None])[0]
                        return drop_counter(img) if img else None
                    res, before, raw, upscaled = first("out_result"), first("out_before"), first("out_raw"), first("out_upscaled")
                    use_mask = params.get("use_mask") and params.get("mask")
                    run.update(before_url=view_url(before) if before else None, raw_url=view_url(raw) if raw else None,
                               mask_url=input_mask_url(params["mask"]) if use_mask else None)
                    post = {k: bool(params.get(f"post_{k}")) for k in ("colors", "warp", "poisson")}
                    if params.get("mode") == "paste" and use_mask and raw and any(post.values()):
                        try:  # automatic fixes; the plain paste stays available as <run>.png
                            await adjust_run(run, AlignReq(save=True, **post))
                        except Exception as e:  # never fail the run because of the post-processing
                            print(f"post-processing failed for {run_id}: {e!r}")
                    await finish_job(job, "done",
                                     result_url=view_url(res) if res else None, before_url=view_url(before) if before else None,
                                     raw_url=view_url(raw) if raw else None,
                                     upscaled_url=view_url(upscaled) if upscaled else None,
                                     mask_url=input_mask_url(params["mask"]) if use_mask else None,
                                     filename=res["filename"] if res else None)
                    return
    except asyncio.CancelledError:
        raise
    except HTTPException as e:
        await finish_job(job, "error", error=str(e.detail))
    except Exception as e:  # keep the queue alive, report to the UI
        await finish_job(job, "error", error=repr(e))


def save_run_config(run_id: str, run: dict, params: dict, graph: dict) -> None:
    """Writes <output>/InpaintStudio/<run>/config.json next to the run's images: every setting of the run,
    the prompt text the encoder gets (with the hidden notes) and the ComfyUI graph, so a run on disk explains itself."""
    out_dir = COMFY_OUTPUT or Path(installer.load_config()["output_dir"])
    prompt = params["prompt"] if params.get("task") == "generate" else graphs.edit_prompt(params)
    config = {"id": run_id, "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(run["created"])),
              "params": params, "size": run["size"], "encoder_prompt": prompt, "graph": graph}
    try:
        d = out_dir / "InpaintStudio" / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps(config, indent=2, default=str))
    except OSError:
        pass


@app.post("/api/jobs")
async def create_job(params: dict):
    if params.get("preset"):
        pr = presets.PRESETS.get(params["preset"])
        if not pr:
            raise HTTPException(400, f"unknown model preset {params['preset']}")
        params["task"] = params.get("task") or "edit"
        if params["task"] not in pr["modes"]:
            raise HTTPException(400, f"{pr['title']} cannot {params['task']}")
        # Advanced overrides: an explicitly chosen unet / clip / vae wins over the preset's file
        resolved = presets.resolve(params["preset"], params.get("quant"))
        params.update({k: params.get(k) or v for k, v in resolved.items()})
    if params.get("task") == "generate":
        params.update(use_mask=False, mask=None, image=None, denoise=1.0)
    # extra reference images: only as many as the model's text encoder takes
    refs = [r for r in (params.get("refs") or []) if isinstance(r, str) and r]
    params["refs"] = refs[:graphs.MAX_REFS.get(params.get("family"), 0)]
    takes = params.get("ref_takes") or []   # "what to take from it", one short text per reference
    params["ref_takes"] = [str(t or "").strip() for t in takes[:len(params["refs"])]]
    crops = params.get("ref_crops") or []   # optional {x, y, w, h} per reference
    params["ref_crops"] = [graphs.crop_box(c) for c in crops[:len(params["refs"])]]
    up = presets.COMPONENTS.get(params.get("upscaler") or "")
    if int(params.get("upscale") or 0) > 1 and up and up.get("kind") == "upscaler":
        params.update(upscale_model=up["path"].rsplit("/", 1)[-1], upscale_native=up["scale"])
    else:
        params["upscale"] = 0
    if params.get("family") == "qwen21_turbo":  # fixed few-step schedule, no CFG
        params.update(steps=graphs.turbo_steps(params["steps"]), cfg=1.0)
    rep = graphs.size_report(params["src_w"], params["src_h"], params["megapixels"], params["resolution"])
    params["work_w"], params["work_h"] = rep["work_w"], rep["work_h"]
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    params["prefix"] = f"InpaintStudio/{run_id}"
    graph = graphs.build_edit_graph(params)
    run = {"id": run_id, "created": time.time(), "status": "queued",
           "params": {k: params.get(k) for k in HISTORY_PARAMS}, "size": rep, "frames": []}
    save_run(run)
    save_run_config(run_id, run, params, graph)
    job = {"run": run, "params": params, "graph": graph, "value": 0}
    JOBS[run_id] = job
    job["task"] = asyncio.create_task(run_job(job))
    return job_summary(job)


@app.get("/api/jobs")
async def list_jobs():
    return [job_summary(j) for j in JOBS.values()]


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found or already finished")
    pid = job["run"].get("prompt_id")
    q = await comfy_json("GET", "/queue")
    if pid and pid in [item[1] for item in q["queue_running"]]:
        await client.post("/interrupt", json={"prompt_id": pid})
        return {"cancelled": True, "was": "running"}
    if pid and pid in [item[1] for item in q["queue_pending"]]:
        await client.post("/queue", json={"delete": [pid]})
    job["task"].cancel()
    await finish_job(job, "cancelled", error="Removed from queue")
    return {"cancelled": True, "was": "queued"}


@app.websocket("/ws/jobs")
async def ws_jobs(ws: WebSocket):
    await ws.accept()
    SUBSCRIBERS.add(ws)
    await ws.send_json({"type": "snapshot", "jobs": [job_summary(j) for j in JOBS.values()]})
    try:
        while True:
            await ws.receive_text()  # keep-alive; the client does not send anything meaningful
    except WebSocketDisconnect:
        pass
    finally:
        SUBSCRIBERS.discard(ws)


@app.on_event("startup")
async def start_comfy():
    try:
        await ensure_comfy()
    except Exception as e:  # never block the UI (it shows the setup page instead)
        print(f"could not start ComfyUI: {e!r}")


@app.on_event("shutdown")
async def stop_comfy():
    INSTALLER.cancel()
    COMFY_PROC.stop()


@app.on_event("startup")
async def mark_orphaned_runs():
    # jobs live in memory; runs left "queued"/"running" by a previous server process are stale
    for f in RUNS.glob("*/run.json"):
        try:
            run = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if run.get("status") in ("queued", "running"):
            run.update({"status": "error", "error": "Server restarted before the job finished"})
            save_run(run)


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


app.mount("/data/runs", StaticFiles(directory=RUNS), name="runs")
app.mount("/", StaticFiles(directory=WEB), name="web")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT") or 7380))
