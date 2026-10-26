"""Own files imported in the Download Center: a model, LoRA, upscaler or any other model file that is already on
the Mac. The file is not copied: a symlink in the matching models/ folder points to it (ComfyUI follows symlinks,
Finder aliases do not work). A language model (.gguf, kind "llm") is linked into LM Studio's models folder instead,
for Improve prompt and Prompt ideas. Imports are kept in imports.json and registered into presets.COMPONENTS / PRESETS
(`apply`), so pickers, the installed check and the graphs treat them like downloaded items."""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import installer
import presets

FILE = installer.APP_SUPPORT / "imports.json"
WATCH_FILE = installer.APP_SUPPORT / "watch_folders.json"   # {"folders": {kind: [path]}, "ignored": [source]}
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
    "llm": ("", "Language model (LM Studio)"),
}


def lmstudio_models() -> Path:
    """LM Studio's models folder (its settings name it; INPAINT_STUDIO_LMSTUDIO_MODELS overrides)."""
    if os.environ.get("INPAINT_STUDIO_LMSTUDIO_MODELS"):
        return Path(os.environ["INPAINT_STUDIO_LMSTUDIO_MODELS"]).expanduser()
    try:
        folder = json.loads((Path.home() / ".lmstudio" / "settings.json").read_text()).get("downloadsFolder")
    except (OSError, json.JSONDecodeError, AttributeError):
        folder = None
    return Path(folder).expanduser() if folder else Path.home() / ".lmstudio" / "models"


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
    if any(t in n for t in ("text_encoder", "text-encoder", "qwen_2.5_vl", "qwen3vl", "qwen_3_4b", "qwen_3_8b", "mistral", "t5xxl", "clip_l")):
        return "text_encoder"
    if "controlnet" in n or "control" in n:
        return "model_patch"
    return "model"


def guess_base(path: str) -> str:
    """The model line of a diffusion model file, from its name ('' = unknown, so a watched folder skips it)."""
    n = re.sub(r"[^a-z0-9.]+", "-", Path(path).name.lower())
    if "klein" in n:
        return "flux2_klein_9b" if "9b" in n else "flux2_klein_4b" if "4b" in n else ""
    if "flux" in n and "dev" in n:
        return "flux2_dev"
    if "z-image" in n or "zimage" in n:
        return "zimage_turbo"
    if "qwen" in n:
        if "edit" in n and "2511" in n:
            return "qwen_edit_2511"
        if "2512" in n:
            return "qwen_2512"
        if "2.1" in n or "2-1" in n:
            return "qwen21_viggle" if "viggle" in n else "qwen21_uc" if "uc" in n.split("-") else "qwen21"
    return ""


def guess_scale(path: str) -> int:
    m = re.search(r"(\d)x|x(\d)", Path(path).name.lower())
    return int(m[1] or m[2]) if m else 4


def _link(cfg: dict, item: dict[str, Any]) -> Path:
    if item["kind"] == "llm":   # LM Studio wants <publisher>/<model>/<file>
        return lmstudio_models() / "inpaint-studio" / Path(item["file"]).stem / item["file"]
    return Path(cfg["models_dir"]) / KINDS[item["kind"]][0] / item["file"]


def add(cfg: dict, path: str, kind: str, title: str = "", base: str = "", families: list[str] | None = None,
        scale: int | None = None, text_encoder: str = "", vae: str = "", watched: str = "") -> dict[str, Any]:
    src = Path(path).expanduser()
    if kind not in KINDS:
        raise ImportError_(f"unknown kind {kind}")
    if not src.is_file():
        raise ImportError_(f"{src} is not a file")
    if src.suffix.lower() not in EXTENSIONS:
        raise ImportError_(f"{src.name}: only {', '.join(EXTENSIONS)} files can be imported")
    if kind == "llm" and src.suffix.lower() != ".gguf":
        raise ImportError_("LM Studio runs language models as .gguf files")
    if kind == "model" and base not in presets.PRESETS:
        raise ImportError_("pick the model line the file belongs to (it decides the graph and settings)")
    for key, folder in ((text_encoder, "text_encoders"), (vae, "vae")):
        if kind == "model" and key and presets.COMPONENTS.get(key, {}).get("folder") != folder:
            raise ImportError_(f"unknown {folder.rstrip('s').replace('_', ' ')} {key}")
    item = {"id": uuid.uuid4().hex[:8], "kind": kind, "source": str(src.resolve()), "file": src.name,
            "title": (title or src.stem).strip(), "size": src.stat().st_size}
    if watched:
        item["watched"] = watched   # found in a watched folder: it follows the folder (gone with the file or the folder)
    if kind == "model":
        item["base"] = base
        base_pr = presets.PRESETS[base]
        item["text_encoder"] = text_encoder or base_pr["text_encoder"]
        item["vae"] = vae or base_pr["vae"]
    if kind == "lora":
        item["families"] = [f for f in (families or []) if isinstance(f, str)]
    if kind == "upscaler":
        item["scale"] = int(scale or guess_scale(src.name))
    link = _link(cfg, item)
    if any(i["kind"] == kind and i["file"] == src.name for i in load()):
        raise ImportError_(f"{src.name} is already imported")
    if kind == "llm" and src.resolve().is_relative_to(lmstudio_models().resolve()):
        item["in_lmstudio"] = True   # LM Studio has it already: only listed, no link (Remove leaves it alone)
    elif link.exists() or link.is_symlink():
        # a file that already sits in its models/ folder is registered without a link (Remove leaves it alone)
        if link.resolve() != src.resolve():
            raise ImportError_(f"another file named {src.name} is already in {link.parent}")
    else:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(src.resolve())
    _save([*load(), item])
    apply()
    return item


def update(import_id: str, title: str = "", base: str = "", families: list[str] | None = None, scale: int | None = None,
           text_encoder: str = "", vae: str = "") -> dict[str, Any]:
    """Changes what an imported file is (model line with its text encoder / VAE, LoRA families, upscaler scale, name)."""
    items = load()
    item = next((i for i in items if i["id"] == import_id), None)
    if not item:
        raise KeyError(import_id)
    if title.strip():
        item["title"] = title.strip()
    if item["kind"] == "model":
        if base not in presets.PRESETS:
            raise ImportError_("pick the model line the file belongs to")
        for key, folder in ((text_encoder, "text_encoders"), (vae, "vae")):
            if key and presets.COMPONENTS.get(key, {}).get("folder") != folder:
                raise ImportError_(f"unknown {folder.rstrip('s').replace('_', ' ')} {key}")
        item["base"] = base
        item["text_encoder"] = text_encoder or presets.PRESETS[base]["text_encoder"]
        item["vae"] = vae or presets.PRESETS[base]["vae"]
    elif item["kind"] == "lora" and families is not None:
        item["families"] = [f for f in families if isinstance(f, str)]
    elif item["kind"] == "upscaler" and scale:
        item["scale"] = int(scale)
    item.pop("check", None)
    _save(items)
    apply()
    return item


def remove(cfg: dict, import_id: str, forget: bool = False) -> dict[str, Any]:
    """`forget`: the user removed a watched file, so its folder does not bring it back."""
    items = load()
    item = next((i for i in items if i["id"] == import_id), None)
    if not item:
        raise KeyError(import_id)
    link = _link(cfg, item)
    if link.is_symlink() and not item.get("in_lmstudio"):   # only our link goes; the file it points to stays
        link.unlink()
    _save([i for i in items if i["id"] != import_id])
    apply()
    if forget and item.get("watched"):
        ignore(item["source"])
    return item


def apply() -> None:
    """Registers the imports as components (LoRAs, upscalers, other files) and presets (diffusion models)."""
    for key in [k for k, c in presets.COMPONENTS.items() if c.get("imported")]:
        del presets.COMPONENTS[key]
    for key in [k for k, p in presets.PRESETS.items() if p.get("imported")]:
        del presets.PRESETS[key]
    items = load()
    for i in items:   # components first: an imported model may use an imported text encoder or VAE
        if i["kind"] in ("model", "llm"):   # a language model lives in LM Studio, not in ComfyUI
            continue
        c = {"title": i["title"], "repo": "", "path": i["file"], "folder": KINDS[i["kind"]][0], "size": i["size"],
             "imported": True, "import_id": i["id"], "description": f"Imported: {i['source']}"}
        if i["kind"] == "lora":
            c.update(kind="lora", families=i.get("families") or [], strength=1.0)
        elif i["kind"] == "upscaler":
            c.update(kind="upscaler", scale=i.get("scale", 4))
        else:
            c["kind"] = i["kind"]
        presets.COMPONENTS[f"imp_{i['id']}"] = c
    for i in items:
        base = presets.PRESETS.get(i.get("base", ""))
        if i["kind"] != "model" or not base:
            continue
        quant = Path(i["file"]).suffix.lstrip(".").upper() or "FILE"   # shown as "<title> · GGUF"
        # a removed text encoder / VAE import falls back to the one of the model line
        te, vae = i.get("text_encoder", ""), i.get("vae", "")
        presets.PRESETS[f"imp_{i['id']}"] = {
            **base, "title": i["title"], "imported": True, "recommended": False,
            "note": f"Imported: {i['source']}", "good_for": f"Your own file, run like {base['title']}.",
            "text_encoder": te if te in presets.COMPONENTS else base["text_encoder"],
            "vae": vae if vae in presets.COMPONENTS else base["vae"],
            "repo": "", "default_quant": quant, "quants": {quant: {"file": i["file"], "size": i["size"]}}}


# ---------------------------------------------------------------- watched folders
# More folders per category (e.g. LM Studio's models folder): every matching file in them is imported on its own,
# on server start, when the folders are saved and when the Download Center is opened.

def watch_load() -> dict[str, Any]:
    try:
        raw = json.loads(WATCH_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    folders = raw.get("folders") if isinstance(raw, dict) else None
    folders = {k: [p for p in v if isinstance(p, str)] for k, v in (folders or {}).items()
               if k in KINDS and isinstance(v, list)}
    ignored = [x for x in raw.get("ignored", []) if isinstance(x, str)] if isinstance(raw, dict) else []
    return {"folders": {k: v for k, v in folders.items() if v}, "ignored": ignored}


def watch_save(folders: dict[str, list[str]]) -> dict[str, Any]:
    """Replaces the watched folders; a folder that is gone from the list takes its imports with it."""
    clean: dict[str, list[str]] = {}
    for kind, paths in folders.items():
        if kind not in KINDS:
            raise ImportError_(f"unknown category {kind}")
        for p in paths:
            p = str(p).strip()
            if not p:
                continue
            path = Path(p).expanduser()
            if not path.is_dir():
                raise ImportError_(f"{path} is not a folder")
            if str(path) not in clean.setdefault(kind, []):
                clean[kind].append(str(path))
    cur = watch_load()
    WATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    WATCH_FILE.write_text(json.dumps({"folders": clean, "ignored": cur["ignored"]}, indent=1))
    return cur


def _walk(folder: Path):
    for root, dirs, files in os.walk(folder, followlinks=True):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in sorted(files):
            if not f.startswith(".") and Path(f).suffix.lower() in EXTENSIONS:
                yield Path(root) / f


def sync(cfg: dict) -> dict[str, Any]:
    """Imports every new file in the watched folders and drops imports whose file or folder is gone.
    Returns {added, removed}. A model whose line is not in its name gets the default one and `check` = True."""
    watch = watch_load()
    added, removed = [], []
    for item in load():   # watched imports whose folder was removed or whose file is gone
        w = item.get("watched")
        if w and (w not in watch["folders"].get(item["kind"], []) or not Path(item["source"]).exists()):
            remove(cfg, item["id"])
            removed.append(item["file"])
    have = {(i["kind"], i["source"]) for i in load()}
    models_dir = Path(cfg["models_dir"]).resolve()
    for kind, folders in watch["folders"].items():
        own = models_dir / KINDS[kind][0] if KINDS[kind][0] else None
        for folder in map(Path, folders):
            if not folder.is_dir():
                continue
            for f in _walk(folder):
                src = str(f.resolve())
                if (kind, src) in have or src in watch["ignored"] or f.name.lower().startswith("mmproj"):
                    continue
                if kind == "llm" and f.suffix.lower() != ".gguf":
                    continue
                if own and f.resolve().is_relative_to(own):
                    continue   # already in the models folder itself
                base = guess_base(f.name) if kind in ("model", "lora") else ""
                guessed = bool(base)
                if kind == "model" and not base:
                    base = presets.DEFAULT_PRESET   # unknown line: imported flagged "check", the user sets it in Edit
                # a LoRA fits the model line its name points to, else it is offered for every line
                families = [presets.lora_family(presets.PRESETS[base])] if base else sum(presets.LORA_GROUPS.values(), [])
                try:
                    item = add(cfg, src, kind, base=base if kind == "model" else "", families=families, watched=str(folder))
                except (ImportError_, OSError):
                    continue   # e.g. a file with the same name is already there
                if kind in ("model", "lora") and not guessed:
                    item["check"] = True   # the model line is a guess the user should confirm (Edit)
                    _save([{**i, "check": True} if i["id"] == item["id"] else i for i in load()])
                added.append(item["file"])
                have.add((kind, src))
    return {"added": added, "removed": removed}


def ignore(source: str) -> None:
    """A watched file the user removed by hand is not imported again."""
    watch = watch_load()
    if source not in watch["ignored"]:
        WATCH_FILE.write_text(json.dumps({"folders": watch["folders"], "ignored": [*watch["ignored"], source]}, indent=1))
