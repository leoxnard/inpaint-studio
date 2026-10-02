"""Model presets for the download centre and the model picker.

A preset is one diffusion model (GGUF, several quantisations) plus the text encoder and VAE it
needs. `family` selects the graph builder in graphs.py; `modes` says what the model can do:
"edit" (instruction edit / img2img on an input image) and "generate" (text to image).
Sizes are bytes as listed on Hugging Face. Only formats that run on Apple Silicon are listed
(GGUF and int8 convrot; fp8, NVFP4 and MLX files are left out).
"""

from __future__ import annotations

from typing import Any

COMPONENTS: dict[str, dict[str, Any]] = {
    "qwen3vl_8b": {"title": "Qwen3-VL 8B text encoder (int8)", "repo": "Comfy-Org/Qwen-Image-2.1",
                   "path": "text_encoders/qwen3vl_8b_int8_convrot.safetensors", "folder": "text_encoders", "size": 9_350_798_360},
    "vae_qwen21": {"title": "Qwen-Image 2.1 VAE", "repo": "Comfy-Org/Qwen-Image-2.1",
                   "path": "vae/qwen_image_2.1_vae_bf16.safetensors", "folder": "vae", "size": 675_509_688},
    "qwen25vl_7b": {"title": "Qwen2.5-VL 7B text encoder (fp8)", "repo": "Comfy-Org/Qwen-Image_ComfyUI",
                    "path": "split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors", "folder": "text_encoders",
                    "size": 9_384_670_680},
    "vae_qwen": {"title": "Qwen-Image VAE", "repo": "Comfy-Org/Qwen-Image_ComfyUI",
                 "path": "split_files/vae/qwen_image_vae.safetensors", "folder": "vae", "size": 253_806_246},
    "qwen3_4b": {"title": "Qwen3 4B text encoder", "repo": "Comfy-Org/z_image_turbo",
                 "path": "split_files/text_encoders/qwen_3_4b.safetensors", "folder": "text_encoders", "size": 8_044_982_048},
    "vae_ae": {"title": "Flux AE VAE", "repo": "Comfy-Org/z_image_turbo",
               "path": "split_files/vae/ae.safetensors", "folder": "vae", "size": 335_304_388},
    "viggle_node": {"title": "Viggle Turbo sigmas (ComfyUI node)", "repo": "Viggle/Qwen-Image-2.1-viggle-turbo",
                    "path": "comfyui/viggle_turbo.py", "folder": "custom_node", "size": 5_467},
    "up_ultrasharp_v2": {"title": "UltraSharp V2 (4x)", "kind": "upscaler", "scale": 4, "repo": "Kim2091/UltraSharpV2",
                         "path": "4x-UltraSharpV2.safetensors", "folder": "upscale_models", "size": 139_792_588,
                         "description": "Optional upscaler: most detail for photos, a bit slower."},
    "up_ultrasharp": {"title": "4x-UltraSharp", "kind": "upscaler", "scale": 4, "repo": "Kim2091/UltraSharp",
                      "path": "4x-UltraSharp.safetensors", "folder": "upscale_models", "size": 66_864_028,
                      "description": "Optional upscaler: popular, sharp 4x upscale for photos."},
    "up_realesrgan_x2": {"title": "RealESRGAN 2x", "kind": "upscaler", "scale": 2, "repo": "ai-forever/Real-ESRGAN",
                         "path": "RealESRGAN_x2.pth", "folder": "upscale_models", "size": 67_061_725,
                         "description": "Optional upscaler: fast, natural-looking 2x upscale."},
    "sam3": {"title": "SAM3 (masking)", "repo": "Comfy-Org/sam3.1",
             "path": "checkpoints/sam3.1_multiplex_fp16.safetensors", "folder": "checkpoints", "size": 1_745_546_848},
}


def _quants(stem: str, sizes: dict[str, int]) -> dict[str, dict[str, Any]]:
    """GGUF files are '<stem>-<quant>.gguf'; int8 convrot is a safetensors file (normal UNet loader)."""
    return {q: {"file": f"{stem}-{q}.{'safetensors' if q == 'int8_convrot' else 'gguf'}", "size": s} for q, s in sizes.items()}


PRESETS: dict[str, dict[str, Any]] = {
    "qwen21_uc": {
        "good_for": "Best all-rounder for editing photos by instruction: replace, remove or restyle things. No content filter.",
        "recommended": True,
        "title": "Qwen-Image 2.1 UC", "family": "qwen21", "modes": ["edit", "generate"],
        "note": "Uncensored fine-tune of Qwen-Image 2.1. Edits by instruction and generates.",
        "repo": "abenzerps/Qwen-Image-2.1-Uncensored-GGUF", "default_quant": "Q8_0",
        "quants": _quants("qwen-image-2.1-UC", {
            "Q4_0": 4_151_573_280, "Q4_K_M": 4_604_558_112, "Q5_K_M": 5_221_284_640, "Q6_K": 5_876_556_576,
            "int8_convrot": 7_256_796_840, "Q8_0": 7_591_557_920, "BF16": 14_230_272_800}),
        "text_encoder": "qwen3vl_8b", "vae": "vae_qwen21",
        "defaults": {"steps": 20, "cfg": 1.0, "sampler": "euler", "scheduler": "simple"},
    },
    "qwen21": {
        "good_for": "Same as UC, but the official model with its usual content limits. Good default to share with others.",
        "title": "Qwen-Image 2.1", "family": "qwen21", "modes": ["edit", "generate"],
        "note": "Official Qwen-Image 2.1 (GGUF by unsloth). Edits by instruction and generates.",
        "repo": "unsloth/Qwen-Image-2.1-GGUF", "default_quant": "Q8_0",
        "quants": _quants("qwen-image-2.1", {
            "Q2_K": 2_466_137_824, "Q3_K_S": 2_724_742_880, "Q3_K_M": 3_168_290_528, "Q3_K_XL": 3_612_493_536,
            "Q4_K_S": 3_906_356_960, "Q4_K_M": 4_199_565_024, "Q5_K_S": 4_501_948_128, "Q5_K_M": 5_390_223_072,
            "Q6_K": 6_271_551_200, "Q6_K_XL": 6_718_506_720, "Q8_0": 7_640_860_384, "F16": 14_230_275_808}),
        "text_encoder": "qwen3vl_8b", "vae": "vae_qwen21",
        "defaults": {"steps": 20, "cfg": 1.0, "sampler": "euler", "scheduler": "simple"},
    },
    "qwen21_viggle": {
        "good_for": "Fast drafts and quick edits in 6 steps (about 5x faster). A bit softer detail, weaker on small text and complex edits.",
        "recommended": True,
        "title": "Qwen-Image 2.1 Viggle Turbo", "family": "qwen21_turbo", "modes": ["edit", "generate"],
        "note": "Few-step distilled Qwen-Image 2.1 (v0.3, LoRA merged): 6 steps, no CFG, about 5x faster.",
        "repo": "Viggle/Qwen-Image-2.1-viggle-turbo", "default_quant": "Q8_0",
        "quants": _quants("Qwen-Image-2.1-viggle-turbo-v0.3-6step", {
            "Q4_K_M": 4_335_931_552, "Q5_K_M": 5_141_237_920, "Q6_K": 5_996_875_936,
            "int8_convrot": 7_256_783_064, "Q8_0": 7_687_180_448}),
        "text_encoder": "qwen3vl_8b", "vae": "vae_qwen21", "nodes": ["viggle_node"],
        "defaults": {"steps": 6, "cfg": 1.0, "sampler": "euler", "scheduler": "simple"},
    },
    "qwen_edit_2511": {
        "good_for": "The hardest edits: keeping the identity of a person, group photos, relighting, new viewpoints, product and material swaps.",
        "title": "Qwen-Image-Edit 2511", "family": "qwen_edit", "modes": ["edit"], "experimental": True,
        "note": "20B edit model (GGUF by unsloth). Strong edits, but large and slow on 32 GB.",
        "repo": "unsloth/Qwen-Image-Edit-2511-GGUF", "default_quant": "Q4_K_S",
        "quants": _quants("qwen-image-edit-2511", {
            "Q2_K": 7_468_022_368, "Q3_K_S": 9_218_914_912, "Q3_K_M": 9_920_805_472, "Q3_K_L": 10_581_408_352,
            "Q4_0": 11_852_773_984, "Q4_K_S": 12_410_747_488, "Q4_1": 12_843_678_304, "Q4_K_M": 13_244_758_624,
            "Q5_K_S": 14_325_611_104, "Q5_0": 14_400_813_664, "Q5_K_M": 15_027_501_664, "Q5_1": 15_391_717_984,
            "Q6_K": 16_852_417_120, "Q8_0": 21_761_817_184, "BF16": 40_872_114_784}),
        "text_encoder": "qwen25vl_7b", "vae": "vae_qwen",
        "defaults": {"steps": 20, "cfg": 4.0, "sampler": "euler", "scheduler": "simple"},
    },
    "qwen_2512": {
        "good_for": "Top photorealism and rendered text for text-to-image: posters, slides, realistic people and nature.",
        "title": "Qwen-Image 2512", "family": "qwen", "modes": ["generate"], "experimental": True,
        "note": "20B text-to-image model (Dec 2025, GGUF by unsloth): very realistic people and text. Large and slow on 32 GB.",
        "repo": "unsloth/Qwen-Image-2512-GGUF", "default_quant": "Q4_K_S",
        "quants": _quants("qwen-image-2512", {
            "Q2_K": 7_333_837_344, "Q3_K_S": 9_223_928_352, "Q3_K_M": 9_932_896_800, "Q4_0": 11_852_773_920,
            "Q4_K_S": 12_268_010_016, "Q4_1": 12_843_678_240, "Q4_K_M": 13_244_758_560, "Q5_K_S": 14_298_184_224,
            "Q5_0": 14_400_813_600, "Q5_K_M": 15_000_074_784, "Q5_1": 15_391_717_920, "Q6_K": 16_824_990_240,
            "Q8_0": 21_761_817_120, "BF16": 40_872_114_720}),
        "text_encoder": "qwen25vl_7b", "vae": "vae_qwen",
        "defaults": {"steps": 20, "cfg": 4.0, "sampler": "euler", "scheduler": "simple"},
    },
    "zimage_turbo": {
        "good_for": "Very fast photorealistic text-to-image (8 steps) with good English/Chinese text.",
        "recommended": True,
        "title": "Z-Image Turbo", "family": "zimage", "modes": ["generate", "edit"],
        "note": "Fast text-to-image (8 steps, GGUF by unsloth). Edit = img2img/inpaint only, no instructions.",
        "repo": "unsloth/Z-Image-Turbo-GGUF", "default_quant": "Q8_0",
        "quants": _quants("z-image-turbo", {
            "Q2_K": 3_639_683_136, "Q3_K_S": 3_951_806_016, "Q3_K_M": 4_186_161_216, "Q4_0": 4_585_244_736,
            "Q4_K_S": 4_710_950_976, "Q4_1": 4_850_665_536, "Q4_K_M": 5_017_613_376, "Q5_K_S": 5_237_860_416,
            "Q5_0": 5_263_542_336, "Q5_1": 5_528_963_136, "Q5_K_M": 5_574_444_096, "Q6_K": 5_910_505_536,
            "Q8_0": 7_224_707_136, "BF16": 12_311_939_136}),
        "text_encoder": "qwen3_4b", "vae": "vae_ae",
        "defaults": {"steps": 8, "cfg": 1.0, "sampler": "res_multistep", "scheduler": "simple"},
    },
}

DEFAULT_PRESET = "qwen21_uc"


def resolve(preset_id: str, quant: str | None) -> dict[str, Any]:
    """File names (as ComfyUI lists them) and family for a preset + quantisation."""
    pr = PRESETS[preset_id]
    q = quant if quant in pr["quants"] else pr["default_quant"]
    return {"family": pr["family"], "unet": pr["quants"][q]["file"],
            "clip": COMPONENTS[pr["text_encoder"]]["path"].rsplit("/", 1)[-1],
            "vae": COMPONENTS[pr["vae"]]["path"].rsplit("/", 1)[-1]}


# ---------------------------------------------------------------- memory estimate (Apple Silicon)
# Unified memory: macOS lets the GPU use about 70-75 % of RAM. ComfyUI keeps the diffusion model
# and VAE loaded while sampling and loads the text encoder before that (and drops it if needed).
GPU_SHARE = 0.72
ACTIVATIONS = 3.0e9  # attention/latents at ~1 MP plus ComfyUI overhead


def memory_need(pid: str, quant: str) -> int:
    """Rough peak memory in bytes for one run of a preset + quantisation."""
    pr = PRESETS[pid]
    unet = pr["quants"][quant]["size"]
    vae = COMPONENTS[pr["vae"]]["size"]
    te = COMPONENTS[pr["text_encoder"]]["size"]
    return int(max(unet + vae + ACTIVATIONS, te + 1.5e9))


def memory_fit(need: int, ram: int) -> str:
    """'good' fits comfortably, 'tight' may swap and get slow, 'no' will not work well."""
    if need <= ram * GPU_SHARE:
        return "good"
    if need <= ram * 0.9:
        return "tight"
    return "no"


def recommended_quant(pid: str, ram: int) -> str:
    """Largest quant that fits comfortably, preferring Q8_0 over bigger 16-bit files."""
    pr = PRESETS[pid]
    fitting = [q for q in pr["quants"] if memory_fit(memory_need(pid, q), ram) == "good"]
    if not fitting:
        return min(pr["quants"], key=lambda q: pr["quants"][q]["size"])
    no16 = [q for q in fitting if q not in ("BF16", "F16")]
    pool = no16 or fitting
    return max(pool, key=lambda q: pr["quants"][q]["size"])
