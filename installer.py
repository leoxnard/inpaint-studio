"""First-run setup: find or install ComfyUI, the GGUF custom node and the models, and run ComfyUI.

Everything a fresh Mac needs besides this app itself (uv + the app's Python deps are installed
by the Mac launcher before the server can start). Downloads come from GitHub and Hugging Face,
no accounts needed. An existing Comfy Desktop install is detected and reused.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

import presets

APP_SUPPORT = Path(os.environ.get("INPAINT_STUDIO_HOME") or Path.home() / "Library/Application Support/Inpaint Studio")
CONFIG_FILE = APP_SUPPORT / "config.json"
COMFY_DESKTOP = Path.home() / "Library/Application Support/Comfy Desktop"
COMFY_PORT = 8188
COMFY_ZIP = "https://github.com/comfyanonymous/ComfyUI/archive/refs/tags/v0.38.0.zip"
# Pinned on 2026-10-03: patch_gguf_loader regex-patches this code, so an unpinned main could break it
GGUF_ZIP = "https://github.com/city96/ComfyUI-GGUF/archive/6ea2651e7df66d7585f6ffee804b20e92fb38b8a.zip"
HF = "https://huggingface.co/{repo}/resolve/main/{path}"

BASE_STEPS = [  # id, title, description
    ("comfyui", "ComfyUI", "Image generation engine with its own Python environment (PyTorch etc., ~1.5 GB)"),
    ("gguf_node", "GGUF loader", "ComfyUI-GGUF custom node, patched for Qwen-Image 2.1"),
]
MODEL_FOLDERS = ["checkpoints", "clip", "clip_vision", "diffusion_models", "unet", "text_encoders", "vae",
                 "vae_approx", "loras", "upscale_models", "embeddings", "controlnet"]


# ---------------------------------------------------------------- config

def _desktop_paths() -> dict[str, str]:
    """Paths of an existing Comfy Desktop install, if any."""
    found: dict[str, str] = {}
    for main in sorted(Path.home().glob("ComfyUI-Installs/*/ComfyUI/main.py")):
        found["comfy_dir"] = str(main.parent)
        break
    for yaml in sorted((COMFY_DESKTOP / "instance-model-paths").glob("*.yaml")):
        m = re.search(r"^[ \t]+base_path:\s*'([^']+)'", yaml.read_text(errors="ignore"), re.MULTILINE)
        if m:
            models = Path(m.group(1))
            found["models_dir"] = str(models)
            for k in ("input", "output"):
                if (models.parent / k).is_dir():
                    found[f"{k}_dir"] = str(models.parent / k)
            break
    return found


def defaults() -> dict[str, str]:
    d = {"comfy_dir": str(APP_SUPPORT / "ComfyUI"), "models_dir": str(APP_SUPPORT / "models"),
         "input_dir": str(APP_SUPPORT / "input"), "output_dir": str(APP_SUPPORT / "output")}
    d.update(_desktop_paths())
    return d


def load_config() -> dict[str, str]:
    cfg = defaults()
    try:
        cfg.update({k: v for k, v in json.loads(CONFIG_FILE.read_text()).items() if k in cfg and v})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_config(values: dict[str, Any]) -> dict[str, str]:
    cfg = load_config()
    for k in cfg:
        if values.get(k):
            cfg[k] = str(Path(str(values[k])).expanduser())
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=1))
    return cfg


def venv_python(cfg: dict) -> Path:
    return Path(cfg["comfy_dir"]) / ".venv/bin/python3"


# ---------------------------------------------------------------- what is installed

def _model_file(cfg: dict, folders: list[str], name: str) -> Path | None:
    for f in folders:
        p = Path(cfg["models_dir"]) / f / name
        if p.exists():  # follows symlinks (e.g. into LM Studio's folder)
            return p
    return None


def item_path(cfg: dict, item: str) -> Path | None:
    """Existing file of a downloadable item ("component:<id>" or "model:<preset>:<quant>")."""
    kind, _, rest = item.partition(":")
    if kind == "component":
        c = presets.COMPONENTS[rest]
        if c["folder"] == "custom_node":  # a single-file ComfyUI custom node
            f = Path(cfg["comfy_dir"]) / "custom_nodes" / Path(c["path"]).name
            return f if f.is_file() else None
        folders = [c["folder"], "clip"] if c["folder"] == "text_encoders" else [c["folder"]]
        return _model_file(cfg, folders, Path(c["path"]).name)
    pid, _, q = rest.partition(":")
    return _model_file(cfg, ["diffusion_models", "unet"], presets.PRESETS[pid]["quants"][q]["file"])


def item_target(cfg: dict, item: str) -> tuple[str, Path, int]:
    """Download URL, destination and size of an item."""
    kind, _, rest = item.partition(":")
    if kind == "component":
        c = presets.COMPONENTS[rest]
        base = Path(cfg["comfy_dir"]) / "custom_nodes" if c["folder"] == "custom_node" else Path(cfg["models_dir"]) / c["folder"]
        return HF.format(repo=c["repo"], path=c["path"]), base / Path(c["path"]).name, c["size"]
    pid, _, q = rest.partition(":")
    pr = presets.PRESETS[pid]
    f = pr["quants"][q]
    return HF.format(repo=pr["repo"], path=f["file"]), Path(cfg["models_dir"]) / "diffusion_models" / f["file"], f["size"]


def item_title(item: str) -> str:
    kind, _, rest = item.partition(":")
    if kind == "component":
        return presets.COMPONENTS[rest]["title"]
    if kind == "model":
        pid, _, q = rest.partition(":")
        return f"{presets.PRESETS[pid]['title']} · {q}"
    return dict((s, t) for s, t, _ in BASE_STEPS)[item]


def valid_item(item: str) -> bool:
    kind, _, rest = item.partition(":")
    if kind == "component":
        return rest in presets.COMPONENTS
    if kind == "model":
        pid, _, q = rest.partition(":")
        return pid in presets.PRESETS and q in presets.PRESETS[pid]["quants"]
    return item in dict((s, t) for s, t, _ in BASE_STEPS)


def installed(cfg: dict) -> dict[str, bool]:
    """Base steps plus every component and every preset quantisation."""
    loader = Path(cfg["comfy_dir"]) / "custom_nodes/ComfyUI-GGUF/loader.py"
    have = {
        "comfyui": (Path(cfg["comfy_dir"]) / "main.py").is_file() and venv_python(cfg).exists(),
        "gguf_node": loader.is_file() and "qwen_image21" in loader.read_text(errors="ignore"),
    }
    for cid in presets.COMPONENTS:
        have[f"component:{cid}"] = item_path(cfg, f"component:{cid}") is not None
    for pid, pr in presets.PRESETS.items():
        for q in pr["quants"]:
            have[f"model:{pid}:{q}"] = item_path(cfg, f"model:{pid}:{q}") is not None
    return have


def preset_status(have: dict[str, bool], pid: str) -> dict[str, Any]:
    pr = presets.PRESETS[pid]
    quants = [q for q in pr["quants"] if have[f"model:{pid}:{q}"]]
    comps = needed_components(pid)
    return {"installed_quants": quants, "missing_components": [c for c in comps if not have[f"component:{c}"]],
            "complete": bool(quants) and all(have[f"component:{c}"] for c in comps)}


def needed_components(pid: str) -> list[str]:
    pr = presets.PRESETS[pid]
    return [pr["text_encoder"], pr["vae"], *pr.get("nodes", [])]


def ready(have: dict[str, bool]) -> bool:
    return have["comfyui"] and have["gguf_node"] and any(preset_status(have, pid)["complete"] for pid in presets.PRESETS)


def expand(items: list[str], have: dict[str, bool]) -> list[str]:
    """Install order: base steps, then components (incl. the ones a chosen model needs), then models."""
    want = set(items)
    for it in items:
        if it.startswith("model:"):
            want.update(f"component:{c}" for c in needed_components(it.split(":")[1]) if not have[f"component:{c}"])
    base = [s for s, _, _ in BASE_STEPS if s in want]
    comps = [f"component:{c}" for c in presets.COMPONENTS if f"component:{c}" in want]
    models = sorted(i for i in want if i.startswith("model:"))
    return base + comps + models


# ---------------------------------------------------------------- install

class Installer:
    """Download queue: items run one after another; more can be queued while one is running."""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.current: asyncio.Task | None = None
        self.skip = False
        self.steps: dict[str, dict] = {}
        self.error: str | None = None
        self.restart_hint = False

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def state(self) -> dict:
        return {"running": self.running, "steps": self.steps, "error": self.error, "restart_comfy": self.restart_hint}

    def start(self, order: list[str], on_done) -> None:
        """Queue items (already queued or running ones are ignored); starts the worker when idle.
        on_done runs once the queue is empty."""
        if not self.running:
            self.steps = {}
            self.error = None
            self.restart_hint = False
        for s in order:
            if self.steps.get(s, {}).get("state") in ("pending", "running"):
                continue
            self.steps.pop(s, None)  # re-queued after an error or cancel: move to the end
            self.steps[s] = {"state": "pending", "title": item_title(s), "message": "", "done": 0, "total": None, "rate": 0}
        if not self.running:
            self.task = asyncio.create_task(self._run(on_done))

    def cancel(self, item: str | None = None) -> None:
        """Cancel one queued or running item, or the whole queue."""
        if not self.running:
            return
        if item is None:
            self.task.cancel()
            return
        st = self.steps.get(item)
        if st and st["state"] == "pending":
            st.update(state="cancelled", message="Cancelled")
        elif st and st["state"] == "running" and self.current:
            self.skip = True
            self.current.cancel()

    def _next(self) -> str | None:
        return next((s for s, st in self.steps.items() if st["state"] == "pending"), None)

    async def _step(self, cfg: dict, s: str, st: dict) -> None:
        if ":" in s:
            url, dest, size = item_target(cfg, s)
            await self._download(url, dest, size, st)
        else:
            await getattr(self, f"_install_{s}")(cfg, s, st)

    async def _run(self, on_done) -> None:
        cfg = load_config()
        try:
            while (s := self._next()) is not None:
                st = self.steps[s]
                st["state"] = "running"
                self.skip = False
                self.current = asyncio.create_task(self._step(cfg, s, st))
                try:
                    await self.current
                    st.update(state="done", message="Done")
                except asyncio.CancelledError:
                    if not self.skip:
                        raise  # whole queue cancelled
                    st.update(state="cancelled", message="Cancelled")
                except Exception as e:  # shown in the UI; the queue goes on with the next item
                    self.error = str(e) or repr(e)
                    st.update(state="error", message=self.error)
        except asyncio.CancelledError:
            for st in self.steps.values():
                if st["state"] in ("running", "pending"):
                    st.update(state="cancelled", message="Cancelled")
            raise
        finally:
            self.current = None
            await on_done()

    # -- steps

    async def _install_comfyui(self, cfg: dict, s: str, st: dict) -> None:
        target = Path(cfg["comfy_dir"])
        if not (target / "main.py").is_file():
            await self._download_zip(COMFY_ZIP, target, st, "Downloading ComfyUI")
        uv = find_uv()
        if not venv_python(cfg).exists():
            st["message"] = "Creating Python environment"
            await self._exec([uv, "venv", "--python", "3.12", str(target / ".venv")], st)
        st.update(message="Installing PyTorch and ComfyUI packages (takes a few minutes)", done=0, total=None)
        await self._exec([uv, "pip", "install", "--python", str(venv_python(cfg)), "-r", str(target / "requirements.txt")], st)
        for d in (cfg["input_dir"], cfg["output_dir"]):
            Path(d).mkdir(parents=True, exist_ok=True)

    async def _install_gguf_node(self, cfg: dict, s: str, st: dict) -> None:
        if not venv_python(cfg).exists():
            raise RuntimeError("ComfyUI is not installed yet")
        target = Path(cfg["comfy_dir"]) / "custom_nodes/ComfyUI-GGUF"
        if not (target / "loader.py").is_file():
            await self._download_zip(GGUF_ZIP, target, st, "Downloading ComfyUI-GGUF")
        st.update(message="Patching for Qwen-Image 2.1", done=0, total=None)
        patch_gguf_loader(target / "loader.py")
        st["message"] = "Installing gguf package"
        await self._exec([find_uv(), "pip", "install", "--python", str(venv_python(cfg)), "-r", str(target / "requirements.txt")], st)

    # -- helpers

    async def _download(self, url: str, dest: Path, size: int | None, st: dict) -> None:
        """Stream to dest.part (resumes after an interruption), then rename."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        st.update(message=f"Downloading {dest.name}", done=have, total=size)
        headers = {"Range": f"bytes={have}-"} if have else {}
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60, read=120)) as client:
            async with client.stream("GET", url, headers=headers) as r:
                expected = expected_sha256([*r.history, r])
                if r.status_code == 416:  # already complete
                    await self._verify(part, dest, expected, st)
                    return
                r.raise_for_status()
                if r.status_code == 200:
                    have = 0  # server ignored the range
                total = have + int(r.headers.get("content-length") or 0)
                st.update(done=have, total=total or size)
                t0, b0 = time.monotonic(), have
                with open(part, "ab" if have else "wb") as f:
                    async for chunk in r.aiter_bytes(1 << 20):
                        f.write(chunk)
                        have += len(chunk)
                        st["done"] = have
                        dt = time.monotonic() - t0
                        if dt > 1:
                            st["rate"] = round((have - b0) / dt)
        await self._verify(part, dest, expected, st)

    async def _verify(self, part: Path, dest: Path, expected: str | None, st: dict) -> None:
        if expected:
            st["message"] = f"Verifying {dest.name}"
        await verify_download(part, expected)
        part.replace(dest)

    async def _download_zip(self, url: str, target: Path, st: dict, label: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            zpath = Path(tmp) / "src.zip"
            await self._download(url, zpath, None, st)
            st.update(message=f"{label}: unpacking", done=0, total=None)
            await asyncio.to_thread(_extract_stripped, zpath, target)

    async def _exec(self, cmd: list[str], st: dict) -> None:
        st.update(done=0, total=None, rate=0)
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        tail: list[str] = []
        try:
            assert proc.stdout
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if line:
                    tail = (tail + [line])[-20:]
                    st["detail"] = line[:200]
            if await proc.wait() != 0:
                raise RuntimeError(f"{Path(cmd[0]).name} {cmd[1]} failed: " + " | ".join(tail[-3:]))
        finally:
            if proc.returncode is None:
                proc.kill()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def expected_sha256(responses) -> str | None:
    """SHA256 of an LFS file as announced by Hugging Face (x-linked-etag, or etag on the final response)."""
    for r in responses:
        h = getattr(r, "headers", r)
        for name in ("x-linked-etag", "etag"):
            v = (h.get(name) or "").strip()
            v = v.removeprefix("W/").strip('"').lower()
            if re.fullmatch(r"[0-9a-f]{64}", v):
                return v
    return None


async def verify_download(part: Path, expected: str | None) -> None:
    """Check the finished .part file against the expected sha256 (skipped when unknown); delete it on mismatch."""
    if not expected:
        return
    if await asyncio.to_thread(file_sha256, part) != expected:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{part.name.removesuffix('.part')}: checksum mismatch, download again")


def _extract_stripped(zpath: Path, target: Path) -> None:
    """Unpack a GitHub archive without its top-level 'repo-branch/' folder."""
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            rel = Path(*Path(info.filename).parts[1:])
            if not rel.parts or ".." in rel.parts:
                continue
            out = target / rel
            if info.is_dir():
                out.mkdir(parents=True, exist_ok=True)
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)


def patch_gguf_loader(loader: Path) -> None:
    """ComfyUI-GGUF does not know the qwen_image21 architecture yet; add it to IMG_ARCH_LIST."""
    text = loader.read_text()
    if "qwen_image21" in text:
        return
    new, n = re.subn(r"(IMG_ARCH_LIST\s*=\s*\{[^}]*)\}", r'\1, "qwen_image21"}', text, count=1)
    if not n:
        raise RuntimeError("could not patch ComfyUI-GGUF (IMG_ARCH_LIST not found)")
    loader.write_text(new)


def find_uv() -> str:
    for c in (shutil.which("uv"), Path.home() / ".local/bin/uv", "/opt/homebrew/bin/uv", "/usr/local/bin/uv"):
        if c and Path(c).exists():
            return str(c)
    raise RuntimeError("uv not found (the Mac app installs it on first start)")


# ---------------------------------------------------------------- run ComfyUI

class ComfyProcess:
    """ComfyUI started by this server (headless). A ComfyUI that is already running, e.g.
    Comfy Desktop, is used as is and never stopped."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None

    @property
    def managed(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, cfg: dict) -> None:
        if self.managed:
            return
        APP_SUPPORT.mkdir(parents=True, exist_ok=True)
        paths = APP_SUPPORT / "model-paths.yaml"
        lines = ["inpaint_studio:", f"  base_path: '{cfg['models_dir']}'", "  is_default: true"]
        lines += [f"  {f}: {f}/" for f in MODEL_FOLDERS]
        paths.write_text("\n".join(lines) + "\n")
        for d in (cfg["input_dir"], cfg["output_dir"]):
            Path(d).mkdir(parents=True, exist_ok=True)
        log = open(Path.home() / "Library/Logs/InpaintStudio-ComfyUI.log", "ab")
        self.proc = subprocess.Popen(
            [str(venv_python(cfg)), "-s", "main.py", "--port", str(COMFY_PORT), "--extra-model-paths-config", str(paths),
             "--input-directory", cfg["input_dir"], "--output-directory", cfg["output_dir"]],
            cwd=cfg["comfy_dir"], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        log.close()

    def stop(self) -> None:
        if not self.managed:
            return
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
