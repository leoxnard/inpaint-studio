"""Size math and ComfyUI API graph builders.

Mirrors the exact resize rules of the ComfyUI nodes we use so the UI can predict
working sizes before anything runs:
  * ImageScaleToTotalPixels (megapixels * 1024^2, rounded to `steps`)
  * TextEncodeQwenImage21 (reference image resized to `resolution`^2, rounded to 32)

Gray-noise limit: on Apple Silicon (MPS) the Qwen-Image 2.1 edit path breaks as soon
as the target or reference image reaches 4096 latent tokens (pixels / 16 per side).
Tested: 4032 tokens works, 4096 and above turns the whole image into gray noise.
"""

from __future__ import annotations

import math
from typing import Any

TOKEN_LIMIT = 4096  # first failing token count; keep strictly below
SCALE_STEPS = 32


def scale_to_megapixels(width: int, height: int, megapixels: float, steps: int = SCALE_STEPS) -> tuple[int, int]:
    total = megapixels * 1024 * 1024
    scale = math.sqrt(total / (width * height))
    return round(width * scale / steps) * steps, round(height * scale / steps) * steps


def reference_size(width: int, height: int, resolution: int) -> tuple[int, int]:
    ratio = width / height
    w = round(math.sqrt(resolution * resolution * ratio) / 32) * 32
    h = round(math.sqrt(resolution * resolution / ratio) / 32) * 32
    return max(32, w), max(32, h)


def matching_resolution(work_w: int, work_h: int) -> int:
    """Encoder resolution whose reference image has exactly the working size (or comes closest).
    A reference that differs from the working size makes the free edit come out slightly shifted
    and scaled (tested: 1008 -> ref 1376x736 for work 1344x736 gave 2.3 px shift / 0.5 % scale,
    the matching 992 gave 0.4 px / 0 %)."""
    centre = round(math.sqrt(work_w * work_h) / 16) * 16
    candidates = range(max(256, centre - 160), centre + 161, 16)

    def miss(r: int) -> int:
        rw, rh = reference_size(work_w, work_h, r)
        return abs(rw - work_w) + abs(rh - work_h)
    return min(candidates, key=lambda r: (miss(r), abs(r - centre)))


def tokens(width: int, height: int) -> int:
    return (width // 16) * (height // 16)


def size_report(width: int, height: int, megapixels: float, resolution: int) -> dict[str, Any]:
    ww, wh = scale_to_megapixels(width, height, megapixels)
    rw, rh = reference_size(ww, wh, resolution)
    t_target, t_ref = tokens(ww, wh), tokens(rw, rh)
    return {
        "work_w": ww, "work_h": wh, "ref_w": rw, "ref_h": rh,
        "target_tokens": t_target, "ref_tokens": t_ref,
        "safe": t_target < TOKEN_LIMIT and t_ref < TOKEN_LIMIT,
    }


def safe_settings(width: int, height: int, megapixels: float, resolution: int) -> dict[str, Any]:
    """Largest megapixels <= requested (and matching resolution) that stays under the limit."""
    mp, res = megapixels, resolution
    for _ in range(200):
        rep = size_report(width, height, mp, res)
        if rep["safe"]:
            return {"megapixels": round(mp, 3), "resolution": res, **rep}
        if rep["target_tokens"] >= TOKEN_LIMIT:
            mp -= 0.01
        if rep["ref_tokens"] >= TOKEN_LIMIT:
            res -= 16
    raise ValueError("could not find safe settings")


def _mask_chain(g: dict, image_ref: list, text: str, threshold: float, refine: int, expand: int) -> list:
    g["sam_ckpt"] = {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sam3.1_multiplex_fp16.safetensors"}}
    g["sam_text"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["sam_ckpt", 1], "text": text}}
    g["sam"] = {"class_type": "SAM3_Detect", "inputs": {
        "model": ["sam_ckpt", 0], "image": image_ref, "conditioning": ["sam_text", 0],
        "threshold": threshold, "refine_iterations": refine, "individual_masks": False}}
    g["grow"] = {"class_type": "GrowMask", "inputs": {"mask": ["sam", 0], "expand": expand, "tapered_corners": True}}
    return ["grow", 0]


def build_mask_graph(image: str, megapixels: float, text: str, threshold: float = 0.5,
                     refine: int = 2, expand: int = 24, invert: bool = False) -> dict:
    """Step 1: only SAM3 -> hard mask at working size, returned as a temp preview image."""
    g: dict[str, Any] = {
        "load": {"class_type": "LoadImage", "inputs": {"image": image}},
        "scale": {"class_type": "ImageScaleToTotalPixels", "inputs": {
            "image": ["load", 0], "upscale_method": "lanczos", "megapixels": megapixels, "resolution_steps": SCALE_STEPS}},
    }
    mask = _mask_chain(g, ["scale", 0], text, threshold, refine, expand)
    if invert:
        g["invert"] = {"class_type": "InvertMask", "inputs": {"mask": mask}}
        mask = ["invert", 0]
    g["mask_img"] = {"class_type": "MaskToImage", "inputs": {"mask": mask}}
    g["out_mask"] = {"class_type": "PreviewImage", "inputs": {"images": ["mask_img", 0]}}
    return g


# "Remove watermarks and text" (p["clean_overlays"]): appended to the prompt
CLEAN_NOTE = ("Remove all watermarks, logos, captions and overlaid text from the image and restore what "
              "is behind them.")
CLEAN_NOTE_GENERATE = "The image has no watermarks, logos, captions or overlaid text."

# extend canvas: the new area and erased parts arrive as blurred, smeared colour; edit models copy their input,
# so without this they keep the blur
OUTPAINT_NOTE = ("The blurred, smeared areas of the image are missing parts of the photo. Fill them with sharp, "
                 "realistic content that continues the scene naturally, matching the perspective, light and detail of "
                 "the rest. Keep everything sharp in the image as it is.")

KEEP_IDENTICAL = ("Keep everything else in the image exactly identical to the original: same framing, "
                  "perspective, positions, people, objects, colors, lighting and fine details. "
                  "Only change what is described above.")


# extra reference images (besides the edited image) the text encoder of each family takes:
# TextEncodeQwenImage21 has up to 16 slots, TextEncodeQwenImageEditPlus 3 in total (image1 = edited image)
MAX_REFS = {"qwen21": 3, "qwen21_turbo": 3, "qwen_edit": 2}


REF_NOTE = ("{main} is the image to edit. Keep its framing, composition, camera angle, perspective and "
            "everything the instruction does not change. Take from {refs} only what the instruction asks for. "
            "Add no body parts, people or objects that the instruction does not ask for.")


def ref_labels(family: str, n: int) -> tuple[str, list[str]]:
    """The encoder's own names for the edited image and the references: Qwen 2.1 tags them <image1>, <image2>
    (TextEncodeQwenImage21), Edit 2511 "Picture 1", "Picture 2" (TextEncodeQwenImageEditPlus)."""
    name = (lambda i: f"Picture {i}") if family == "qwen_edit" else (lambda i: f"<image{i}>")
    return name(1), [name(i) for i in range(2, n + 2)]


def reference_note(p: dict[str, Any]) -> str:
    """Hidden instruction for edits with extra reference images: without it the model sometimes takes
    a reference as the image to edit (its framing and all) when the prompt does not say which is which.
    p["ref_note"] overrides it (Advanced in the UI); an empty string turns it off."""
    family = p.get("family", "qwen21")
    n = min(len(p.get("refs") or []), MAX_REFS.get(family, 0))
    if not n or p.get("task") == "generate":
        return ""
    if p.get("ref_note") is not None:
        return str(p["ref_note"]).strip()
    main, refs = ref_labels(family, n)
    return REF_NOTE.format(main=main, refs=refs[0] if n == 1 else ", ".join(refs[:-1]) + " and " + refs[-1])


def reference_takes(p: dict[str, Any]) -> str:
    """p["ref_takes"]: one short text per reference ("face"). Each becomes an explicit replacement order after
    the prompt, so it counts as part of the instruction even when the prompt does not mention it."""
    family = p.get("family", "qwen21")
    n = min(len(p.get("refs") or []), MAX_REFS.get(family, 0))
    if not n or p.get("task") == "generate":
        return ""
    main, refs = ref_labels(family, n)
    takes = [str(t).strip().rstrip(".") for t in (p.get("ref_takes") or [])[:n]]
    orders = [f"Replace the {t} in {main} with the {t} from {ref}, in the place and at the size of the {t} in {main}."
              for ref, t in zip(refs, takes) if t]
    if not orders:
        return ""
    # a "face" take otherwise brings the whole head (hair, no cap) from the reference
    one = len(orders) == 1
    keep = (f"Replace only {'this part itself' if one else 'these parts themselves'}: anything on, over or around "
            f"{'it' if one else 'them'} in {main}, like headwear, glasses, hair, jewellery or clothing, stays unless "
            "the instruction changes it.")
    return " ".join([*orders, keep])


def edit_prompt(p: dict[str, Any]) -> str:
    prompt = p["prompt"].strip()
    if note := reference_note(p):
        prompt = f"{note}\n\n{prompt}"
    if takes := reference_takes(p):
        prompt = f"{prompt}\n\n{takes}"
    if p.get("clean_overlays"):
        prompt = f"{prompt}\n\n{CLEAN_NOTE_GENERATE if p.get('task') == 'generate' else CLEAN_NOTE}"
    if p.get("outpaint"):
        prompt = f"{prompt}\n\n{OUTPAINT_NOTE}"
    if p.get("mode") == "paste" or (p.get("keep_whole") and not p.get("use_mask")):
        # also for whole-image edits when asked (keep_whole); p["keep_note"] replaces the default (Advanced in the UI), empty turns it off;
        # keep_identical=False is the older way to turn it off
        keep = KEEP_IDENTICAL if p.get("keep_identical", True) else ""
        if p.get("keep_note") is not None:
            keep = str(p["keep_note"]).strip()
        if keep:
            return f"{prompt.strip()}\n\n{keep}"
    return prompt


CLIP_TYPES = {"qwen21": "qwen_image", "qwen21_turbo": "qwen_image", "qwen_edit": "qwen_image", "qwen": "qwen_image",
              "zimage": "lumina2"}
CPU_TEXT_ENCODER = {"qwen_edit", "qwen"}  # fp8 text encoders cannot run on MPS
# Viggle Turbo raw sigma nodes per step count (5-7); change steps only at the high-noise end
VIGGLE_NODES = {5: "1.0, 0.875, 0.75, 0.5, 0.25", 6: "1.0, 0.9375, 0.875, 0.75, 0.5, 0.25",
                7: "1.0, 0.9583, 0.9167, 0.875, 0.75, 0.5, 0.25"}


def crop_box(c: Any) -> dict[str, int] | None:
    """A valid {x, y, w, h} pixel crop (at least 16 px per side) or None for the whole image."""
    if not isinstance(c, dict):
        return None
    try:
        box = {k: int(round(float(c[k]))) for k in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        return None
    if box["x"] < 0 or box["y"] < 0 or box["w"] < 16 or box["h"] < 16:
        return None
    return box


def reference_images(g: dict[str, Any], p: dict[str, Any], family: str) -> list[list]:
    """Load and scale the extra reference images (p["refs"], input filenames) like the main image.

    p["ref_crops"][i] (optional): {x, y, w, h} in the reference's pixels. Only that part goes to the
    encoder, at most at its own size (a small crop is not scaled up), which also makes every step faster."""
    links = []
    crops = p.get("ref_crops") or []
    for i, name in enumerate((p.get("refs") or [])[:MAX_REFS.get(family, 0)], start=1):
        g[f"ref{i}_load"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        src, mp = [f"ref{i}_load", 0], p["megapixels"]
        if (c := crop_box(crops[i - 1] if i <= len(crops) else None)):
            g[f"ref{i}_crop"] = {"class_type": "ImageCrop", "inputs": {
                "image": src, "width": c["w"], "height": c["h"], "x": c["x"], "y": c["y"]}}
            src, mp = [f"ref{i}_crop", 0], min(mp, round(c["w"] * c["h"] / 1e6, 3))
        g[f"ref{i}_scale"] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
            "image": src, "upscale_method": "lanczos", "megapixels": mp, "resolution_steps": SCALE_STEPS}}
        links.append([f"ref{i}_scale", 0])
    return links


MAX_LORAS = 3


def apply_loras(g: dict[str, Any], loras: Any) -> None:
    """LoRAs ([{name, strength}], at most MAX_LORAS) chained after the diffusion model loader. The last
    LoRA node takes the key "unet", so every node that uses the model gets it with the LoRAs applied."""
    valid = [(str(l["name"]), float(l.get("strength", 1.0))) for l in (loras or [])
             if isinstance(l, dict) and l.get("name") and float(l.get("strength", 1.0)) != 0][:MAX_LORAS]
    if not valid:
        return
    g["unet_file"] = g.pop("unet")
    prev = ["unet_file", 0]
    for i, (name, strength) in enumerate(valid):
        key = "unet" if i == len(valid) - 1 else f"lora_{i}"
        g[key] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": prev, "lora_name": name, "strength_model": strength}}
        prev = [key, 0]


def turbo_steps(steps: int) -> int:
    return min(7, max(5, int(steps)))


def build_edit_graph(p: dict[str, Any]) -> dict:
    """Step 2: edit an input image (task "edit") or generate from text (task "generate").

    Expected keys: image, mask (input filename or None), refs (extra reference images, optional),
    use_mask, megapixels, resolution,
    prompt, negative, steps, denoise, seed, cfg, sampler, scheduler, feather,
    unet, clip, vae, prefix, mode, family, task.

    family (see presets.py): "qwen21" (Qwen-Image 2.1, edit + generate), "qwen21_turbo" (same with
    the Viggle 6-step sigmas, no CFG), "qwen_edit" (Qwen-Image-Edit 2511), "qwen" (Qwen-Image 2512,
    generate only) or "zimage" (Z-Image Turbo: generate, edit = img2img/inpaint).

    mode "inpaint" (default): only the masked latent area is re-generated (noise mask).
    mode "paste": the model edits the whole image freely, then only the masked area is
    pasted into the original. Better when the model keeps copying the old content.
    """
    family = p.get("family", "qwen21")
    generate = p.get("task") == "generate"
    # empty when the page loaded before ComfyUI listed its options
    p = {**p, "sampler": p.get("sampler") or "euler", "scheduler": p.get("scheduler") or "simple"}
    g: dict[str, Any] = {
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": p["vae"]}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": p["clip"], "type": CLIP_TYPES[family],
                                                         "device": "cpu" if family in CPU_TEXT_ENCODER else "default"}},
    }
    if not generate:
        g["load"] = {"class_type": "LoadImage", "inputs": {"image": p["image"]}}
        g["scale"] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
            "image": ["load", 0], "upscale_method": "lanczos", "megapixels": p["megapixels"], "resolution_steps": SCALE_STEPS}}
    unet = p["unet"]
    if unet.endswith(".gguf"):
        g["unet"] = {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": unet}}
    else:
        g["unet"] = {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}}
    apply_loras(g, p.get("loras"))

    prompt, negative = edit_prompt(p), p.get("negative", "")
    refs = reference_images(g, p, family)
    ref_latent = None
    if family in ("qwen21", "qwen21_turbo"):
        g["model"] = {"class_type": "QwenImage21Cache", "inputs": {"model": ["unet", 0], "device": "auto", "dtype": "default"}}
        g["encode"] = {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["clip", 0], "vae": ["vae", 0], "prompt": prompt, "negative_prompt": negative, "resolution": p["resolution"]}}
        # the edited image is image 1 (its size sets the latent), extra references follow
        images = refs if generate else [["scale", 0], *refs]
        for i, link in enumerate(images, start=1):
            g["encode"]["inputs"][f"images.image_{i}"] = link
        if not generate:
            ref_latent = ["encode", 2]
        pos, neg = ["encode", 0], ["encode", 1]
    elif family == "qwen_edit":
        g["model_shift"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["unet", 0], "shift": 3.1}}
        g["model"] = {"class_type": "CFGNorm", "inputs": {"model": ["model_shift", 0], "strength": 1.0}}
        for key, text in (("encode", prompt), ("encode_neg", negative)):
            g[f"{key}_raw"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {
                "clip": ["clip", 0], "prompt": text, "vae": ["vae", 0], "image1": ["scale", 0],
                **{f"image{i}": link for i, link in enumerate(refs, start=2)}}}
            g[key] = {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {
                "conditioning": [f"{key}_raw", 0], "reference_latents_method": "index_timestep_zero"}}
        pos, neg = ["encode", 0], ["encode_neg", 0]
    elif family == "qwen":
        g["model"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["unet", 0], "shift": 3.1}}
        g["encode"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}}
        g["encode_neg"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": negative}}
        pos, neg = ["encode", 0], ["encode_neg", 0]
    elif family == "zimage":
        g["model"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["unet", 0], "shift": 3.0}}
        g["encode"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}}
        if negative.strip():
            g["encode_neg"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": negative}}
        else:
            g["encode_neg"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["encode", 0]}}
        pos, neg = ["encode", 0], ["encode_neg", 0]
    else:
        raise ValueError(f"unknown model family {family}")

    if generate:
        empty = "EmptyLatentImage" if family in ("qwen21", "qwen21_turbo") else "EmptySD3LatentImage"
        g["latent_src"] = {"class_type": empty, "inputs": {"width": p["work_w"], "height": p["work_h"], "batch_size": 1}}
        latent = ["latent_src", 0]
        return _sample_and_save(g, p, latent, pos, neg, use_mask=False, generate=True)

    use_mask = bool(p.get("use_mask")) and bool(p.get("mask"))
    if use_mask:
        g["mask_load"] = {"class_type": "LoadImageMask", "inputs": {"image": p["mask"], "channel": "red"}}
        g["mask_img"] = {"class_type": "MaskToImage", "inputs": {"mask": ["mask_load", 0]}}
        # the mask is drawn at working size; scale defensively in case it is off by a few pixels
        g["mask_fit"] = {"class_type": "ImageScale", "inputs": {
            "image": ["mask_img", 0], "upscale_method": "bilinear", "width": p["work_w"], "height": p["work_h"], "crop": "disabled"}}
        mask_img = ["mask_fit", 0]
        if p.get("feather", 0) > 0:
            g["feather"] = {"class_type": "ImageBlur", "inputs": {"image": mask_img, "blur_radius": int(p["feather"]), "sigma": max(1.0, p["feather"] / 3)}}
            mask_img = ["feather", 0]
        g["mask"] = {"class_type": "ImageToMask", "inputs": {"image": mask_img, "channel": "red"}}
        # hard 0/1 mask for sampling: soft edges let the chunked sampler leak garbage from the
        # unmasked context into the result; the soft mask is only used for pasting
        # snapped to the 16 px latent grid so every latent cell is fully inside or outside
        g["mask_grid_down"] = {"class_type": "ImageScale", "inputs": {
            "image": ["mask_fit", 0], "upscale_method": "area", "width": p["work_w"] // 16, "height": p["work_h"] // 16, "crop": "disabled"}}
        g["mask_grid_up"] = {"class_type": "ImageScale", "inputs": {
            "image": ["mask_grid_down", 0], "upscale_method": "nearest-exact", "width": p["work_w"], "height": p["work_h"], "crop": "disabled"}}
        g["mask_hard_src"] = {"class_type": "ImageToMask", "inputs": {"image": ["mask_grid_up", 0], "channel": "red"}}
        g["mask_hard"] = {"class_type": "ThresholdMask", "inputs": {"mask": ["mask_hard_src", 0], "value": 0.5}}
        if p.get("mode", "inpaint") == "paste" and family != "zimage":
            # start from the original latent (exact working size, no noise mask): the edit stays
            # pixel-aligned with the original, and denoise < 1 keeps it even closer
            g["latent_src"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["scale", 0], "vae": ["vae", 0]}}
            latent = ["latent_src", 0]
        else:
            g["latent_src"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["scale", 0], "vae": ["vae", 0]}}
            g["latent"] = {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["latent_src", 0], "mask": ["mask_hard", 0]}}
            latent = ["latent", 0]
    elif ref_latent:
        latent = ref_latent
    else:
        g["latent_src"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["scale", 0], "vae": ["vae", 0]}}
        latent = ["latent_src", 0]
    return _sample_and_save(g, p, latent, pos, neg, use_mask=use_mask, generate=False)


def _sample_and_save(g: dict, p: dict[str, Any], latent: list, pos: list, neg: list, use_mask: bool, generate: bool) -> dict:
    every, last = int(p.get("save_every") or 0), int(p.get("save_last") or 0)
    if every > 0 or last > 0 or p.get("family") == "qwen21_turbo":  # turbo needs its own sigmas
        final_latent = _chunked_sampler(g, p, latent, every, last, pos, neg)
    else:
        g["sampler"] = {"class_type": "KSampler", "inputs": {
            "model": ["model", 0], "positive": pos, "negative": neg, "latent_image": latent,
            "seed": int(p["seed"]), "steps": int(p["steps"]), "cfg": float(p["cfg"]),
            "sampler_name": p["sampler"], "scheduler": p["scheduler"], "denoise": float(p["denoise"])}}
        final_latent = ["sampler", 0]
    g["decode"] = {"class_type": "VAEDecode", "inputs": {"samples": final_latent, "vae": ["vae", 0]}}
    result = ["decode", 0]
    if use_mask:
        g["composite"] = {"class_type": "ImageCompositeMasked", "inputs": {
            "destination": ["scale", 0], "source": ["decode", 0], "x": 0, "y": 0,
            # in paste mode the free edit is reference-sized, which can differ by a few pixels
            "resize_source": True, "mask": ["mask", 0]}}
        result = ["composite", 0]
    # saved (not temp previews) so the run history survives reloads and ComfyUI restarts;
    # the server strips ComfyUI's _00001_ counter afterwards (every run has its own prefix)
    prefix = p.get("prefix", "InpaintStudio/edit")
    g["out_result"] = {"class_type": "SaveImage", "inputs": {"images": result, "filename_prefix": prefix}}
    factor = int(p.get("upscale") or 0)
    if factor > 1 and p.get("upscale_model") and p.get("upscale_engine") == "seedvr2":
        files = {"model": p["upscale_model"], "vae": p["upscale_vae"]}
        up = _seedvr2(g, result, files, factor, p.get("color_correction") or "lab", int(p.get("seed") or 0), "sv_")
        g["out_upscaled"] = {"class_type": "SaveImage", "inputs": {"images": up, "filename_prefix": f"{prefix}_x{factor}"}}
    elif factor > 1 and p.get("upscale_model"):
        # in pixel space with an upscale model (tiled), so no VAE decode at the large size
        g["up_model"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": p["upscale_model"]}}
        g["up"] = {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["up_model", 0], "image": result}}
        up = ["up", 0]
        native = int(p.get("upscale_native") or factor)
        if native != factor:
            g["up_fit"] = {"class_type": "ImageScaleBy", "inputs": {"image": up, "upscale_method": "lanczos", "scale_by": factor / native}}
            up = ["up_fit", 0]
        g["out_upscaled"] = {"class_type": "SaveImage", "inputs": {"images": up, "filename_prefix": f"{prefix}_x{factor}"}}
    if not generate:
        g["out_before"] = {"class_type": "SaveImage", "inputs": {"images": ["scale", 0], "filename_prefix": f"{prefix}/before"}}
    if use_mask:  # the raw model output before pasting, to judge how well it lines up
        g["out_raw"] = {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": f"{prefix}_raw"}}
    return g


def _save_chunk(g: dict, i: int, a: int, b: int, steps: int, prefix: str, paste_mask: list | None, noise_masked: bool) -> None:
    """Decode the chunk's denoised prediction and save it as step_B_STEPS (pasted into the original
    when there is a mask, plus the raw full image in paste mode)."""
    g[f"chunk_dec_{i}"] = {"class_type": "VAEDecode", "inputs": {"samples": [f"chunk_{i}", 1], "vae": ["vae", 0]}}
    step_img = [f"chunk_dec_{i}", 0]
    if paste_mask and not noise_masked:
        g[f"stepraw_{b}"] = {"class_type": "SaveImage", "inputs": {
            "images": step_img, "filename_prefix": f"{prefix}/raw_step_{step_name(b, steps)}"}}
    if paste_mask:
        g[f"chunk_paste_{i}"] = {"class_type": "ImageCompositeMasked", "inputs": {
            "destination": ["scale", 0], "source": step_img, "x": 0, "y": 0, "resize_source": True, "mask": paste_mask}}
        step_img = [f"chunk_paste_{i}", 0]
    g[f"stepsave_{b}"] = {"class_type": "SaveImage", "inputs": {
        "images": step_img, "filename_prefix": f"{prefix}/step_{step_name(b, steps)}"}}


def step_chunks(steps: int, every: int, last: int = 0) -> list[tuple[int, int]]:
    """Chunk boundaries: every N steps, each of the last N steps, and the final step."""
    ends = {steps}
    if every > 0:
        ends.update(range(every, steps, every))
    if last > 0:
        ends.update(range(max(1, steps - last), steps))
    ends = sorted(ends)
    return list(zip([0] + ends[:-1], ends))


PHASES = ("load", "encode", "sample", "decode", "save")


def node_phase(node: str, class_type: str, chunks: list[tuple[int, int]] | None = None,
               n_refs: int = 0) -> dict | None:
    """Which part of the workflow a ComfyUI node belongs to, for the live workflow strip in Runs:
    {"phase", "detail"}, or None for helper nodes (sigmas, noise, guider …) that should not move it.
    ComfyUI loads models lazily: the text encoder inside the encode node, the diffusion model in the
    first sampler node, so those phases include the loading."""
    if class_type.startswith(("TextEncode", "CLIPTextEncode")):
        return {"phase": "encode", "detail": f"+ {n_refs} ref{'s' if n_refs > 1 else ''}" if n_refs else ""}
    if class_type.startswith("VAEEncode"):
        return {"phase": "encode", "detail": "image"}
    if class_type.startswith(("SamplerCustom", "KSampler")):
        return {"phase": "sample", "detail": ""}
    if class_type.startswith("VAEDecode"):
        if node.startswith("chunk_dec_") and chunks:
            k = int(node.rsplit("_", 1)[1])
            if k < len(chunks) - 1:
                return {"phase": "decode", "detail": f"step {chunks[k][1]}"}
        return {"phase": "decode", "detail": "result"}
    if class_type in ("SaveImage", "PreviewImage") or class_type.startswith(("ImageComposite", "ImageUpscale", "Upscale")):
        return {"phase": "save", "detail": "upscaling" if "Upscale" in class_type else ""}
    if node == "up_fit":   # fits a classic upscaler's output to the wanted factor
        return {"phase": "save", "detail": "upscaling"}
    if class_type.startswith(("LoadImage", "ImageScale", "ImageCrop")) or class_type.endswith("Loader") \
            or "Loader" in class_type:
        return {"phase": "load", "detail": ""}
    return None


def step_name(step: int, steps: int) -> str:
    return f"{step:0{len(str(steps))}d}_{steps}"


def _chunked_sampler(g: dict, p: dict[str, Any], latent: list, every: int, last: int, pos: list, neg: list) -> list:
    """Same scheme as the 'Qwen2.1 GGUF Steps' workflow, unrolled instead of a loop node:
    each chunk samples sigmas[start..end] with SamplerCustomAdvanced (noise only in the first
    chunk), and the chunk's denoised prediction is decoded and saved as step_END_STEPS."""
    turbo = p.get("family") == "qwen21_turbo"
    steps = turbo_steps(p["steps"]) if turbo else int(p["steps"])
    save_steps = every > 0 or last > 0
    g["noise"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": int(p["seed"])}}
    g["no_noise"] = {"class_type": "DisableNoise", "inputs": {}}
    if turbo:  # distilled: no CFG, euler on the Viggle sigma schedule (sized from the latent)
        g["guider"] = {"class_type": "BasicGuider", "inputs": {"model": ["model", 0], "conditioning": pos}}
        g["ksel"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}}
        g["sched"] = {"class_type": "ViggleTurboSigmas", "inputs": {"latent": latent, "nodes": VIGGLE_NODES[steps]}}
    else:
        g["guider"] = {"class_type": "CFGGuider", "inputs": {
            "model": ["model", 0], "positive": pos, "negative": neg, "cfg": float(p["cfg"])}}
        g["ksel"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": p["sampler"]}}
        g["sched"] = {"class_type": "BasicScheduler", "inputs": {
            "model": ["model", 0], "scheduler": p["scheduler"], "steps": steps, "denoise": float(p["denoise"])}}
    prefix = p.get("prefix", "InpaintStudio/edit")
    paste_mask = ["mask", 0] if "mask" in g else None
    noise_masked = "latent" in g
    if noise_masked:
        # Each chunk divides the whole latent by (1 - sigma_end); a single run does that once,
        # chunks compound it and the unmasked area drifts (turns purple). Restoring the area
        # outside the mask after every chunk keeps it at the clean original.
        g["outside"] = {"class_type": "InvertMask", "inputs": {"mask": ["mask_hard", 0]}}
    for i, (a, b) in enumerate(step_chunks(steps, every, last)):
        g[f"upto_{i}"] = {"class_type": "SplitSigmas", "inputs": {"sigmas": ["sched", 0], "step": b}}
        g[f"from_{i}"] = {"class_type": "SplitSigmas", "inputs": {"sigmas": [f"upto_{i}", 0], "step": a}}
        g[f"chunk_{i}"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0] if i == 0 else ["no_noise", 0], "guider": ["guider", 0], "sampler": ["ksel", 0],
            "sigmas": [f"from_{i}", 1], "latent_image": latent}}
        if save_steps:
            _save_chunk(g, i, a, b, steps, prefix, paste_mask, noise_masked)
        if noise_masked:
            g[f"chunk_fix_{i}"] = {"class_type": "LatentCompositeMasked", "inputs": {
                "destination": [f"chunk_{i}", 0], "source": latent, "x": 0, "y": 0, "resize_source": False, "mask": ["outside", 0]}}
            latent = [f"chunk_fix_{i}", 0]
        else:
            latent = [f"chunk_{i}", 0]
    return latent


SEEDVR2_COLORS = ("lab", "wavelet", "adain", "none")


def _seedvr2(g: dict[str, Any], image: list, files: dict[str, str], factor: float, color_correction: str,
             seed: int, key: str = "", size: tuple[int, int] | None = None) -> list:
    """SeedVR2 nodes after ComfyUI's utility_seedvr2 template (resize by the factor, or to `size`, one sampler step,
    tiled VAE, colour correction); `key` prefixes the node ids so they fit into a larger graph."""
    tiles = {"tile_size": 512, "overlap": 128, "temporal_size": 4096, "temporal_overlap": 8}
    k = lambda n: f"{key}{n}"
    g.update({
        k("unet"): {"class_type": "UNETLoader", "inputs": {"unet_name": files["model"], "weight_dtype": "default"}},
        k("vae"): {"class_type": "VAELoader", "inputs": {"vae_name": files["vae"]}},
        k("resize"): _scale(image, factor, size),
        k("pre"): {"class_type": "SeedVR2Preprocess", "inputs": {"resized_images": [k("resize"), 0]}},
        k("encode"): {"class_type": "VAEEncodeTiled", "inputs": {"pixels": [k("pre"), 0], "vae": [k("vae"), 0], **tiles}},
        k("cond"): {"class_type": "SeedVR2Conditioning", "inputs": {"model": [k("unet"), 0], "vae_conditioning": [k("encode"), 0]}},
        k("sampler"): {"class_type": "KSampler", "inputs": {
            "model": [k("unet"), 0], "positive": [k("cond"), 0], "negative": [k("cond"), 1], "latent_image": [k("encode"), 0],
            "seed": seed, "steps": 1, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        k("decode"): {"class_type": "VAEDecodeTiled", "inputs": {"samples": [k("sampler"), 0], "vae": [k("vae"), 0], **tiles}},
        k("post"): {"class_type": "SeedVR2PostProcessing", "inputs": {
            "images": [k("decode"), 0], "original_resized_images": [k("resize"), 0],
            "color_correction_method": color_correction if color_correction in SEEDVR2_COLORS else "lab"}},
    })
    return [k("post"), 0]


def _scale(image: list, factor: float, size: tuple[int, int] | None) -> dict:
    """Lanczos resize by `factor`, or to exactly `size` (w, h) when given."""
    if size:
        return {"class_type": "ImageScale", "inputs": {"image": image, "upscale_method": "lanczos",
                                                       "width": size[0], "height": size[1], "crop": "disabled"}}
    return {"class_type": "ImageScaleBy", "inputs": {"image": image, "upscale_method": "lanczos", "scale_by": factor}}


def build_upscale_graph(image: str, comp: dict[str, Any], files: dict[str, str], factor: float, prefix: str,
                        color_correction: str = "lab", seed: int = 0, size: tuple[int, int] | None = None) -> dict:
    """Upscale an image as its own run. Classic upscalers (`UpscaleModelLoader`) are fitted to `factor`
    (or to exactly `size`, from a target width) when their native scale differs; SeedVR2 (`comp["engine"] == "seedvr2"`) follows ComfyUI's
    utility_seedvr2 template: resize by the factor, one sampler step, tiled VAE, colour correction.
    `files`: the model file names ("model", and "vae" for SeedVR2)."""
    g: dict[str, Any] = {"load": {"class_type": "LoadImage", "inputs": {"image": image}}}
    if comp.get("engine") == "seedvr2":
        up = _seedvr2(g, ["load", 0], files, factor, color_correction, seed, size=size)
    else:
        g["up_model"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": files["model"]}}
        g["up"] = {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["up_model", 0], "image": ["load", 0]}}
        up = ["up", 0]
        native = int(comp.get("scale") or factor)
        if size or native != factor:
            g["up_fit"] = _scale(up, factor / native, size)
            up = ["up_fit", 0]
    g["out_result"] = {"class_type": "SaveImage", "inputs": {"images": up, "filename_prefix": prefix}}
    return g
