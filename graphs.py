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


def build_edit_graph(p: dict[str, Any]) -> dict:
    """Step 2: Qwen-Image 2.1 edit, optionally restricted to an uploaded mask.

    Expected keys: image, mask (input filename or None), use_mask, megapixels, resolution,
    prompt, negative, steps, denoise, seed, cfg, sampler, scheduler, feather,
    unet, clip, vae, prefix, mode.

    mode "inpaint" (default): only the masked latent area is re-generated (noise mask).
    mode "paste": the model edits the whole image freely, then only the masked area is
    pasted into the original. Better when the model keeps copying the old content.
    """
    g: dict[str, Any] = {
        "load": {"class_type": "LoadImage", "inputs": {"image": p["image"]}},
        "scale": {"class_type": "ImageScaleToTotalPixels", "inputs": {
            "image": ["load", 0], "upscale_method": "lanczos", "megapixels": p["megapixels"], "resolution_steps": SCALE_STEPS}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": p["vae"]}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": p["clip"], "type": "qwen_image", "device": "default"}},
        "encode": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["clip", 0], "vae": ["vae", 0], "images.image_1": ["scale", 0],
            "prompt": p["prompt"], "negative_prompt": p.get("negative", ""), "resolution": p["resolution"]}},
    }
    unet = p["unet"]
    if unet.endswith(".gguf"):
        g["unet"] = {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": unet}}
    else:
        g["unet"] = {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}}
    g["model"] = {"class_type": "QwenImage21Cache", "inputs": {"model": ["unet", 0], "device": "auto", "dtype": "default"}}

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
        if p.get("mode", "inpaint") == "paste":
            latent = ["encode", 2]
        else:
            g["latent_src"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["scale", 0], "vae": ["vae", 0]}}
            g["latent"] = {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["latent_src", 0], "mask": ["mask_hard", 0]}}
            latent = ["latent", 0]
    else:
        latent = ["encode", 2]

    every = int(p.get("save_every") or 0)
    if every > 0:
        final_latent = _chunked_sampler(g, p, latent, every)
    else:
        g["sampler"] = {"class_type": "KSampler", "inputs": {
            "model": ["model", 0], "positive": ["encode", 0], "negative": ["encode", 1], "latent_image": latent,
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
    g["out_result"] = {"class_type": "SaveImage", "inputs": {"images": result, "filename_prefix": p.get("prefix", "InpaintStudio/edit")}}
    g["out_before"] = {"class_type": "PreviewImage", "inputs": {"images": ["scale", 0]}}
    return g


def step_chunks(steps: int, every: int) -> list[tuple[int, int]]:
    return [(a, min(a + every, steps)) for a in range(0, steps, every)]


def _chunked_sampler(g: dict, p: dict[str, Any], latent: list, every: int) -> list:
    """Same scheme as the 'Qwen2.1 GGUF Steps' workflow, unrolled instead of a loop node:
    each chunk samples sigmas[start..end] with SamplerCustomAdvanced (noise only in the first
    chunk), and the chunk's denoised prediction is decoded and saved as step_START-END."""
    steps = int(p["steps"])
    g["noise"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": int(p["seed"])}}
    g["no_noise"] = {"class_type": "DisableNoise", "inputs": {}}
    g["guider"] = {"class_type": "CFGGuider", "inputs": {
        "model": ["model", 0], "positive": ["encode", 0], "negative": ["encode", 1], "cfg": float(p["cfg"])}}
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
    for i, (a, b) in enumerate(step_chunks(steps, every)):
        g[f"upto_{i}"] = {"class_type": "SplitSigmas", "inputs": {"sigmas": ["sched", 0], "step": b}}
        g[f"from_{i}"] = {"class_type": "SplitSigmas", "inputs": {"sigmas": [f"upto_{i}", 0], "step": a}}
        g[f"chunk_{i}"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0] if i == 0 else ["no_noise", 0], "guider": ["guider", 0], "sampler": ["ksel", 0],
            "sigmas": [f"from_{i}", 1], "latent_image": latent}}
        g[f"chunk_dec_{i}"] = {"class_type": "VAEDecode", "inputs": {"samples": [f"chunk_{i}", 1], "vae": ["vae", 0]}}
        step_img = [f"chunk_dec_{i}", 0]
        if paste_mask:  # show each step the way the final result will look: pasted into the original
            g[f"chunk_paste_{i}"] = {"class_type": "ImageCompositeMasked", "inputs": {
                "destination": ["scale", 0], "source": step_img, "x": 0, "y": 0, "resize_source": True, "mask": paste_mask}}
            step_img = [f"chunk_paste_{i}", 0]
        g[f"stepsave_{b}"] = {"class_type": "SaveImage", "inputs": {
            "images": step_img, "filename_prefix": f"{prefix}/step_{a:03d}-{b:03d}"}}
        if noise_masked:
            g[f"chunk_fix_{i}"] = {"class_type": "LatentCompositeMasked", "inputs": {
                "destination": [f"chunk_{i}", 0], "source": latent, "x": 0, "y": 0, "resize_source": False, "mask": ["outside", 0]}}
            latent = [f"chunk_fix_{i}", 0]
        else:
            latent = [f"chunk_{i}", 0]
    return latent
