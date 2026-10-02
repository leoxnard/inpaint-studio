"""First-run setup: find or install ComfyUI, the GGUF custom node and the models, and run ComfyUI.

Everything a fresh Mac needs besides this app itself (uv + the app's Python deps are installed
by the Mac launcher before the server can start). Downloads come from GitHub and Hugging Face,
no accounts needed. An existing Comfy Desktop install is detected and reused.
"""

from __future__ import annotations

import asyncio
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

APP_SUPPORT = Path(os.environ.get("INPAINT_STUDIO_HOME") or Path.home() / "Library/Application Support/Inpaint Studio")
CONFIG_FILE = APP_SUPPORT / "config.json"
COMFY_DESKTOP = Path.home() / "Library/Application Support/Comfy Desktop"
COMFY_PORT = 8188
COMFY_ZIP = "https://github.com/comfyanonymous/ComfyUI/archive/refs/tags/v0.38.0.zip"
GGUF_ZIP = "https://github.com/city96/ComfyUI-GGUF/archive/refs/heads/main.zip"
HF = "https://huggingface.co/{repo}/resolve/main/{path}"

UNET_REPO = "abenzerps/Qwen-Image-2.1-Uncensored-GGUF"
UNET_FILES = {  # quantisation -> (file, bytes)
    "Q4_K_M": ("qwen-image-2.1-UC-Q4_K_M.gguf", 4_604_558_112),
    "Q8_0": ("qwen-image-2.1-UC-Q8_0.gguf", 7_591_557_920),
    "BF16": ("qwen-image-2.1-UC-BF16.gguf", 14_230_272_800),
}
MODELS = {  # step id -> (repo, path in repo, folder in models dir, bytes)
    "text_encoder": ("Comfy-Org/Qwen-Image-2.1", "text_encoders/qwen3vl_8b_int8_convrot.safetensors", "text_encoders", 9_350_798_360),
    "vae": ("Comfy-Org/Qwen-Image-2.1", "vae/qwen_image_2.1_vae_bf16.safetensors", "vae", 675_509_688),
    "sam3": ("Comfy-Org/sam3.1", "checkpoints/sam3.1_multiplex_fp16.safetensors", "checkpoints", 1_745_546_848),
}
STEP_INFO = [  # id, title, description, optional
    ("comfyui", "ComfyUI", "Image generation engine with its own Python environment (PyTorch etc., ~1.5 GB)", False),
    ("gguf_node", "GGUF loader", "ComfyUI-GGUF custom node, patched for Qwen-Image 2.1", False),
    ("unet", "Image model", "Qwen-Image 2.1 UC (GGUF) from Hugging Face", False),
    ("text_encoder", "Text encoder", "Qwen3-VL 8B int8, reads the prompt and the input image", False),
    ("vae", "VAE", "Turns latents into pixels and back", False),
    ("sam3", "Masking (SAM3)", "Optional: automatic masks and the mask tools. Without it, the app only edits whole images", True),
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
         "input_dir": str(APP_SUPPORT / "input"), "output_dir": str(APP_SUPPORT / "output"), "quant": "Q8_0"}
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
            cfg[k] = str(values[k]) if k == "quant" else str(Path(str(values[k])).expanduser())
    if cfg["quant"] not in UNET_FILES:
        raise ValueError(f"unknown quantisation {cfg['quant']}")
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


def _has_unet(cfg: dict) -> bool:
    for f in ("diffusion_models", "unet"):
        d = Path(cfg["models_dir"]) / f
        if d.is_dir() and any(p.exists() for p in d.glob("qwen-image-2.1*.gguf")):
            return True
    return False


def installed(cfg: dict) -> dict[str, bool]:
    loader = Path(cfg["comfy_dir"]) / "custom_nodes/ComfyUI-GGUF/loader.py"
    return {
        "comfyui": (Path(cfg["comfy_dir"]) / "main.py").is_file() and venv_python(cfg).exists(),
        "gguf_node": loader.is_file() and "qwen_image21" in loader.read_text(errors="ignore"),
        "unet": _has_unet(cfg),
        **{k: _model_file(cfg, [folder, "clip"] if k == "text_encoder" else [folder], Path(path).name) is not None
           for k, (_, path, folder, _) in MODELS.items()},
    }


def step_size(step: str, cfg: dict) -> int | None:
    if step == "unet":
        return UNET_FILES[cfg["quant"]][1]
    return MODELS[step][3] if step in MODELS else None


# ---------------------------------------------------------------- install

class Installer:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.steps: dict[str, dict] = {}
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def state(self) -> dict:
        return {"running": self.running, "steps": self.steps, "error": self.error}

    def start(self, steps: list[str], on_done) -> None:
        order = [s for s, *_ in STEP_INFO if s in steps]
        self.steps = {s: {"state": "pending", "message": "", "done": 0, "total": None, "rate": 0} for s in order}
        self.error = None
        self.task = asyncio.create_task(self._run(order, on_done))

    def cancel(self) -> None:
        if self.running:
            self.task.cancel()

    async def _run(self, order: list[str], on_done) -> None:
        cfg = load_config()
        try:
            for s in order:
                st = self.steps[s]
                st["state"] = "running"
                await getattr(self, f"_install_{'model' if s in MODELS else s}")(cfg, s, st)
                st.update(state="done", message="Done")
        except asyncio.CancelledError:
            for st in self.steps.values():
                if st["state"] in ("running", "pending"):
                    st.update(state="cancelled", message="Cancelled")
            raise
        except Exception as e:  # shown in the UI
            self.error = str(e) or repr(e)
            for st in self.steps.values():
                if st["state"] == "running":
                    st.update(state="error", message=self.error)
                elif st["state"] == "pending":
                    st.update(state="skipped", message="Skipped after an error")
        finally:
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

    async def _install_unet(self, cfg: dict, s: str, st: dict) -> None:
        name, size = UNET_FILES[cfg["quant"]]
        await self._download(HF.format(repo=UNET_REPO, path=name), Path(cfg["models_dir"]) / "diffusion_models" / name, size, st)

    async def _install_model(self, cfg: dict, s: str, st: dict) -> None:
        repo, path, folder, size = MODELS[s]
        await self._download(HF.format(repo=repo, path=path), Path(cfg["models_dir"]) / folder / Path(path).name, size, st)

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
                if r.status_code == 416:  # already complete
                    part.replace(dest)
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
