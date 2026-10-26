"""Improve prompt: a local language model rewrites the prompt before the picture is made.

The model runs in LM Studio (or any OpenAI-compatible server, INPAINT_STUDIO_LLM_URL), not in ComfyUI: on a Mac
ComfyUI keeps text models on the CPU (25-39 s per token for a 9B model) and its int8 matmul does not exist on MPS.
LM Studio runs them on the GPU (Qwen3.5 9B: ~30 tokens/s, a rewrite in 5-15 s). Any chat model works; a vision
model (LM Studio type "vlm") also sees the images of an edit. The system prompts are the ones of ComfyUI's official
Qwen-Image 2.1 templates (image_qwen_image_2_1_t2i / _image_edit), read from ComfyUI (GET /templates/<name>.json)
so they are never copied here. Pure functions, unit-tested; server.enhance_prompt does the requests.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

URL = os.environ.get("INPAINT_STUDIO_LLM_URL", "http://127.0.0.1:1234").rstrip("/")
FAMILIES = ("qwen21", "qwen21_turbo")
TEMPLATE = {"t2i": "image_qwen_image_2_1_t2i", "i2i": "image_qwen_image_2_1_image_edit"}
MAX_TOKENS = 2048
TIMEOUT = 5 * 60    # seconds, loading the model in LM Studio included
UNLOAD_AFTER = 300  # fallback TTL; the server unloads the model right after its answer (RAM for ComfyUI)
# LM Studio also lists the image models it shares with ComfyUI as "llm": these architectures are not chat models
IMAGE_ARCHS = {"qwen_image", "qwen_image21", "lumina2", "wan", "flux", "flux2", "sd3", "sdxl", "hidream", "ltxv"}
CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def kind(task: str) -> str:
    return "t2i" if task == "generate" else "i2i"


def system_prompt(template: dict) -> str:
    """The text wired into TextGenerate's system_prompt input (the template may nest it in a subgraph)."""
    for g in [template, *template.get("definitions", {}).get("subgraphs", [])]:
        nodes = {n["id"]: n for n in g.get("nodes", [])}
        links = {}
        for link in g.get("links", []):
            if isinstance(link, dict):
                links[link["id"]] = link["origin_id"]
            else:   # [id, origin_id, origin_slot, target_id, target_slot, type]
                links[link[0]] = link[1]
        for n in nodes.values():
            if n.get("type") != "TextGenerate":
                continue
            for inp in n.get("inputs", []):
                if inp.get("name") == "system_prompt" and inp.get("link") is not None:
                    src = nodes.get(links.get(inp["link"]))
                    vals = (src or {}).get("widgets_values") or []
                    if vals and isinstance(vals[0], str) and vals[0].strip():
                        return vals[0]
    raise ValueError("no system prompt found in the enhancer template")


def chat_models(listing: dict) -> list[dict[str, Any]]:
    """Chat models from LM Studio's GET /api/v0/models: [{id, vision, loaded}], loaded ones first."""
    out = [{"id": m["id"], "vision": m.get("type") == "vlm", "loaded": m.get("state") == "loaded"}
           for m in listing.get("data", [])
           if m.get("type") in ("llm", "vlm") and (m.get("arch") or "").lower() not in IMAGE_ARCHS]
    return sorted(out, key=lambda m: (not m["loaded"], not m["vision"], m["id"]))


def lms_listing(items: list) -> dict:
    """`lms ls --json` (works while LM Studio's server is off) in the shape of GET /api/v0/models, for chat_models."""
    return {"data": [{"id": m.get("modelKey"), "type": "vlm" if m.get("vision") else m.get("type"),
                      "arch": m.get("architecture"), "state": "not-loaded"}
                     for m in items if isinstance(m, dict) and m.get("modelKey")]}


def loaded_instances(listing: dict, model: str) -> list[str]:
    """Instance ids of a model that LM Studio has loaded, from its GET /api/v1/models (unloaded one by one)."""
    return [i["id"] for m in listing.get("models", []) if m.get("key") == model
            for i in m.get("loaded_instances") or [] if i.get("id")]


def image_note(n: int, edit: bool = True) -> str:
    """Names the images of a job for the rewriter. The official system prompt keeps <imageN> tags only for two or more
    input images, so a text-only model (no images attached) would take a job with references for a single image and
    write the references away. The tags are the ones TextEncodeQwenImage21 puts in front of the images."""
    if edit and n >= 2:
        return (f"\n\n(The edit has {n} images: <image1> is the image to edit, "
                f"{'<image2>' if n == 2 else f'<image2> to <image{n}>'} {'is a reference' if n == 2 else 'are references'}. "
                "Use these tags for the images in the rewritten prompt.)")
    if not edit and n >= 1:
        return (f"\n\n(The prompt can refer to {n} reference image{'s' if n > 1 else ''} as "
                f"{'<image1>' if n == 1 else f'<image1> to <image{n}>'}. Keep these tags in the rewritten prompt.)")
    return ""


def request(model: str, system: str, prompt: str, images: list[str] | None = None, seed: int = 0,
            n_images: int = 0, edit: bool = True) -> dict[str, Any]:
    """Chat completion body. images: data URLs (edited image first, then the references); only for vision models.
    n_images: how many images the job has in all (image_note), also without vision.
    The language note sits in the user turn: at the end of the long system prompt small models ignore it."""
    lang = "" if CJK.search(prompt) else "\n\n(Write the rewritten prompt in English.)"
    content: Any = (prompt or "(no instruction)") + image_note(n_images, edit) + lang
    if images:
        content = [*({"type": "image_url", "image_url": {"url": u}} for u in images), {"type": "text", "text": content}]
    return {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            # sampling of the official templates; no thinking: it only costs time here
            "temperature": 1.0, "top_p": 0.95, "max_tokens": MAX_TOKENS, "seed": seed % 2 ** 31,
            "reasoning_effort": "none", "ttl": UNLOAD_AFTER}


def answer(response: dict) -> str:
    """The rewritten prompt from a chat completion, cleaned."""
    try:
        return clean(response["choices"][0]["message"].get("content") or "")
    except (KeyError, IndexError, TypeError):
        return ""


def wrong_language(prompt: str, text: str) -> bool:
    """A Chinese (or Japanese / Korean) rewrite of a prompt that had none: asked again once."""
    return bool(CJK.search(text)) and not CJK.search(prompt)


def clean(text: str) -> str:
    """One paragraph: no thinking block, code fence, label or quotes around the whole text."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"^```\w*\s*|\s*```$", "", text).strip()
    text = re.sub(r"^(rewritten|enhanced)?\s*prompt\s*:\s*", "", text, flags=re.I)
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return re.sub(r"\s*\n+\s*", " ", text).strip()


# ---------------------------------------------------------------- Prompt ideas (Generate: a request -> prompts)

IDEA_FIELDS = ("subject", "setting", "style", "camera")
IDEAS_MAX = 24
IDEAS_SYSTEM = """You write prompts for a text-to-image model. The user describes what pictures they want; you return one \
prompt per picture. Write every field in English, in plain descriptive language, no lists, no hashtags, no quality tags.
- title: a short name for the picture (the item it is for, e.g. the playlist name), in the user's wording.
- subject: the main subject and what it does or how it looks, concrete and visual (one or two sentences).
- setting: where it is, the background, time of day, weather, surroundings (one sentence).
- style: the medium and look, e.g. photo, oil painting, 3D render, flat illustration; colours, mood, light (one sentence).
- camera: framing, angle, lens and depth of field, e.g. "close-up from a low angle, 35 mm, shallow depth of field". \
Choose it yourself only when the request names none.
Requirements in the request (camera angle, perspective, framing, size, what must or must not be in the picture) hold for \
every prompt: put each into the field it belongs to, in concrete words, and never replace it with your own idea. Make the \
subject agree with the camera: a record seen from directly above lies flat and is a perfect circle, so it does not stand \
upright. "90 degrees from the ceiling to the floor" or "top-down" means: camera directly above, pointing straight down, \
flat lay, no perspective.
Do not put any text, letters or logos into the picture unless the user asks for it."""
IDEAS_TEMPLATE = """Every picture follows a fixed template ({shapes}). Each shape is exactly where one main object goes, at \
exactly that size; the picture keeps these outlines. Plan every prompt around them:
- Make the main object fit the shape it fills (a circle = a round object such as a record, a plate or a clock) and say \
that it fills that area.
- Choose a camera that shows the shape undistorted: a round object straight from above or straight from the front. Write \
no lens, focal length or depth-of-field words.
- Keep the background in the same view as the object: seen from above, describe only the surface it lies on (a table, a \
blanket, a floor) and say there is no room, no wall and no horizon. A bigger object around it (e.g. a record player \
around a record) has to fill the whole picture, or it ends up smaller than the shape.
- Add nothing that would cover or tilt the main shape. Describe small visible details concretely (e.g. a crisp printed label)."""
IDEAS_TITLE = """The app writes each title into its picture afterwards. For every prompt also choose:
- title_position: {position_rule}
- title_size: small, medium or large, fitting the title's length and the picture.
- title_font: the font that fits the mood, one of: {fonts}.
Describe the picture so the area at title_position stays calm and empty; never describe the title or any lettering."""
TITLE_FIELDS = ("title_position", "title_size", "title_font")


def ideas_request(model: str, text: str, count: int = 0, same_style: bool = False, seed: int = 0,
                  fonts: list[str] | None = None, position: str = "auto", template: str = "") -> dict[str, Any]:
    """Chat completion body for Prompt ideas: the answer is JSON that follows the schema (LM Studio enforces it).
    fonts (Add title): every idea also gets the title's position, size and font (title.py writes it later).
    position other than auto is fixed: the model only plans the picture around it.
    template (Generate with a template): its shapes in words, e.g. "a circle 80 % wide in the centre"."""
    n = max(0, min(int(count or 0), IDEAS_MAX))
    rules = [f"Write exactly {n} prompts." if n else
             "Write as many prompts as the request asks for (one per item it names); if it names none, write 4."]
    if same_style:
        rules.append("All pictures belong to one series: use the same style and camera for every prompt.")
    else:
        rules.append("Give the prompts varied ideas.")
    item = {"type": "object", "additionalProperties": False, "required": ["title", *IDEA_FIELDS],
            "properties": {k: {"type": "string"} for k in ("title", *IDEA_FIELDS)}}
    system = IDEAS_SYSTEM
    if template.strip():
        system += "\n\n" + IDEAS_TEMPLATE.format(shapes=template.strip())
    if fonts:
        item["required"] += list(TITLE_FIELDS)
        places = ["top", "center", "bottom"] if position not in ("top", "center", "bottom") else [position]
        item["properties"].update(title_position={"type": "string", "enum": places},
                                  title_size={"type": "string", "enum": ["small", "medium", "large"]},
                                  title_font={"type": "string", "enum": list(fonts)})
        rule = ("where the title goes (top, center or bottom), where the picture will have a calm, plain area for it."
                if len(places) > 1 else f"always {position}; keep that area of the picture calm and plain for it.")
        system += "\n\n" + IDEAS_TITLE.format(fonts=", ".join(fonts), position_rule=rule)
    ideas = {"type": "array", "items": item, "minItems": n or 1, "maxItems": n or IDEAS_MAX}
    schema = {"type": "object", "additionalProperties": False, "required": ["ideas"], "properties": {"ideas": ideas}}
    return {"model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": f"{text.strip()}\n\n({' '.join(rules)})"}],
            "temperature": 0.8, "top_p": 0.95, "max_tokens": 8192, "seed": seed % 2 ** 31,
            "reasoning_effort": "none", "ttl": UNLOAD_AFTER,
            "response_format": {"type": "json_schema", "json_schema": {"name": "prompt_ideas", "strict": True,
                                                                       "schema": schema}}}


def compose(idea: dict) -> str:
    """The prompt sent to the image model: the four fields as one paragraph."""
    parts = [str(idea.get(k) or "").strip().rstrip(".") for k in IDEA_FIELDS]
    out = ". ".join(p for p in parts if p)
    return f"{out}." if out else ""


def ideas(response: dict, same_style: bool = False, with_title: bool = False, position: str = "auto") -> list[dict[str, str]]:
    """The ideas of a chat completion: [{title, subject, setting, style, camera, prompt}] (+ title_position / _size /
    _font with_title). same_style copies the first idea's style and camera to all (the model sometimes drifts)."""
    try:
        text = response["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        text = ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```\w*\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("the model did not answer with JSON")
    raw = data.get("ideas") if isinstance(data, dict) else data
    keys = ("title", *IDEA_FIELDS, *(TITLE_FIELDS if with_title else ()))
    out = [{k: str(i.get(k) or "").strip() for k in keys}
           for i in (raw or []) if isinstance(i, dict)][:IDEAS_MAX]
    if with_title and position in ("top", "center", "bottom"):   # fixed by the user, never the model's choice
        for i in out:
            i["title_position"] = position
    if same_style and out:
        for i in out[1:]:
            i.update(style=out[0]["style"], camera=out[0]["camera"])
    for i in out:
        i["prompt"] = compose(i)
    return [i for i in out if i["prompt"]]
