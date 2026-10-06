"""Improve prompt: a local language model rewrites the prompt before the picture is made.

The model runs in LM Studio (or any OpenAI-compatible server, INPAINT_STUDIO_LLM_URL), not in ComfyUI: on a Mac
ComfyUI keeps text models on the CPU (25-39 s per token for a 9B model) and its int8 matmul does not exist on MPS.
LM Studio runs them on the GPU (Qwen3.5 9B: ~30 tokens/s, a rewrite in 5-15 s). Any chat model works; a vision
model (LM Studio type "vlm") also sees the images of an edit. The system prompts are the ones of ComfyUI's official
Qwen-Image 2.1 templates (image_qwen_image_2_1_t2i / _image_edit), read from ComfyUI (GET /templates/<name>.json)
so they are never copied here. Pure functions, unit-tested; server.enhance_prompt does the requests.
"""

from __future__ import annotations

import os
import re
from typing import Any

URL = os.environ.get("INPAINT_STUDIO_LLM_URL", "http://127.0.0.1:1234").rstrip("/")
FAMILIES = ("qwen21", "qwen21_turbo")
TEMPLATE = {"t2i": "image_qwen_image_2_1_t2i", "i2i": "image_qwen_image_2_1_image_edit"}
MAX_TOKENS = 2048
TIMEOUT = 5 * 60    # seconds, loading the model in LM Studio included
UNLOAD_AFTER = 300  # LM Studio unloads a model it loaded for us after this many idle seconds (RAM for ComfyUI)
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


def request(model: str, system: str, prompt: str, images: list[str] | None = None, seed: int = 0) -> dict[str, Any]:
    """Chat completion body. images: data URLs (edited image first, then the references); only for vision models.
    The language note sits in the user turn: at the end of the long system prompt small models ignore it."""
    lang = "" if CJK.search(prompt) else "\n\n(Write the rewritten prompt in English.)"
    content: Any = (prompt or "(no instruction)") + lang
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
