Brain note: ~/brain/Projects/inpaint-studio.md

# Inpaint Studio

Local UI on top of ComfyUI (127.0.0.1:8188): optional SAM3 mask (tune + brush-paint), then an
edit (inpaint / free edit + paste / no mask) or text-to-image generate with model presets
(Qwen-Image 2.1 UC/official/Viggle Turbo, Edit 2511, 2512, Z-Image) and live per-step previews.
Runs on Leonard's Mac; the .app is self-contained so it can be shared. No deploy.

## Commands
- Run: `./run.sh` (http://127.0.0.1:7380), or the Mac app built by `macos/build-app.sh`
  (code bundled in the .app; the server itself starts ComfyUI headless if 8188 is free and stops it on
  shutdown). First-run setup: `installer.py` + `web/setup.js`; data in `~/Library/Application Support/Inpaint Studio`
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
- `align.py` – post-processing of a paste-mode free edit: shift/scale alignment (phase
  correlation), local warp fix (DIS optical flow), colour/exposure match (Lab offset field) and
  Poisson edges, all measured outside the mask. `POST /api/runs/{id}/align`; also run
  automatically after paste jobs (saved as `<run>_fixed.png`).
- `presets.py` – model presets (files, quants, sizes, defaults), components (encoders, VAEs,
  SAM3, upscalers, Viggle node) and the RAM-fit estimate. `installer.py` – setup/downloads and
  the headless ComfyUI process.
- `web/` – vanilla HTML/JS/CSS, no build step. Two views in one page: Create (`#createView`) and Runs
  (`#runsView`), switched by a hash router (`#create`, `#runs/<id>`). Hidden `#mode`, `#aspect`, `#viewRaw`
  stay the source of truth; the Area cards, aspect tiles and Pasted/Raw switch only write into them.
  Design tokens (Ollama style, see `design/`) are CSS variables at the top of `styles.css`.
- Dev preview: launch config `inpaint-studio-dev` (port 7381), since the installed app usually holds 7380.

## Rules
- Gray-noise limit: on MPS the edit breaks at >= 4096 latent tokens for target or reference
  (pixels/16 per side). `graphs.TOKEN_LIMIT`; the UI auto-fixes size by default.
- Model, text encoder and VAE must match: the presets encode the pairs. fp8 text encoders
  (Qwen2.5-VL for 2511/2512) run on the CPU (`device: cpu`), MPS cannot do fp8.
- Encoder resolution is matched to the working size by default (`graphs.matching_resolution`):
  a different reference size shifts/scales the free edit.
- Uploads go to ComfyUI `input/inpaint-studio/`. Results: `output/InpaintStudio/<run>.png`,
  `<run>_raw.png`, `<run>_fixed.png`, `<run>_x2.png`; `<run>/before.png` and
  `<run>/step_NN_TOTAL.png` (+ `raw_step_…`). The server strips ComfyUI's `_00001_` counter.
- Run history: `~/Library/Application Support/Inpaint Studio/runs/<id>/run.json` + live preview
  JPEGs (override with `INPAINT_STUDIO_DATA`), served at `/data/runs`, listed by
  `GET /api/runs`, removed by `DELETE /api/runs/{id}`.
