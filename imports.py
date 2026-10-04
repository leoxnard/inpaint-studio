"""Own files imported in the Download Center: a model, LoRA, upscaler or any other model file that is already on
the Mac. The file is not copied: a symlink in the matching models/ folder points to it (ComfyUI follows symlinks,
Finder aliases do not work). Imports are kept in imports.json and registered into presets.COMPONENTS / PRESETS
(`apply`), so pickers, the installed check and the graphs treat them like downloaded items."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import installer
import presets

FILE = installer.APP_SUPPORT / "imports.json"
EXTENSIONS = (".safetensors", ".gguf", ".sft", ".pth", ".pt", ".ckpt", ".bin")
# kind -> models/ folder and what the Download Center calls it
KINDS: dict[str, tuple[str, str]] = {
    "model": ("diffusion_models", "Diffusion model"),
    "lora": ("loras", "LoRA"),
    "upscaler": ("upscale_models", "Upscaler"),
    "text_encoder": ("text_encoders", "Text encoder"),
    "vae": ("vae", "VAE"),
    "model_patch": ("model_patches", "Control patch"),
    "controlnet": ("controlnet", "ControlNet"),
}


class ImportError_(ValueError):
    """A file that cannot be imported (the message is shown to the user)."""


def load() -> list[dict[str, Any]]:
    try:
        data = json.loads(FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [i for i in data if isinstance(i, dict) and i.get("kind") in KINDS]


def _save(items: list[dict[str, Any]]) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(items, indent=1))


def guess_kind(path: str) -> str:
    """A first guess from the file name; the user can change it before importing."""
    n = Path(path).name.lower()
    if "lora" in n:
        return "lora"
    if re.search(r"(^|[^a-z])(\d)x([^a-z0-9]|$)|esrgan|ultrasharp|_x\d", n) or n.endswith(".pth"):
        return "upscaler"
    if "vae" in n:
        return "vae"
    if any(t in n for t in ("text_encoder", "qwen_2.5_vl", "qwen3vl", "qwen_3_4b", "t5xxl", "clip_l")):
        return "text_encoder"
    if "controlnet" in n or "control" in n:
        return "model_patch"
    return "model"


def guess_scale(path: str) -> int:
    m = re.search(r"(\d)x|x(\d)", Path(path).name.lower())
    return int(m[1] or m[2]) if m else 4


def _link(cfg: dict, item: dict[str, Any]) -> Path:
    return Path(cfg["models_dir"]) / KINDS[item["kind"]][0] / item["file"]


def add(cfg: dict, path: str, kind: str, title: str = "", base: str = "", families: list[str] | None = None,
        scale: int | None = None) -> dict[str, Any]:
    src = Path(path).expanduser()
    if kind not in KINDS:
        raise ImportError_(f"unknown kind {kind}")
    if not src.is_file():
        raise ImportError_(f"{src} is not a file")
    if src.suffix.lower() not in EXTENSIONS:
        raise ImportError_(f"{src.name}: only {', '.join(EXTENSIONS)} files can be imported")
    if kind == "model" and base not in presets.PRESETS:
        raise ImportError_("pick the model line the file belongs to (it decides text encoder and VAE)")
    item = {"id": uuid.uuid4().hex[:8], "kind": kind, "source": str(src.resolve()), "file": src.name,
            "title": (title or src.stem).strip(), "size": src.stat().st_size}
    if kind == "model":
        item["base"] = base
    if kind == "lora":
        item["families"] = [f for f in (families or []) if isinstance(f, str)]
    if kind == "upscaler":
        item["scale"] = int(scale or guess_scale(src.name))
    link = _link(cfg, item)
    if link.exists() or link.is_symlink():
        if link.resolve() == src.resolve():
            raise ImportError_(f"{src.name} is already in models/{KINDS[kind][0]}")
        raise ImportError_(f"another file named {src.name} is already in models/{KINDS[kind][0]}")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(src.resolve())
    _save([*load(), item])
    apply()
    return item


def remove(cfg: dict, import_id: str) -> dict[str, Any]:
    items = load()
    item = next((i for i in items if i["id"] == import_id), None)
    if not item:
        raise KeyError(import_id)
    link = _link(cfg, item)
    if link.is_symlink():   # only our link goes; the file it points to stays
        link.unlink()
    _save([i for i in items if i["id"] != import_id])
    apply()
    return item


def apply() -> None:
    """Registers the imports as components (LoRAs, upscalers, other files) and presets (diffusion models)."""
    for key in [k for k, c in presets.COMPONENTS.items() if c.get("imported")]:
        del presets.COMPONENTS[key]
    for key in [k for k, p in presets.PRESETS.items() if p.get("imported")]:
        del presets.PRESETS[key]
    for i in load():
        key = f"imp_{i['id']}"
        if i["kind"] == "model":
            base = presets.PRESETS.get(i.get("base", ""))
            if not base:
                continue
            quant = Path(i["file"]).suffix.lstrip(".").upper() or "FILE"   # shown as "<title> · GGUF"
            presets.PRESETS[key] = {**base, "title": i["title"], "imported": True, "recommended": False,
                                    "note": f"Imported: {i['source']}", "good_for": f"Your own file, run like {base['title']}.",
                                    "repo": "", "default_quant": quant, "quants": {quant: {"file": i["file"], "size": i["size"]}}}
            continue
        c = {"title": i["title"], "repo": "", "path": i["file"], "folder": KINDS[i["kind"]][0], "size": i["size"],
             "imported": True, "import_id": i["id"], "description": f"Imported: {i['source']}"}
        if i["kind"] == "lora":
            c.update(kind="lora", families=i.get("families") or [], strength=1.0)
        elif i["kind"] == "upscaler":
            c.update(kind="upscaler", scale=i.get("scale", 4))
        else:
            c["kind"] = i["kind"]
        presets.COMPONENTS[key] = c
