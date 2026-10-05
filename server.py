"""Inpaint Studio - local two-step mask + Qwen-Image 2.1 edit UI for ComfyUI.

Step 1 runs only SAM3 and returns the mask, so it can be tuned (and painted) quickly.
Step 2 runs the edit; ComfyUI's per-step latent previews are relayed to the browser.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
from contextlib import asynccontextmanager
import io
import json
import os
import re
import shutil
import struct
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import websockets
import numpy as np
import psutil
from PIL import Image, ImageFilter, ImageOps, PngImagePlugin
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

import align
import graphs
import prepare
import imports
import installer
import presets
import sysload

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
# the very first start (no setup config, no runs yet) opens the beginner guide once; guide.html marks it seen
GUIDE_SEEN = installer.APP_SUPPORT / "guide_seen"
FRESH_INSTALL = not installer.CONFIG_FILE.exists() and not RUNS.exists()
RUNS.mkdir(parents=True, exist_ok=True)
imports.apply()   # own files from the Download Center become components / presets
HISTORY_PARAMS = ("prompt", "negative", "mode", "use_mask", "steps", "denoise", "seed", "cfg", "sampler",
                  "scheduler", "feather", "megapixels", "resolution", "save_every", "save_last", "unet",
                  "keep_identical", "preset", "quant", "task", "family",
                  "upscale", "upscale_width", "upscale_long_side", "upscale_mb", "grain", "upscaler", "post_colors", "post_warp", "post_poisson", "post_align", "post_grain", "grain_strength", "refs", "ref_takes", "ref_crops", "ref_note", "clean_overlays", "keep_whole", "keep_note", "upscale_of", "color_correction", "group", "variant",
                  "crop_stitch", "crop_context", "crop_box", "orig_size", "outpaint", "loras", "outpaint_colors", "crop_grain", "outpaint_holes", "remove_bg", "control")

@asynccontextmanager
async def lifespan(app):
    await start_comfy()
    await reattach_runs()
    yield
    INSTALLER.cancel()
    COMFY_PROC.stop()


app = FastAPI(title="Inpaint Studio", lifespan=lifespan)
client = httpx.AsyncClient(base_url=COMFY, timeout=60)


# Hosts/origins allowed to use the API (any port): blocks other websites and DNS rebinding
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver",
                 *(h.strip().lower() for h in os.environ.get("INPAINT_STUDIO_ALLOWED_HOSTS", "").split(",") if h.strip())}


def local_origin(host: str | None, origin: str | None) -> bool:
    def name(netloc: str) -> str:
        netloc = netloc.strip().lower()
        return netloc[:netloc.index("]") + 1] if netloc.startswith("[") and "]" in netloc else netloc.split(":")[0]
    if name(host or "") not in ALLOWED_HOSTS:
        return False
    return origin is None or name(urlsplit(origin).netloc) in ALLOWED_HOSTS


@app.middleware("http")
async def security_headers(request, call_next):
    if request.url.path.startswith(("/api/", "/data/")) and not local_origin(
            request.headers.get("host"), request.headers.get("origin")):
        return JSONResponse({"detail": "forbidden origin"}, status_code=403)
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


async def submit(graph: dict, client_id: str, extra: dict | None = None, front: bool = False) -> str:
    body = {"prompt": graph, "client_id": client_id}
    if front:  # ahead of everything still pending in ComfyUI's queue (after the running prompt)
        body["front"] = True
    if extra:
        body["extra_data"] = extra
    res = await comfy_json("POST", "/prompt", json=body)
    if res.get("node_errors"):
        raise HTTPException(400, json.dumps(res["node_errors"])[:2000])
    return res["prompt_id"]


async def wait_history(prompt_id: str, timeout: float = 300, cancelled: asyncio.Event | None = None) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        if cancelled and cancelled.is_set():
            raise HTTPException(499, "Mask cancelled")
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
    counter never matters; if the output folder is not reachable the name is kept. Already renamed
    (a second server that shares the run folder finished the same run) also gives the new name."""
    new = re.sub(r"_\d{5}_(\.\w+)$", r"\1", img["filename"])
    src = (COMFY_OUTPUT or Path(installer.load_config()["output_dir"])) / img.get("subfolder", "") / img["filename"]
    dst = src.with_name(new)
    if new == img["filename"] or img.get("type", "output") != "output":
        return img
    if not src.exists() and dst.is_file():
        return {**img, "filename": new}
    if not src.is_file() or dst.exists():
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
    download = INSTALLER.progress()   # summed bytes of the download queue, for the topbar button
    try:
        q = await comfy_json("GET", "/queue")
    except HTTPException as e:
        # boot: startup progress while the ComfyUI this server started is not answering yet (the loading screen)
        return {"comfy": False, "error": e.detail, "download": download, "boot": COMFY_PROC.boot()}
    return {"comfy": True, "running": len(q["queue_running"]), "pending": len(q["queue_pending"]), "download": download,
            "load": await asyncio.to_thread(system_load), "model_loaded": MODELS["loaded"]}


# Whether ComfyUI holds a model: set by every finished run or mask, cleared by Free memory and when ComfyUI restarts
# (another process id). ComfyUI has no API for its loaded models, so runs started elsewhere are not seen.
MODELS: dict[str, Any] = {"loaded": False, "comfy_pid": None}


def system_load() -> dict:
    pid = MODELS["comfy_pid"]
    if not pid or not psutil.pid_exists(pid):
        pid = sysload.comfy_pid(urlsplit(COMFY).port or 8188)
        if pid != MODELS["comfy_pid"]:
            MODELS.update(comfy_pid=pid, loaded=False)
    return sysload.snapshot(pid)


@app.post("/api/comfy/free")
async def free_memory():
    """Unloads ComfyUI's models and frees its cache (the next run loads its model again)."""
    if JOBS:
        raise HTTPException(409, "a run is queued or running; free memory when the queue is empty")
    await comfy_json("POST", "/free", json={"unload_models": True, "free_memory": True})
    MODELS["loaded"] = False
    return {"freed": True}


# ---------------------------------------------------------------- prompt history (edit/generate prompts, mask texts)

HISTORY_KINDS, HISTORY_MAX = ("prompts", "masks"), 50


def load_prompt_history() -> dict:
    try:
        data = json.loads((RUNS.parent / "prompt_history.json").read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    return {k: [t for t in data.get(k, []) if isinstance(t, str)] for k in HISTORY_KINDS}


def remember_prompt(kind: str, text: str) -> None:
    """Newest first, no duplicates, at most HISTORY_MAX per kind."""
    text = (text or "").strip()
    if not text:
        return
    data = load_prompt_history()
    data[kind] = [text, *[t for t in data[kind] if t != text]][:HISTORY_MAX]
    try:
        (RUNS.parent / "prompt_history.json").write_text(json.dumps(data, indent=1))
    except OSError:
        pass


@app.get("/api/prompt-history")
async def prompt_history():
    return load_prompt_history()


# ---------------------------------------------------------------- setup (first run, optional masking)

@functools.cache
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
    comps = [{"id": f"component:{cid}", "key": cid, "title": c["title"], "size": c["size"], "file": presets.file_name(c),
             "installed": have[f"component:{cid}"], "kind": c.get("kind"), "scale": c.get("scale"),
             "engine": c.get("engine"), "group": c.get("group"), "needs": c.get("needs", []), "families": c.get("families"),
             "strength": c.get("strength"), "repo": c["repo"], "description": c.get("description", ""),
             "imported": bool(c.get("imported")), "types": c.get("types")}
             for cid, c in presets.COMPONENTS.items()]
    ram = system_ram()
    for c in comps:
        if c["engine"] == "seedvr2":
            c["memory"] = presets.seedvr2_memory(c["key"])
            c["fit"] = presets.memory_fit(c["memory"], ram)
    models = []
    for pid, pr in presets.PRESETS.items():
        st = installer.preset_status(have, pid)
        models.append({
            "id": pid, "title": pr["title"], "family": pr["family"], "modes": pr["modes"], "note": pr["note"],
            "experimental": bool(pr.get("experimental")), "default_quant": pr["default_quant"], "defaults": pr["defaults"],
            "good_for": pr.get("good_for", ""), "recommended": bool(pr.get("recommended")), "imported": bool(pr.get("imported")),
            "recommended_quant": presets.recommended_quant(pid, ram),
            "text_encoder": pr["text_encoder"], "vae": pr["vae"], "nodes": pr.get("nodes", []), **st,
            "quants": [{"id": f"model:{pid}:{q}", "quant": q, "size": f["size"], "file": f["file"], "installed": have[f"model:{pid}:{q}"],
                        "memory": presets.memory_need(pid, q), "fit": presets.memory_fit(presets.memory_need(pid, q), ram)}
                       for q, f in pr["quants"].items()]})
    return {"ready": installer.ready(have), "mask_available": have["component:sam3"], "config": cfg,
            "system": {"ram": ram, "gpu_budget": int(ram * presets.GPU_SHARE), "seedvr2_ref_mp": presets.SEEDVR2_REF_MP},
            "base": base, "components": comps, "lora_groups": presets.LORA_GROUPS, "imports": imports.load(),
            "import_kinds": {k: label for k, (_, label) in imports.KINDS.items()}, "presets": models, "default_preset": presets.DEFAULT_PRESET,
            "comfy": {"up": await comfy_up(), "managed": COMFY_PROC.managed}, "install": INSTALLER.state()}


class ImportReq(BaseModel):
    path: str
    kind: str
    title: str = ""
    base: str = ""                 # diffusion model: the preset whose text encoder, VAE and settings it uses
    families: list[str] = []       # LoRA: the model families it works with
    scale: int | None = Field(None, ge=1, le=8)   # upscaler


@app.post("/api/imports/pick")
async def pick_import_file():
    """Opens the macOS file dialog on this Mac (the server runs here) and returns the chosen path with a guess
    of what it is. Cancelled: path null."""
    script = 'POSIX path of (choose file with prompt "Import a model file into Inpaint Studio")'
    proc = await asyncio.create_subprocess_exec("osascript", "-e", script, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    path = out.decode().strip()
    if proc.returncode != 0 or not path:
        return {"path": None}
    return await import_guess(path)


def local_file(url: str) -> Path:
    """The file on disk behind a result URL the page knows (/api/view of an output image, or /data/runs/...)."""
    parts = urlsplit(url)
    if parts.path == "/api/view":
        q = httpx.QueryParams(parts.query)
        if q.get("type", "output") != "output":
            raise HTTPException(400, "only result files can be saved")
        root = (COMFY_OUTPUT or Path(installer.load_config()["output_dir"])).resolve()
        path = (root / q.get("subfolder", "") / q.get("filename", "")).resolve()
    elif parts.path.startswith("/data/runs/"):
        root = RUNS.resolve()
        path = (root / parts.path.removeprefix("/data/runs/")).resolve()
    else:
        raise HTTPException(400, "unknown file")
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "file not found")
    return path


class RevealReq(BaseModel):
    url: str


@app.post("/api/reveal")
async def reveal_file(req: RevealReq):
    """Shows a result file in the Finder (the server runs on this Mac)."""
    path = local_file(req.url)
    subprocess.Popen(["open", "-R", str(path)])
    return {"path": str(path)}


class PickFolderReq(BaseModel):
    start: str = ""   # folder the dialog opens in (the last one used)


@app.post("/api/pick-folder")
async def pick_folder(req: PickFolderReq | None = None):
    """Opens the macOS folder dialog on this Mac and returns the chosen folder (cancelled: path null)."""
    start = Path(req.start).expanduser() if req and req.start else None
    where = f' default location (POSIX file "{start}")' if start and start.is_dir() and '"' not in str(start) else ""
    script = f'POSIX path of (choose folder with prompt "Save the results into this folder"{where})'
    proc = await asyncio.create_subprocess_exec("osascript", "-e", script, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    path = out.decode().strip()
    return {"path": path if proc.returncode == 0 and path else None}


class ExportItem(BaseModel):
    url: str
    name: str


class ExportReq(BaseModel):
    folder: str
    items: list[ExportItem] = Field(min_length=1, max_length=500)


@app.post("/api/export")
async def export_files(req: ExportReq):
    """Copies result files into a folder the user picked (an existing name gets a number: name (2).png)."""
    folder = Path(req.folder).expanduser()
    if not folder.is_dir():
        raise HTTPException(400, "the folder does not exist")
    saved = []
    for it in req.items:
        src = local_file(it.url)
        name = Path(it.name).name or src.name
        dst = folder / name
        n = 2
        while dst.exists():
            dst = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
            n += 1
        await asyncio.to_thread(shutil.copyfile, src, dst)
        saved.append(dst.name)
    return {"folder": str(folder), "saved": saved}


@app.get("/api/imports/guess")
async def import_guess(path: str):
    p = Path(path).expanduser()
    if not p.is_file():
        raise HTTPException(400, f"{p} is not a file")
    return {"path": str(p), "name": p.name, "size": p.stat().st_size, "kind": imports.guess_kind(p.name),
            "scale": imports.guess_scale(p.name), "supported": p.suffix.lower() in imports.EXTENSIONS}


@app.post("/api/imports")
async def add_import(req: ImportReq):
    try:
        item = imports.add(installer.load_config(), req.path, req.kind, req.title, req.base, req.families, req.scale)
    except (imports.ImportError_, OSError) as e:
        raise HTTPException(400, str(e)) from e
    return {"item": item, "setup": await setup_status()}


@app.delete("/api/imports/{import_id}")
async def remove_import(import_id: str):
    try:
        imports.remove(installer.load_config(), import_id)
    except KeyError as e:
        raise HTTPException(404, "import not found") from e
    return await setup_status()


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
    """Queue base steps, components ("component:<id>") and models ("model:<preset>:<quant>");
    a model's missing text encoder / VAE are added automatically. While a download runs, new items
    are appended to the queue."""
    if not req.items or not all(installer.valid_item(i) for i in req.items):
        raise HTTPException(400, "unknown or no items")
    cfg = installer.load_config()
    have = installer.installed(cfg)
    order = [i for i in installer.expand(req.items, have) if not have.get(i)]
    if not order:
        return await setup_status()

    async def done() -> None:
        finished = [i for i, st in INSTALLER.steps.items() if st["state"] == "done"]
        new_code = any(i in ("comfyui", "gguf_node") or (i.startswith("component:")
                       and presets.COMPONENTS[i.split(":")[1]]["folder"] == "custom_node") for i in finished)
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


class CancelReq(BaseModel):
    item: str | None = None  # None cancels the whole queue


@app.post("/api/setup/cancel")
async def setup_cancel(req: CancelReq):
    INSTALLER.cancel(req.item)
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
        "unets": unets, "clips": clips, "vaes": vaes, "loras": options("LoraLoaderModelOnly", "lora_name"),
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
    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
    except (OSError, Image.DecompressionBombError) as e:
        raise HTTPException(400, f"{file.filename or 'the file'} is not an image this app can read") from e
    safe_name = f"{uuid.uuid4().hex[:8]}_{Path(file.filename or 'image.png').name}"
    name = await upload_to_comfy(data, safe_name, SUBFOLDER)
    return {"name": name, "width": width, "height": height}


@app.post("/api/upload-mask")
async def upload_mask(file: UploadFile = File(...)):
    data = await file.read()
    name = await upload_to_comfy(data, f"mask_{uuid.uuid4().hex[:8]}.png", f"{SUBFOLDER}/masks")
    return {"name": name}


class SizeReq(BaseModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    megapixels: float = Field(0.95, gt=0)
    resolution: int = Field(1024, gt=0)
    mask_bbox: list[int] | None = None   # crop & stitch: mask bounding box in source pixels (x0, y0, x1, y1)
    crop_context: float = 0.5


@app.post("/api/size")
async def size(req: SizeReq):
    report = graphs.size_report(req.width, req.height, req.megapixels, req.resolution)
    report["suggested"] = graphs.safe_settings(req.width, req.height, req.megapixels, req.resolution)
    report["match_res"] = graphs.matching_resolution(report["work_w"], report["work_h"])
    if req.mask_bbox and len(req.mask_bbox) == 4:
        box = prepare.crop_box(tuple(req.mask_bbox), req.width, req.height, req.crop_context)
        crop = graphs.size_report(box["w"], box["h"], req.megapixels, req.resolution)
        report["crop"] = {**box, "work_w": crop["work_w"], "work_h": crop["work_h"],
                          "scale": round(crop["work_w"] / box["w"], 2)}
    return report


class MaskReq(BaseModel):
    image: str
    megapixels: float
    text: str
    threshold: float = 0.5
    refine: int = 2
    expand: int = 24
    invert: bool = False
    token: str = ""  # lets the page cancel this mask via /api/mask/{token}/cancel


MASKS: dict[str, tuple[str, asyncio.Event]] = {}  # token -> (prompt_id, cancelled) of masks in flight


@app.post("/api/mask")
async def mask(req: MaskReq):
    """Runs the mask graph and waits for it. Masks jump ahead of queued edit runs (front of ComfyUI's
    queue), so they only wait for the run that is currently sampling."""
    graph = graphs.build_mask_graph(req.image, req.megapixels, req.text, req.threshold, req.refine, req.expand, req.invert)
    remember_prompt("masks", req.text)
    t0 = time.time()
    pid = await submit(graph, f"inpaint-studio-{uuid.uuid4().hex[:6]}", front=True)
    token = req.token or pid
    cancelled = asyncio.Event()
    MASKS[token] = (pid, cancelled)
    try:
        entry = await wait_history(pid, timeout=1800, cancelled=cancelled)
    finally:
        MASKS.pop(token, None)
    imgs = entry.get("outputs", {}).get("out_mask", {}).get("images", [])
    if not imgs:
        raise HTTPException(500, "mask output missing")
    MODELS["loaded"] = True
    return {"mask_url": view_url(imgs[0]), "seconds": round(time.time() - t0, 1)}


@app.post("/api/mask/{token}/cancel")
async def cancel_mask(token: str):
    if token not in MASKS:
        raise HTTPException(404, "mask not found or already finished")
    pid, cancelled = MASKS[token]
    cancelled.set()
    q = await comfy_json("GET", "/queue")
    if pid in [item[1] for item in q["queue_running"]]:
        await client.post("/interrupt", json={"prompt_id": pid})
    elif pid in [item[1] for item in q["queue_pending"]]:
        await client.post("/queue", json={"delete": [pid]})
    return {"cancelled": True}


# ---------------------------------------------------------------- run history (persisted on disk)

def save_run(run: dict) -> None:
    d = RUNS / run["id"]
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "run.json.tmp"
    tmp.write_text(json.dumps(run, indent=1))
    tmp.replace(d / "run.json")


@app.get("/api/runs/{run_id}/comfyui.png")
async def comfyui_png(run_id: str):
    """The run's result with its ComfyUI graph in the PNG (the "prompt" text chunk ComfyUI writes itself), so
    dropping the file onto ComfyUI opens the exact workflow."""
    d = (RUNS / run_id).resolve()
    stored = load_job(run_id) if d.parent == RUNS.resolve() else None
    try:
        run = json.loads((d / "run.json").read_text())
    except (OSError, json.JSONDecodeError):
        run = None
    if not stored or not run or not run.get("result_url"):
        raise HTTPException(404, "this run has no result with a stored graph")
    img = await _fetch_view(run.get("crop_url") or run["result_url"])   # crop & stitch: the graph made the crop

    def encode() -> bytes:
        info = PngImagePlugin.PngInfo()
        info.add_text("prompt", json.dumps(stored["graph"]))
        buf = io.BytesIO()
        img.save(buf, "PNG", pnginfo=info)
        return buf.getvalue()
    return Response(await asyncio.to_thread(encode), media_type="image/png",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}_comfyui.png"'})


THUMBS = RUNS.parent / "thumbs"
THUMB_PX = 384


@app.get("/api/thumb")
async def thumb(src: str):
    """A small JPEG of a run image (/api/view?… or /data/runs/…) for the result tiles. Cached on disk by URL;
    URLs of files that get overwritten carry a ?t= stamp, so a cached thumb never goes stale."""
    key = hashlib.sha1(src.encode()).hexdigest()
    found = [p for p in (THUMBS / f"{key}.jpg", THUMBS / f"{key}.png") if p.exists()]
    if found:
        path = found[0]
    else:
        if src.startswith("/api/view?"):
            img = await _fetch_view(src)
        elif src.startswith("/data/runs/"):
            f = (RUNS / urlsplit(src).path.removeprefix("/data/runs/")).resolve()
            if not f.is_relative_to(RUNS.resolve()) or not f.is_file():
                raise HTTPException(404, "image not found")
            img = await asyncio.to_thread(Image.open, f)
        else:
            raise HTTPException(400, "not a run image")

        def make() -> Path:
            im = ImageOps.exif_transpose(img)
            im.thumbnail((THUMB_PX, THUMB_PX), Image.LANCZOS)
            alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
            out = THUMBS / f"{key}.{'png' if alpha else 'jpg'}"   # transparent results keep their alpha
            THUMBS.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(out.name + ".tmp")
            if alpha:
                im.convert("RGBA").save(tmp, "PNG")
            else:
                im.convert("RGB").save(tmp, "JPEG", quality=85)
            tmp.replace(out)
            return out
        path = await asyncio.to_thread(make)
    return FileResponse(path, media_type="image/png" if path.suffix == ".png" else "image/jpeg",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/runs")
async def list_runs(hidden: bool = False):
    """Finished runs, newest first; hidden=1 lists the ones removed from the history instead."""
    runs = []
    for f in RUNS.glob("*/run.json"):
        try:
            runs.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    # failed runs stay listed (crash, OOM, gray noise); runs the user cancelled do not
    runs = [r for r in runs if r.get("status") in ("done", "error") and bool(r.get("hidden")) == hidden]
    return sorted(runs, key=lambda r: r.get("created", 0), reverse=True)


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str):
    d = (RUNS / run_id).resolve()
    if d.parent != RUNS.resolve() or not d.is_dir():
        raise HTTPException(404, "run not found")
    if run_id in JOBS:
        raise HTTPException(409, "the run is still queued or running; cancel it first")
    shutil.rmtree(d)
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


class VersionReq(BaseModel):
    kind: str   # "result" (corrected (pasted) result), "whole" (corrected whole image of a paste), "raw" (untouched)


def shown_url(run: dict) -> str | None:
    """The result as Runs shows it (older runs kept post-processing in <run>_fixed.png / _grain.png / aligned.png)."""
    return run.get("fixed_url") or run.get("grain_url") or (run.get("aligned") or {}).get("url") or run.get("result_url")


def raw_view_url(run: dict) -> str | None:
    """The untouched image Runs shows as Raw / Clean: the raw image of a paste or the result before post-processing."""
    url = run.get("raw_url") or run.get("source_url")
    return url if url and url != shown_url(run) else None


def run_versions(run: dict) -> list[str]:
    """The images a run offers, in the order of the viewer's switch (web/app.js runViews)."""
    out = ["result"] if shown_url(run) else []
    if run.get("whole_url"):
        out.append("whole")
    if raw_view_url(run):
        out.append("raw")
    return out


@app.post("/api/runs/{run_id}/delete-version")
async def delete_version(run_id: str, req: VersionReq):
    """Deletes one image of a run and keeps the rest; the last one can only go with the whole run."""
    f = (RUNS / run_id / "run.json").resolve()
    if f.parent.parent != RUNS.resolve() or not f.is_file():
        raise HTTPException(404, "run not found")
    if run_id in JOBS:
        raise HTTPException(409, "the run is still queued or running")
    run = json.loads(f.read_text())
    versions = run_versions(run)
    if req.kind not in versions:
        raise HTTPException(400, "this run has no such image")
    if len(versions) < 2:
        raise HTTPException(400, "this is the run's only image; delete the run instead")

    def unlink(url: str | None) -> None:
        if not url:
            return
        try:
            local_file(url).unlink(missing_ok=True)
        except HTTPException:   # already gone or not a result file
            pass

    def drop_post() -> None:   # older runs: the post-processed file goes, the plain result is shown again
        for url in (run.get("fixed_url"), run.get("grain_url"), (run.get("aligned") or {}).get("url")):
            unlink(url)
        for k in ("fixed_url", "grain_url", "aligned"):
            run.pop(k, None)
        run["grain"] = False

    if req.kind == "whole":
        unlink(run.pop("whole_url"))
    elif req.kind == "raw":
        url = raw_view_url(run)
        unlink(url)
        for k in ("raw_url", "source_url"):
            if run.get(k) == url:
                run[k] = None
        if run.get("result_url") == url:   # older upscales: the clean upscale was the result file as well
            run["result_url"] = shown_url(run)
            for k in ("fixed_url", "grain_url"):
                run.pop(k, None)
    elif shown_url(run) != run["result_url"]:
        drop_post()
    else:   # the result goes: the corrected whole image takes its place, else the untouched one
        unlink(run["result_url"])
        if run.get("whole_url"):
            run["result_url"] = run.pop("whole_url")
        else:
            url = raw_view_url(run)
            run["result_url"] = url
            for k in ("raw_url", "source_url"):
                if run.get(k) == url:
                    run[k] = None
    parts = urlsplit(run["result_url"])   # a result now in the run dir keeps the run's name for downloads / saves
    run["filename"] = httpx.QueryParams(parts.query).get("filename") if parts.path == "/api/view" else f"{run_id}.png"
    save_run(run)
    return run


@app.post("/api/runs/{run_id}/retry")
async def retry_run(run_id: str):
    """Queues a failed (or any) run again with the same settings, as a new run."""
    stored = load_job(run_id) if (RUNS / run_id).resolve().parent == RUNS.resolve() else None
    if not stored:
        raise HTTPException(404, "this run cannot be retried (no job.json)")
    new_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    # the run id appears in output prefixes and file names; inputs made for the old run (crop, canvas) keep their name
    swap = re.compile(rf"(?<!{re.escape(SUBFOLDER)}/){re.escape(run_id)}")
    params = json.loads(swap.sub(new_id, json.dumps(stored["params"])))
    graph = json.loads(swap.sub(new_id, json.dumps(stored["graph"])))
    old = json.loads((RUNS / run_id / "run.json").read_text())
    run = {"id": new_id, "created": time.time(), "status": "queued", "params": old.get("params", {}),
           "size": old.get("size"), "frames": []}
    if old.get("before_url") and params.get("task") == "upscale":
        run["before_url"] = old["before_url"]
    if params.get("task") != "upscale":
        save_run_config(new_id, run, params, graph)
    return start_job(run, params, graph)


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
    img = Image.open(io.BytesIO(r.content))
    await asyncio.to_thread(img.load)   # decoding a big PNG must not stall the event loop
    return img


async def png_bytes(img: Image.Image) -> bytes:
    def encode() -> bytes:
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    return await asyncio.to_thread(encode)


class PostReq(BaseModel):
    auto: bool = False     # estimate the shift/scale (align.estimate) instead of dx, dy, scale
    dx: float = 0
    dy: float = 0
    scale: float = 1.0
    colors: bool = False
    warp: bool = False
    poisson: bool = False  # seamless edges (masked edits only)
    grain: bool = False    # the original's film / sensor grain (prepare.add_grain)
    grain_strength: float = Field(prepare.GRAIN_STRENGTH, ge=0, le=3)   # 1 = what the result lacks compared with the original
    save: bool = False


def grain_strength(params: dict) -> float:
    v = params.get("grain_strength")
    return prepare.GRAIN_STRENGTH if v is None else min(3.0, max(0.0, float(v)))


def fix_kind(run: dict) -> str | None:
    """Which alignment / colour / warp fixes a run can get: 'paste' (free edit + paste or extend canvas: raw image
    and mask), 'whole' (whole-image edit: the result itself against the original), 'upscale' (colours of the clean
    upscale, measured at the original's size) or None (grain only)."""
    p = run.get("params") or {}
    if p.get("task") == "upscale":
        return "upscale" if run.get("before_url") and (run.get("raw_url") or run.get("result_url")) else None
    if run.get("before_url") and run.get("raw_url") and run.get("mask_url"):
        return "paste"
    if (p.get("task") or "edit") == "edit" and p.get("use_mask") is False and not p.get("outpaint") \
            and run.get("before_url") and run.get("result_url"):
        return "whole"
    return None


def post_kind(run: dict) -> str | None:
    """fix_kind as offered in Post-processing: extend canvas runs are blended at the end of the run and only get grain."""
    return None if (run.get("params") or {}).get("outpaint") else fix_kind(run)


def untouched_url(run: dict, kind: str | None) -> str:
    """The image post-processing starts from, as the model / upscaler made it: the raw image of a paste
    (raw_url, in the run dir once post-processing was saved), else source_url (the untouched result, kept in the
    run dir by the first save) or, before that, the result itself."""
    if kind == "paste":
        return run["raw_url"]
    if run.get("source_url"):
        return run["source_url"]
    if kind == "upscale":   # older upscales: the clean upscale was raw_url, the result could hold the grain
        return run.get("raw_url") or run["result_url"]
    a = run.get("aligned") or {}
    if a.get("outpaint") and a.get("url"):   # older extend-canvas runs: the blend was aligned.png
        return a["url"]
    return run["result_url"]


async def _post_inputs(run: dict, kind: str | None) -> tuple:
    """(original, untouched image, mask) for a run, kept in memory for the run being adjusted."""
    src = untouched_url(run, kind)
    key = f"{run['id']}:{kind}:{src}"
    if key not in _align_cache:
        _align_cache.clear()
        mask = await _fetch_view(run["mask_url"]) if kind == "paste" else None
        _align_cache[key] = (await _fetch_view(run["before_url"]), await _fetch_view(src), mask)
    return _align_cache[key]


_BASE_DIFF: dict[tuple, float] = {}   # (run, kind, result) -> outside_diff of the unaligned paste


async def fix_image(run: dict, req: PostReq, kind: str, whole: bool = False) -> tuple[Image.Image, dict]:
    """Align and fix the edit (shift/scale, local warp, colours, seamless edge) and paste it into the original
    (whole-image edits: the whole fixed image). Returns the image and the numbers for the UI; whole (paste):
    result["whole"] is the corrected raw image as well."""
    original, raw, mask = await _post_inputs(run, kind)
    if kind == "upscale":   # an upscaler keeps the geometry: colours only
        img, stats = await asyncio.to_thread(align.match_colors_scaled, original, raw)
        return img, {"dx": 0, "dy": 0, "scale": 1.0, "colors": True, "warp": False, "poisson": False, **stats}
    params = run.get("params") or {}
    feather = int(params.get("feather") or 0)
    outpaint = bool(params.get("outpaint"))
    if outpaint:   # extend canvas: old image moved to where the model put it, wide fade, colour gain, no seam cut
        mask = align.outpaint_paste_mask(mask.resize(raw.size), 0.09 * max(raw.size))
        original, mask, moved = await asyncio.to_thread(align.outpaint_align, original, raw, mask)
    elif mask is not None and feather > 0 and "type=input" in run["mask_url"]:  # newer runs keep only the uploaded hard mask
        mask = mask.convert("L").filter(ImageFilter.GaussianBlur(max(1.0, feather / 3)))
    result: dict[str, Any] = {}
    if outpaint and moved.get("moved"):
        result["reframed"] = {k: round(moved[k], 3) for k in ("dx", "dy", "scale")}
    dx, dy, scale = req.dx, req.dy, req.scale
    if req.auto:
        est_mask = mask if mask is not None else Image.new("L", original.size, 0)
        est = await asyncio.to_thread(align.estimate, original, raw, est_mask)
        dx, dy, scale = est["dx"], est["dy"], est["scale"]
        result["confidence"] = est["confidence"]
    if not 0.8 <= scale <= 1.25 or abs(dx) > 500 or abs(dy) > 500:
        raise HTTPException(400, "alignment values out of range")
    opts = {"colors": req.colors, "warp": req.warp, "poisson": req.poisson and kind == "paste" and not outpaint}
    composed, stats = await asyncio.to_thread(align.compose, original, raw, mask, dx, dy, scale, **opts,
                                              color_gain=outpaint, whole=whole)
    if whole:
        result["whole"] = stats["whole"]
    key = (run["id"], kind, run.get("result_url"))
    if key not in _BASE_DIFF:   # the same for every slider position: compute once per run
        _, base = await asyncio.to_thread(align.compose, original, raw, mask, 0, 0, 1.0)
        _BASE_DIFF[key] = base["outside_diff"]
    result.update({"dx": dx, "dy": dy, "scale": scale, **opts, "outside_diff": stats["outside_diff"],
                   "unaligned_diff": _BASE_DIFF[key]})
    return composed, result


def _grain_mask_url(run: dict) -> str | None:
    """Masked edits get grain only in the mask (crop & stitch: the mask on the full original)."""
    p = run.get("params") or {}
    if p.get("crop_box") and p.get("orig_mask"):
        return input_mask_url(p["orig_mask"])
    return run.get("mask_url")


def _output_dir() -> Path:
    out = (COMFY_OUTPUT or Path(installer.load_config()["output_dir"])) / "InpaintStudio"
    out.mkdir(parents=True, exist_ok=True)
    return out


async def post_process(run: dict, req: PostReq, kind: str | None, internal: bool = False) -> dict:
    """Post-processing of a finished run: the fixes (fix_image, when kind allows them), then the grain, on top
    of each other, always from the untouched image (untouched_url). Without save a JPEG preview. With save the
    output folder keeps only the corrected images: <run>.png (the result; a paste: the corrected pasted result) and,
    for a paste, <run>_raw.png = the corrected whole generated image (whole_url). The untouched image moves into the
    run dir once (a paste: raw_url = raw.png, else source_url = source.png): the Raw / Clean view and the input of
    every later save. internal (crop & stitch before stitching): only aligned.png, the stitched result comes later."""
    run_id = run["id"]
    if not run.get("result_url"):
        raise HTTPException(400, "the run has no result")
    if kind == "upscale":
        fixes = req.colors
    else:
        fixes = kind is not None and (req.auto or req.dx or req.dy or req.scale != 1 or req.colors or req.warp or req.poisson)
    result: dict[str, Any] = {"kind": kind}
    original, untouched, _ = await _post_inputs(run, kind)
    whole = None
    if fixes or kind == "paste":   # a paste is composed again even without fixes (its untouched part is the raw image)
        img, info = await fix_image(run, req, kind, whole=req.save and kind == "paste" and not internal)
        whole = info.pop("whole", None)
        result.update(info)
    else:
        img = untouched
    stamp = int(time.time() * 1000)
    if req.save and fixes:
        run["aligned"] = {k: result[k] for k in ("dx", "dy", "scale", "colors", "warp", "poisson", "outside_diff")}
        if internal:   # crop & stitch pastes this one into the original
            await asyncio.to_thread(img.save, RUNS / run_id / "aligned.png")
            run["aligned"]["url"] = f"/data/runs/{run_id}/aligned.png?t={stamp}"
    elif req.save and not (run.get("aligned") or {}).get("outpaint"):
        run.pop("aligned", None)
    if req.grain:
        if not run.get("before_url"):
            raise HTTPException(400, "grain needs an original image (not for generated images)")
        mask_url = _grain_mask_url(run)
        mask = await _fetch_view(mask_url) if mask_url else None
        seed = int((run.get("params") or {}).get("seed") or 0)
        img = await asyncio.to_thread(prepare.add_grain, original, img, seed, mask, req.grain_strength)
        if whole is not None:   # the whole generated image gets the grain everywhere
            whole = await asyncio.to_thread(prepare.add_grain, original, whole, seed, None, req.grain_strength)
    if req.save:
        run["grain"], run["grain_strength"] = req.grain, req.grain_strength
        if not internal:
            await save_post_files(run, kind, untouched, img, whole, stamp, changed=fixes or req.grain)
        save_run(run)
        result["url"] = run["result_url"]
    else:
        # one file per request: overlapping slider moves must not show each other's half-written preview
        for old in (RUNS / run_id).glob("post_preview*.jpg"):
            old.unlink(missing_ok=True)
        name = f"post_preview_{stamp}.jpg"
        await asyncio.to_thread(lambda: img.convert("RGB").save(RUNS / run_id / name, quality=92))
        result["url"] = f"/data/runs/{run_id}/{name}"
    result.update(saved=req.save, aligned=run.get("aligned"), grain=run.get("grain"),
                  grain_strength=run.get("grain_strength"), run=run if req.save else None)
    return result


async def save_post_files(run: dict, kind: str | None, untouched: Image.Image, img: Image.Image,
                          whole: Image.Image | None, stamp: int, changed: bool = True) -> None:
    """Writes a saved post-processing so the output folder holds exactly what Runs shows. changed: the corrected
    images go over the output files and the untouched image is kept in the run dir (first save only; the Raw / Clean
    view and the input of later saves). Nothing on: the output files are the untouched images again and the run dir
    copy goes. Either way the files older versions used (<run>_fixed.png, <run>_grain.png, aligned.png) are removed."""
    run_id, d, out = run["id"], RUNS / run["id"], _output_dir()
    name = run.get("filename") or f"{run_id}.png"
    whole_name = f"{Path(name).stem}_raw.png"
    out_url = lambda n: view_url({"filename": n, "subfolder": "InpaintStudio", "type": "output"}) + f"&t={stamp}"
    for key in ("fixed_url", "grain_url"):
        if url := run.pop(key, None):
            try:
                local_file(url).unlink(missing_ok=True)
            except HTTPException:
                pass
    (d / "aligned.png").unlink(missing_ok=True)
    if run.get("aligned"):
        run["aligned"].pop("url", None)
    if not changed:
        if kind == "paste":   # the pasted result again (img) and the raw image back next to it
            await asyncio.to_thread(untouched.save, out / whole_name)
            run["raw_url"] = out_url(whole_name)
            run.pop("whole_url", None)
            (d / "raw.png").unlink(missing_ok=True)
        else:
            img = untouched
            run.pop("source_url", None)
            (d / "source.png").unlink(missing_ok=True)
        await asyncio.to_thread(img.save, out / name)
        run["result_url"] = out_url(name)
        return
    if kind == "paste":
        if not run["raw_url"].startswith("/data/runs/"):
            await asyncio.to_thread(untouched.save, d / "raw.png")
            run["raw_url"] = f"/data/runs/{run_id}/raw.png"
    elif not run.get("source_url"):
        await asyncio.to_thread(untouched.save, d / "source.png")
        run["source_url"] = f"/data/runs/{run_id}/source.png"
        if kind == "upscale":   # the clean upscale is source.png now
            run["raw_url"] = None
    await asyncio.to_thread(img.save, out / name)
    run["result_url"] = out_url(name)
    if whole is not None:
        await asyncio.to_thread(whole.save, out / whole_name)
        run["whole_url"] = out_url(whole_name)


@app.post("/api/runs/{run_id}/post")
async def post_run(run_id: str, req: PostReq):
    f = (RUNS / run_id / "run.json").resolve()
    if f.parent.parent != RUNS.resolve() or not f.is_file():
        raise HTTPException(404, "run not found")
    run = json.loads(f.read_text())
    if (run.get("params") or {}).get("remove_bg"):  # everything here works in RGB and would drop the transparency
        raise HTTPException(400, "transparent results (Remove background) have no post-processing")
    return await post_process(run, req, post_kind(run))


class UpscaleReq(BaseModel):
    image: str                    # ComfyUI input name (as returned by /api/upload)
    upscaler: str                 # component key of an installed upscaler
    upscale_of: str | None = None  # the edit run this upscale follows (queued automatically after it)
    factor: float = Field(2, gt=0)
    long_side: int | None = Field(None, gt=0)  # target length of the longer side in px instead of the factor (the other follows)
    megabytes: float | None = Field(None, gt=0)  # or a rough target file size (prepare.size_for_megabytes)
    color_correction: str = "lab"  # SeedVR2 only
    grain: bool = True            # give the result the original's grain back (prepare.add_grain)
    grain_strength: float = Field(prepare.GRAIN_STRENGTH, ge=0, le=3)


_bpp_cache: dict[str, float] = {}


async def megabytes_size(image: str, src: Image.Image, mb: float) -> tuple[int, int]:
    if image not in _bpp_cache:
        _bpp_cache[image] = await asyncio.to_thread(prepare.png_bytes_per_pixel, src)
    return prepare.size_for_megabytes(*src.size, _bpp_cache[image], mb)


class UpscalePlanReq(BaseModel):
    image: str
    megabytes: float = Field(gt=0)


@app.post("/api/upscale/plan")
async def upscale_plan(req: UpscalePlanReq):
    """The output size a target file size would give (shown in the form before the run)."""
    src = await _fetch_view(input_mask_url(req.image))
    w, h = await megabytes_size(req.image, src, req.megabytes)
    return {"width": w, "height": h, "factor": w / src.size[0]}


@app.post("/api/upscale")
async def upscale(req: UpscaleReq):
    """Upscale an input image with an upscaler picked in the model selector; queued as its own run
    (the source is its "before" image, so Runs can compare)."""
    comp = presets.COMPONENTS.get(req.upscaler)
    if not comp or comp.get("kind") != "upscaler":
        raise HTTPException(400, "unknown upscaler")
    cfg = installer.load_config()
    have = installer.installed(cfg)
    missing = [k for k in [req.upscaler, *comp.get("needs", [])] if not have[f"component:{k}"]]
    if missing:
        raise HTTPException(400, f"not installed: {', '.join(presets.COMPONENTS[k]['title'] for k in missing)} (see Download Center)")
    src = await _fetch_view(input_mask_url(req.image))
    w, h = src.size
    size = None
    if req.long_side:   # the longer of width / height gets it
        size = (req.long_side, max(1, round(h * req.long_side / w))) if w >= h else (max(1, round(w * req.long_side / h)), req.long_side)
    if req.megabytes and not req.long_side:
        size = await megabytes_size(req.image, src, req.megabytes)
    factor = size[0] / w if size else req.factor
    if not 1 <= round(factor, 3) <= 4:
        raise HTTPException(400, f"{w} × {h} px to {size[0]} × {size[1]} px would be ×{factor:.2f}; the factor must be between 1 and 4"
                            if size else "factor must be between 1 and 4")
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    files = {"model": presets.file_name(comp)}
    if comp.get("engine") == "seedvr2":
        files["vae"] = presets.file_name(presets.COMPONENTS[comp["needs"][0]])
    seed = int.from_bytes(os.urandom(4), "big")
    graph = graphs.build_upscale_graph(req.image, comp, files, factor, f"InpaintStudio/{run_id}",
                                       req.color_correction, seed, size)
    what = (f"to {req.long_side} px on the long side" if req.long_side else f"to about {req.megabytes:g} MB ({size[0]} px wide)"
            if req.megabytes else f"×{factor:g}")
    params = {"task": "upscale", "prompt": f"Upscale {what} with {comp['title']}", "upscale_of": req.upscale_of, "upscale": round(factor, 3),
              "upscale_long_side": req.long_side, "upscale_mb": None if req.long_side else req.megabytes, "grain": req.grain, "grain_strength": req.grain_strength, "image": req.image,
              "upscaler": req.upscaler, "color_correction": req.color_correction if comp.get("engine") == "seedvr2" else None,
              "seed": seed, "steps": 1, "save_every": 0, "save_last": 0, "refs": [], "use_mask": False, "mask": None}
    size = {"work_w": size[0], "work_h": size[1]} if size else {"work_w": round(w * factor), "work_h": round(h * factor)}
    run = {"id": run_id, "created": time.time(), "status": "queued", "before_url": input_mask_url(req.image),
           "params": {k: params.get(k) for k in HISTORY_PARAMS}, "size": size, "frames": []}
    return start_job(run, params, graph)


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
            "task": run["params"].get("task"), "upscaler": run["params"].get("upscaler"),
            "created": run["created"], "started": run.get("started"), "size": run.get("size"), "frames": run["frames"],
            "error": run.get("error"),
            "phase": job.get("phase"), "decode_steps": decode_steps(job["params"]), "reattached": job.get("reattached", False)}


async def finish_job(job: dict, status: str, **extra) -> None:
    if job.get("finished"):   # cancel and completion can race; the first one wins
        return
    job["finished"] = True
    run = job["run"]
    now = time.time()
    # took = time from the start of execution (model loading included), not the time waiting in the queue
    run.update({"status": status, "finished": now, "took": now - (run.get("started") or run["created"]), **extra})
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
                try:
                    msg = await asyncio.wait_for(cws.recv(), 30)
                except asyncio.TimeoutError:
                    # silence is normal during long steps; only a prompt ComfyUI no longer has is a problem
                    q = await comfy_json("GET", "/queue")
                    if pid in [item[1] for item in q["queue_running"] + q["queue_pending"]]:
                        continue
                    await follow_job(job)   # finished meanwhile (history) or gone
                    return
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
                    run.update(status="running", started=time.time())   # the UI's elapsed time survives reloads
                    save_run(run)
                    await broadcast({"type": "running", "job_id": run_id, "started": run["started"]})
                elif kind == "progress" and (node == "sampler" or node.startswith("chunk_")):
                    # chunked runs report per chunk; convert to overall step numbers
                    i = int(m[1]) if (m := re.fullmatch(r"chunk_(\d+)", node)) else -1
                    offset = chunk_starts[i] if 0 <= i < len(chunk_starts) else 0   # turbo graphs have chunk_0 even without saved steps
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
                    await complete_run(job, pid)
                    return
    except asyncio.CancelledError:
        raise
    except websockets.ConnectionClosed:
        if run.get("prompt_id"):   # our socket died, ComfyUI may still run the prompt: poll it instead
            await follow_job(job)
        else:
            await finish_job(job, "error", error="Lost the connection to ComfyUI")
    except HTTPException as e:
        await drop_prompt(run.get("prompt_id"))
        await finish_job(job, "error", error=str(e.detail))
    except Exception as e:  # keep the queue alive, report to the UI
        await drop_prompt(run.get("prompt_id"))
        await finish_job(job, "error", error=repr(e))


async def drop_prompt(pid: str | None) -> None:
    """A run that failed on our side must not keep ComfyUI busy: remove or interrupt its prompt."""
    if not pid:
        return
    try:
        q = await comfy_json("GET", "/queue")
        if pid in [item[1] for item in q["queue_running"]]:
            await client.post("/interrupt", json={"prompt_id": pid})
        elif pid in [item[1] for item in q["queue_pending"]]:
            await client.post("/queue", json={"delete": [pid]})
    except Exception:
        pass


async def follow_up_upscale(run: dict, params: dict) -> None:
    """An edit with Upscale on: its result (with post-processing) is upscaled as a run of its own."""
    up = params.get("then_upscale")
    src = run.get("fixed_url") or run.get("result_url")
    if not up or run.get("status") != "done" or not src:
        return
    try:
        img = await _fetch_view(src)
        name = await upload_to_comfy(await png_bytes(img), f"{run['id']}_for_upscale.png", SUBFOLDER)
        await upscale(UpscaleReq(image=name, upscaler=up["upscaler"], factor=up["factor"],
                                 color_correction=up["color_correction"], upscale_of=run["id"]))
    except Exception as e:   # the edit itself is done and stays so
        print(f"follow-up upscale of {run['id']} failed: {e!r}")


async def complete_run(job: dict, pid: str) -> None:
    MODELS["loaded"] = True
    await _complete_run(job, pid)
    await follow_up_upscale(job["run"], job["params"])


async def _complete_run(job: dict, pid: str) -> None:
    """Collects the outputs of a finished prompt, runs the automatic paste fixes and finishes the job."""
    run, params, run_id = job["run"], job["params"], job["run"]["id"]
    entry = await wait_history(pid, timeout=30)
    if any(kind == "execution_interrupted" for kind, _ in entry.get("status", {}).get("messages", [])):
        await finish_job(job, "cancelled", error="Cancelled")
        return
    outs = entry.get("outputs", {})
    def first(key: str) -> dict | None:
        img = (outs.get(key, {}).get("images") or [None])[0]
        return drop_counter(img) if img else None
    res, before, raw, upscaled = first("out_result"), first("out_before"), first("out_raw"), first("out_upscaled")
    if control := first("out_control"):
        run["control_url"] = view_url(control)
    if not res:
        await finish_job(job, "error", error="ComfyUI finished without a result image")
        return
    use_mask = params.get("use_mask") and params.get("mask")
    run.update(before_url=view_url(before) if before else run.get("before_url"), raw_url=view_url(raw) if raw else None,
               mask_url=input_mask_url(params["mask"]) if use_mask else None, result_url=view_url(res) if res else None)
    post = {k: bool(params.get(f"post_{k}")) for k in ("colors", "warp", "poisson")} | {"auto": bool(params.get("post_align"))}
    if params.get("outpaint") and params.get("mode") != "paste" and before and raw:
        try:  # the blend replaces the plain paste in <run>.png
            await outpaint_fix(run, params, view_url(before), view_url(raw))
        except Exception as e:
            print(f"outpaint blend failed for {run_id}: {e!r}")
    # automatic post-processing (free edit + paste, whole image): the corrected images replace the output files, the
    # untouched one moves into the run dir (post_process); crop & stitch adds its grain when stitching, upscales below
    kind = fix_kind(run) if (params.get("mode") == "paste" and use_mask) or not use_mask else None
    fixes = kind is not None and any(post.values())
    grain = bool(params.get("post_grain")) and (params.get("task") or "edit") == "edit" and not params.get("crop_box")
    if res and (fixes or grain):
        try:
            await post_process(run, PostReq(save=True, grain=grain, grain_strength=grain_strength(params),
                                            **(post if fixes else {})), kind if fixes else None,
                               internal=bool(params.get("crop_box")))
        except Exception as e:  # never fail the run because of the post-processing
            print(f"post-processing failed for {run_id}: {e!r}")
    if params.get("task") == "upscale" and params.get("grain") and res:
        # the grained upscale replaces <run>.png, the clean one moves to source.png ("Clean" in Runs)
        run.update(result_url=view_url(res), raw_url=None)
        try:
            await post_process(run, PostReq(save=True, grain=True, grain_strength=grain_strength(params)), "upscale")
        except Exception as e:  # never fail the run because of the post-processing
            print(f"grain failed for {run_id}: {e!r}")
        await finish_job(job, "done", upscaled_url=None, filename=res["filename"])
        return
    if params.get("crop_box") and res:   # crop & stitch: the run's result is the full-size original with the edit
        full = await stitch_result(run, params, view_url(res))
        await finish_job(job, "done", result_url=view_url(full), crop_url=view_url(res), before_url=input_mask_url(params["orig_image"]),
                         raw_url=None, aligned=None, upscaled_url=None, mask_url=input_mask_url(params["orig_mask"]),
                         filename=full["filename"], crop_box=params["crop_box"])
        return
    # result_url / raw_url as the post-processing left them (the corrected files, the raw image in the run dir)
    await finish_job(job, "done", upscaled_url=view_url(upscaled) if upscaled else None, filename=res["filename"])


def start_job(run: dict, params: dict, graph: dict) -> dict:
    """Saves the run (and job.json with everything needed to follow or retry it) and starts following it."""
    save_run(run)
    (RUNS / run["id"] / "job.json").write_text(json.dumps({"params": params, "graph": graph}, default=str))
    job = {"run": run, "params": params, "graph": graph, "value": 0}
    JOBS[run["id"]] = job
    job["task"] = asyncio.create_task(run_job(job))
    return job_summary(job)


async def follow_job(job: dict) -> None:
    """Follows a prompt submitted by an earlier server process (reattached after a restart). ComfyUI sends
    progress and previews only to the client that submitted it, so this only polls queue and history."""
    run, pid = job["run"], job["run"]["prompt_id"]
    fails = 0
    try:
        while True:
            try:
                hist = await comfy_json("GET", f"/history/{pid}")
                q = None if pid in hist else await comfy_json("GET", "/queue")
                fails = 0
            except asyncio.CancelledError:
                raise
            except Exception:   # one failed poll (ComfyUI busy, 502) is not the end of the run
                fails += 1
                if fails >= 5:
                    raise
                await asyncio.sleep(2)
                continue
            if q is None:
                await complete_run(job, pid)
                return
            if pid in [item[1] for item in q["queue_running"]]:
                if run["status"] != "running":
                    run.update(status="running", started=run.get("started") or time.time())
                    save_run(run)
                    await broadcast({"type": "running", "job_id": run["id"], "started": run["started"]})
            elif pid not in [item[1] for item in q["queue_pending"]]:
                await finish_job(job, "error", error="The prompt is no longer in ComfyUI"
                                 if not job.get("reattached") else "Server restarted before the job finished")
                return
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        raise
    except HTTPException as e:
        await finish_job(job, "error", error=str(e.detail))
    except Exception as e:
        await finish_job(job, "error", error=repr(e))


def load_job(run_id: str) -> dict | None:
    try:
        return json.loads((RUNS / run_id / "job.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None


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


async def load_input(name: str) -> Image.Image:
    """An uploaded input image, turned upright like ComfyUI's LoadImage does."""
    img = await _fetch_view(input_mask_url(name))
    return await asyncio.to_thread(lambda: ImageOps.exif_transpose(img).convert("RGB"))


async def crop_input(params: dict, run_id: str) -> None:
    """Crop & stitch: the edit runs on a crop around the mask (at the full working size, so more detail);
    the original stays in orig_image and the result is pasted back after the run (stitch_result)."""
    orig = await load_input(params["image"])
    mask = await _fetch_view(input_mask_url(params["mask"]))
    mask = await asyncio.to_thread(lambda: mask.convert("L").resize(orig.size, Image.BILINEAR))
    bbox = prepare.mask_bbox(mask)
    if not bbox:
        raise HTTPException(400, "the mask is empty")
    box = prepare.crop_box(bbox, *orig.size, context=float(params.get("crop_context") or 0.5))
    files = []
    for img, name in ((prepare.crop(orig, box), f"{run_id}_crop.png"), (prepare.crop(mask, box), f"{run_id}_cropmask.png")):
        files.append(await upload_to_comfy(await png_bytes(img), name, SUBFOLDER))
    params.update(orig_image=params["image"], orig_mask=params["mask"], crop_box=box, orig_size=list(orig.size),
                  image=files[0], mask=files[1], src_w=box["w"], src_h=box["h"], upscale=0)
    fit_size(params)


def fit_size(params: dict) -> None:
    """The input got another shape (crop, outpaint canvas): keep it below the token limit and match the encoder size."""
    w, h = params["src_w"], params["src_h"]
    if not graphs.size_report(w, h, params["megapixels"], params["resolution"])["safe"]:
        params.update({k: v for k, v in graphs.safe_settings(w, h, params["megapixels"], params["resolution"]).items()
                       if k in ("megapixels", "resolution")})
    if params.get("match_ref", True):
        work = graphs.size_report(w, h, params["megapixels"], params["resolution"])
        params["resolution"] = graphs.matching_resolution(work["work_w"], work["work_h"])


async def outpaint_input(params: dict, run_id: str) -> None:
    """Extend canvas: the image is placed on a larger canvas and the new area is inpainted."""
    o = params["outpaint"]
    try:
        cw, ch, x, y = (int(o[k]) for k in ("canvas_w", "canvas_h", "x", "y"))
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(400, "outpaint needs canvas_w, canvas_h, x and y") from e
    orig = await load_input(params["image"])
    f = min(1.0, (40_000_000 / max(1, cw * ch)) ** 0.5)
    if f < 1:   # a small image on a big canvas: the run works at ~1 MP anyway, so pad a smaller copy
        orig = await asyncio.to_thread(orig.resize, (max(1, round(orig.width * f)), max(1, round(orig.height * f))), Image.LANCZOS)
        cw, ch = round(cw * f), round(ch * f)
        x, y = min(round(x * f), cw - orig.width), min(round(y * f), ch - orig.height)
        params["outpaint"] = {**o, "canvas_w": cw, "canvas_h": ch, "x": x, "y": y}
    holes = (await _fetch_view(input_mask_url(o["erase"]))).convert("L") if o.get("erase") else None   # erased parts
    try:
        canvas, mask = await asyncio.to_thread(prepare.pad, orig, x, y, cw, ch, prepare.OUTPAINT_OVERLAP, holes)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    files = []
    parts = [(canvas, f"{run_id}_canvas.png"), (mask, f"{run_id}_canvasmask.png")]
    if holes is not None:   # where the erased parts are on the canvas, for the blend after the run
        grow = max(2, prepare.OUTPAINT_OVERLAP // 4)
        parts.append((Image.fromarray(prepare.hole_mask(holes, x, y, orig.size, cw, ch, grow)), f"{run_id}_holes.png"))
    for img, name in parts:
        files.append(await upload_to_comfy(await png_bytes(img), name, SUBFOLDER))
    if holes is not None:
        params["outpaint_holes"] = files[2]
    # "inpaint": only the new area is generated (noise mask). "paste": the model redraws the whole canvas and the
    # new area is pasted around the old image, with the usual paste fixes (alignment, colours, seam)
    # paste is the default: compared on 2026-10-03 (UC Q8, 20 steps), inpaint left a blurred band along the old edge
    method = "inpaint" if o.get("method") == "inpaint" else "paste"
    params.update(orig_image=params["image"], orig_size=list(orig.size), image=files[0], mask=files[1], use_mask=True,
                  mode=method, src_w=cw, src_h=ch, crop_stitch=False, denoise=1.0)
    if method == "paste":   # the paste fixes, measured on the old image: colours (the checkbox) and a seamless edge
        params.update(post_colors=bool(params.get("outpaint_colors", True)), post_poisson=True, post_warp=False)
    fit_size(params)


async def outpaint_fix(run: dict, params: dict, before_url: str, raw_url: str) -> None:
    """Extend canvas: blend the generated border past the model's halo, optionally colour-matched (align.outpaint_blend);
    the blend replaces the plain paste (<run>.png), the raw image stays."""
    run_id, o = run["id"], params["outpaint"]
    before, raw = await _fetch_view(before_url), await _fetch_view(raw_url)
    s = raw.size[0] / int(o["canvas_w"])   # canvas px -> working px
    ow, oh = params["orig_size"]
    box = (round(int(o["x"]) * s), round(int(o["y"]) * s), round((int(o["x"]) + ow) * s), round((int(o["y"]) + oh) * s))
    holes = None
    if params.get("outpaint_holes"):
        holes = np.asarray((await _fetch_view(input_mask_url(params["outpaint_holes"]))).convert("L").resize(raw.size, Image.BILINEAR))
    fixed = await asyncio.to_thread(align.outpaint_blend, before, raw, box, round(prepare.OUTPAINT_OVERLAP * s),
                                    bool(params.get("outpaint_colors", True)), holes)
    await asyncio.to_thread(fixed.save, local_file(run["result_url"]))
    run["aligned"] = {"outpaint": True, "colors": bool(params.get("outpaint_colors", True))}


async def stitch_result(run: dict, params: dict, crop_url: str) -> dict:
    """Pastes the finished crop (the automatically fixed one if there is one) back into the original."""
    run_id, box = run["id"], params["crop_box"]
    fixed = RUNS / run_id / "aligned.png"
    result = Image.open(fixed) if run.get("aligned") and fixed.exists() else await _fetch_view(crop_url)
    orig = await load_input(params["orig_image"])
    mask = (await _fetch_view(input_mask_url(params["orig_mask"]))).convert("L")
    feather = float(params.get("feather") or 0) * box["w"] / max(1, params["work_w"])   # working px -> source px
    full = await asyncio.to_thread(prepare.stitch, orig, result, mask, box, feather / 3, bool(params.get("crop_grain", True)),
                                    grain_strength=grain_strength(params))
    out_dir = COMFY_OUTPUT or Path(installer.load_config()["output_dir"])
    (out_dir / "InpaintStudio").mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(full.save, out_dir / "InpaintStudio" / f"{run_id}_full.png")
    return {"filename": f"{run_id}_full.png", "subfolder": "InpaintStudio", "type": "output"}


class JobCheck(BaseModel):
    """The fields create_job and the graph builders read without a default; everything else passes through."""
    model_config = {"extra": "allow"}
    task: str = "edit"
    prompt: str = ""
    steps: int = Field(gt=0, le=1000)
    megapixels: float = Field(gt=0, le=64)
    resolution: int = Field(gt=0)
    src_w: int = Field(gt=0)
    src_h: int = Field(gt=0)
    seed: int = Field(0, ge=0)


@app.post("/api/jobs")
async def create_job(params: dict):
    try:
        JobCheck.model_validate(params)
    except ValidationError as e:
        bad = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        raise HTTPException(400, f"invalid job: {bad}") from e
    if params.get("task", "edit") != "generate" and not params.get("image"):
        raise HTTPException(400, "invalid job: image is missing")
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
        # the upscale becomes its own run once the edit is done (two results: the edit and its upscale)
        have = installer.installed(installer.load_config())
        missing = [k for k in [params["upscaler"], *up.get("needs", [])] if not have[f"component:{k}"]]
        if missing:
            raise HTTPException(400, f"not installed: {', '.join(presets.COMPONENTS[k]['title'] for k in missing)} (see Download Center)")
        params["then_upscale"] = {"upscaler": params["upscaler"], "factor": int(params["upscale"]),
                                  "color_correction": params.get("color_correction") or "lab"}
    params["upscale"] = 0
    if params.get("remove_bg"):  # transparent PNG: whole image only, and nothing afterwards that works in RGB
        if params.get("task", "edit") != "edit" or params.get("family") not in graphs.REMOVE_BG_FAMILIES:
            raise HTTPException(400, "Remove background needs an edit with Qwen-Image 2.1")
        if params.get("outpaint"):
            raise HTTPException(400, "Remove background does not work with Extend canvas")
        params.pop("then_upscale", None)
        params.update(use_mask=False, mask=None, crop_stitch=False, upscale=0, keep_whole=False,
                      **{f"post_{k}": False for k in ("align", "colors", "warp", "poisson", "grain")})
    if params.get("control"):   # control guidance (generate): the patch for this family and map type must be installed
        c = params["control"]
        if params.get("task") != "generate" or not isinstance(c, dict) or not c.get("image"):
            raise HTTPException(400, "control guidance needs Generate and a control image")
        if c.get("type") not in ("canny", "depth"):
            raise HTTPException(400, "control type must be canny or depth")
        key = presets.control_patch(params.get("family", ""), c["type"])
        if not key:
            raise HTTPException(400, f"no {c['type']} guidance for this model (Z-Image and Qwen-Image 2512 have it)")
        source = c.get("source") or ("map" if c.get("is_map") else "photo")
        if source not in ("photo", "drawing", "map") or (source == "drawing" and c["type"] != "canny"):
            raise HTTPException(400, "control source must be photo, drawing (edges only) or map")
        need = [key] + (["da3_small"] if c["type"] == "depth" and source == "photo" else [])
        have = installer.installed(installer.load_config())
        missing = [presets.COMPONENTS[k]["title"] for k in need if not have[f"component:{k}"]]
        if missing:
            raise HTTPException(400, f"not installed: {', '.join(missing)} (see Download Center → Control)")
        params["control"] = {"type": c["type"], "image": c["image"], "source": source,
                             "strength": float(c.get("strength", presets.COMPONENTS[key].get("strength", 1.0))),
                             "end": float(c.get("end", 1.0))}
        params["control_patch"] = presets.file_name(presets.COMPONENTS[key])
        params["control_depth_model"] = presets.file_name(presets.COMPONENTS["da3_small"])
    if params.get("family") == "qwen21_turbo":  # fixed few-step schedule, no CFG
        params.update(steps=graphs.turbo_steps(params["steps"]), cfg=1.0)
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    if params.get("crop_stitch") and params.get("use_mask") and params.get("mask") and params.get("task", "edit") == "edit":
        await crop_input(params, run_id)
    elif params.get("outpaint") and params.get("task", "edit") == "edit" and params.get("image"):
        await outpaint_input(params, run_id)
    rep = graphs.size_report(params["src_w"], params["src_h"], params["megapixels"], params["resolution"])
    params["work_w"], params["work_h"] = rep["work_w"], rep["work_h"]
    params["prefix"] = f"InpaintStudio/{run_id}"
    graph = graphs.build_edit_graph(params)
    run = {"id": run_id, "created": time.time(), "status": "queued",
           "params": {k: params.get(k) for k in HISTORY_PARAMS}, "size": rep, "frames": []}
    save_run_config(run_id, run, params, graph)
    remember_prompt("prompts", params.get("prompt", ""))
    return start_job(run, params, graph)


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
    try:
        await job["task"]
    except (asyncio.CancelledError, Exception):
        pass
    await drop_prompt(job["run"].get("prompt_id"))   # submitted while we were cancelling
    await finish_job(job, "cancelled", error="Removed from queue")
    return {"cancelled": True, "was": "queued"}


@app.websocket("/ws/jobs")
async def ws_jobs(ws: WebSocket):
    if not local_origin(ws.headers.get("host"), ws.headers.get("origin")):
        await ws.close(code=1008)
        return
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


async def start_comfy():
    try:
        await ensure_comfy()
    except Exception as e:  # never block the UI (it shows the setup page instead)
        print(f"could not start ComfyUI: {e!r}")


async def reattach_runs():
    """Runs left "queued"/"running" by a previous server process: if ComfyUI still has the prompt (e.g. Comfy
    Desktop kept running), follow it again; otherwise the run failed."""
    up = await comfy_up()
    for f in RUNS.glob("*/run.json"):
        try:
            run = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if run.get("status") not in ("queued", "running"):
            continue
        stored = load_job(run["id"])
        if up and stored and run.get("prompt_id"):
            job = {"run": run, "params": stored["params"], "graph": stored["graph"], "value": 0, "reattached": True}
            JOBS[run["id"]] = job
            job["task"] = asyncio.create_task(follow_job(job))
        else:
            run.update({"status": "error", "error": "Server restarted before the job finished"})
            save_run(run)


@app.get("/")
async def index():
    if FRESH_INSTALL and not GUIDE_SEEN.exists():
        return FileResponse(WEB / "guide.html")
    return FileResponse(WEB / "index.html")


@app.post("/api/guide/seen")
async def guide_seen():
    """The guide was shown: "/" opens the app from now on."""
    GUIDE_SEEN.parent.mkdir(parents=True, exist_ok=True)
    GUIDE_SEEN.touch()
    return {"ok": True}


app.mount("/data/runs", StaticFiles(directory=RUNS), name="runs")
app.mount("/", StaticFiles(directory=WEB), name="web")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT") or 7380))
