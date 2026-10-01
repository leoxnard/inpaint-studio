Brain note: ~/brain/Projects/inpaint-studio.md

# Inpaint Studio

Local two-step UI on top of ComfyUI (127.0.0.1:8188): step 1 runs only SAM3 to get a mask
(tune + brush-paint it), step 2 runs a Qwen-Image 2.1 edit restricted to the mask with live
per-step previews. Runs only on Leonard's Mac, no deploy.

## Commands
- Run: `./run.sh` (http://127.0.0.1:7380), or the Mac app built by `macos/build-app.sh`
  (`~/Applications/Inpaint Studio.app`; quitting it stops the server)
- Tests: `uv run pytest` (the queue integration test uses a tiny CPU-only graph against the
  running ComfyUI and is skipped when ComfyUI is down)

## Architecture
- `graphs.py` – size math (mirrors ImageScaleToTotalPixels / TextEncodeQwenImage21) and the
  ComfyUI API graph builders. Pure functions, unit-tested.
- `server.py` – FastAPI. Proxies uploads/views to ComfyUI, runs the mask graph synchronously.
  Edits are server-side jobs (`POST /api/jobs`): each job is submitted to ComfyUI at once and
  followed by a background task on its own ComfyUI ws (progress + binary latent previews via
  `extra_data.preview_method`). Browsers subscribe to `/ws/jobs`; jobs survive page reloads.
  Jobs live in memory: a server restart marks unfinished runs as errors.
- `align.py` – post-hoc alignment of a paste-mode free edit to the original (numpy phase
  correlation, coarse scale search + full-res sub-pixel pass). `POST /api/runs/{id}/align`.
- `web/` – vanilla HTML/JS/CSS, no build step.

## Rules
- Gray-noise limit: on MPS the edit breaks at >= 4096 latent tokens for target or reference
  (pixels/16 per side). `graphs.TOKEN_LIMIT`; the UI auto-fixes size by default.
- Model, text encoder and VAE must match (Qwen-Image 2.1 ↔ qwen3vl_8b ↔ qwen_image_2.1 VAE).
- Uploads go to ComfyUI `input/inpaint-studio/`, results (incl. before/raw/mask/step images) to
  `output/InpaintStudio/<run>/`.
- Run history: `data/runs/<id>/run.json` + live preview JPEGs (gitignored), served at
  `/data/runs`, listed by `GET /api/runs`, removed by `DELETE /api/runs/{id}`.
