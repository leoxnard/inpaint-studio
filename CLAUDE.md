Brain note: ~/brain/Projects/inpaint-studio.md

# Inpaint Studio

Local two-step UI on top of ComfyUI (127.0.0.1:8188): step 1 runs only SAM3 to get a mask
(tune + brush-paint it), step 2 runs a Qwen-Image 2.1 edit restricted to the mask with live
per-step previews. Runs only on Leonard's Mac, no deploy.

## Commands
- Run: `./run.sh` (http://127.0.0.1:7380)
- Tests: `uv run pytest`

## Architecture
- `graphs.py` – size math (mirrors ImageScaleToTotalPixels / TextEncodeQwenImage21) and the
  ComfyUI API graph builders. Pure functions, unit-tested.
- `server.py` – FastAPI. Proxies uploads/views to ComfyUI, runs the mask graph synchronously,
  and relays an edit job over `/ws/edit` (ComfyUI ws progress + binary latent previews via
  `extra_data.preview_method`).
- `web/` – vanilla HTML/JS/CSS, no build step.

## Rules
- Gray-noise limit: on MPS the edit breaks at >= 4096 latent tokens for target or reference
  (pixels/16 per side). `graphs.TOKEN_LIMIT`; the UI auto-fixes size by default.
- Model, text encoder and VAE must match (Qwen-Image 2.1 ↔ qwen3vl_8b ↔ qwen_image_2.1 VAE).
- Uploads go to ComfyUI `input/inpaint-studio/`, results to `output/InpaintStudio/`.
