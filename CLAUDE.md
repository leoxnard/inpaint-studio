Brain note: ~/brain/Projects/inpaint-studio.md

# Inpaint Studio

Local UI on top of ComfyUI (127.0.0.1:8188). Three tasks: Edit (optional SAM3 mask, tune + brush/wand/bucket;
whole image / inpaint / free edit + paste / extend canvas), Generate (text to image) and Upscale (own task, upscale
models only; edits can still upscale their result). Single images or whole folders, model presets
(Qwen-Image 2.1 UC/official/Viggle Turbo, Edit 2511, 2512, Z-Image), live per-step previews, post-processing.
README.md is the user guide (tables + short bullets); keep it in sync when UI features change.
Runs on Leonard's Mac; the .app is self-contained so it can be shared. No deploy.

## Commands
- Run: `./run.sh` (http://127.0.0.1:7380), or the Mac app built by `macos/build-app.sh`
  (native Swift WKWebView window, `macos/InpaintStudio.swift`; a rebuild quits and reopens the running app; code bundled in the .app; the server itself starts ComfyUI headless if 8188 is free and stops it on
  shutdown; while it boots, `/api/status` → `boot` (phases read from its log, `installer.BOOT_STEPS`) drives the loading screen). First-run setup: `installer.py` + `web/setup.js`; data in `~/Library/Application Support/Inpaint Studio`
- Tests: `uv run pytest` (the queue integration test uses a tiny CPU-only graph against the
  running ComfyUI and is skipped when ComfyUI is down)

## Architecture
- `graphs.py` – size math (mirrors ImageScaleToTotalPixels / TextEncodeQwenImage21) and the
  ComfyUI API graph builders. Pure functions, unit-tested.
- `server.py` – FastAPI. Proxies uploads/views to ComfyUI, runs the mask graph synchronously.
  Edits are server-side jobs (`POST /api/jobs`): each job is submitted to ComfyUI at once and
  followed by a background task on its own ComfyUI ws (progress + binary latent previews via
  `extra_data.preview_method`). Browsers subscribe to `/ws/jobs`; jobs survive page reloads.
  Each run keeps `job.json` (params + graph): after a restart, runs ComfyUI still has are followed again by
  polling (`reattach_runs`), the rest become errors; `POST /api/runs/{id}/retry` queues one again.
  `/api/*`, `/data/*` and `/ws/jobs` only accept local Host/Origin (`local_origin`, extra hosts via
  `INPAINT_STUDIO_ALLOWED_HOSTS`).
- `prepare.py` – crop & stitch (crop around the mask before the run, `stitch` back into the original after
  it → `<run>_full.png`), outpaint padding (`pad`) and the grain (`add_grain`, global or only inside a mask; measured on flat pixels per frequency × brightness band, `grain_profile`, rebuilt band by band, `grain_strength` scales it). Pure functions, unit-tested; used by `create_job`.
- `align.py` – post-processing of a paste-mode free edit or a whole-image edit: shift/scale alignment (phase
  correlation), local warp fix (DIS optical flow), colour/exposure match (Lab offset field) and
  seamless edges (graph-cut seam in a band around the mask edge, paste only), measured outside the mask
  (whole image: on the pixels the edit did not change, `align.unchanged`; upscales: colours only, `align.match_colors_scaled`). Runs → Post-processing picks the steps
  (fixes + grain, `POST /api/runs/{id}/post`, `server.post_process`); the Create options run automatically after
  the job (`post_*` params). Everything that is on goes into one `<run>_fixed.png` (`run.fixed_url`), overwritten on every Apply and removed when all is off; `aligned.png` in the run dir holds the fixes alone.
- `imports.py` – own files from the Download Center (Your files): a model, LoRA, upscaler or other model file on the
  Mac is symlinked into `models/<folder>` and registered as preset / component (`imports.apply`, `imports.json` in
  App Support); Remove deletes only the link. `POST /api/imports/pick` opens the macOS file dialog via osascript.
- `presets.py` – model presets (files, quants, sizes, defaults), components (encoders, VAEs,
  SAM3, upscalers, Viggle node, LoRAs with their `families`, grouped by `LORA_GROUPS`) and the RAM-fit estimate. `installer.py` – setup and the download queue (more items can be queued while one runs) and
  the headless ComfyUI process.
- `web/` – vanilla HTML/JS/CSS, no build step. Two views in one page: Create (`#createView`) and Runs
  (`#runsView`), switched by a hash router (`#create`, `#runs/<id>`). Hidden `#mode`, `#aspect`, `#viewRaw`
  stay the source of truth; the Area cards, aspect tiles and Pasted/Raw switch only write into them.
  Design tokens (Ollama style, see `design/`) are CSS variables at the top of `styles.css`. `compare.js` renders the
  multi-run Compare view (detail grid with synced zoom/pan, split strips); `app.js` handles picking and labels.
- Testing vs. using: Claude tests changes in the Browser pane against the dev server (launch config
  `inpaint-studio-dev`, port 7381, code from the repo); Leonard uses the installed app (7380, bundled code).
  Never test in or quit the app; only rebuild it at the end (`GET :7380/api/jobs` empty first).

## Rules
- Gray-noise limit: on MPS the edit breaks at >= 4096 latent tokens for target or reference
  (pixels/16 per side). `graphs.TOKEN_LIMIT`; the UI auto-fixes size by default.
- Remove background (`remove_bg`, checkbox under the prompt): Qwen-Image 2.1 / Turbo, whole image only. Adds
  `graphs.REMOVE_BG_PROMPT` (ComfyUI's `image_qwen_image_2_1_background_removal` template) after the prompt; the 2.1 VAE
  decodes RGBA, so `<run>.png` is transparent. Upscale, post-processing and the keep note are off (they work in RGB).
- Extra reference images (`refs` in a job): `graphs.MAX_REFS` per family (Qwen 2.1: 3, Edit 2511: 2), mirrored
  in `web/app.js` `MAX_REFS`. The edited image stays image 1 (its size sets the latent). `ref_takes` (one
  text per ref, turned into "Replace the X in <image1> with the X from <image2>." after the prompt) and `graphs.REF_NOTE` name the images the encoder's way (`<image2>` / "Picture 2").
  `ref_crops` ({x,y,w,h} px per ref) → `ImageCrop`, scaled to at most its own size (fewer tokens).
- Model, text encoder and VAE must match: the presets encode the pairs. fp8 text encoders
  (Qwen2.5-VL for 2511/2512) run on the CPU (`device: cpu`), MPS cannot do fp8.
- Encoder resolution is matched to the working size by default (`graphs.matching_resolution`):
  a different reference size shifts/scales the free edit.
- Uploads go to ComfyUI `input/inpaint-studio/`. Results: `output/InpaintStudio/<run>.png`,
  `<run>_raw.png`, `<run>_x2.png`, `<run>_full.png` (crop & stitch), `<run>_fixed.png` holds all post-processing (fixes and grain; older runs may still have `<run>_grain.png`); `<run>/before.png` and
  `<run>/step_NN_TOTAL.png` (+ `raw_step_…`), `<run>/config.json` (all params, encoder prompt, graph). The server strips ComfyUI's `_00001_` counter.
- Run history: `~/Library/Application Support/Inpaint Studio/runs/<id>/run.json` + live preview
  JPEGs (override with `INPAINT_STUDIO_DATA`), served at `/data/runs`, listed by
  `GET /api/runs`, removed by `DELETE /api/runs/{id}`. × on a result tile or Select → remove → hide hides a run
  (`POST …/hide`, files kept); `GET /api/runs?hidden=1` + `POST …/restore` bring it back (Results → Removed).
  Results → Select works on picked runs: compare, post-process all, use as input, download, remove.
- Jobs: `finish_job` runs once per job; a silent (30 s) or closed ComfyUI socket falls back to polling (`follow_job`,
  5 failed polls end the run); deleting a queued/running run is a 409; `POST /api/jobs` input is checked (`JobCheck`).
- Results tiles load `/api/thumb?src=<run image url>` (384 px, cached in App Support `thumbs/`); hover shows the
  original on the right half. `GET /api/runs/{id}/comfyui.png` = result with the API graph as PNG `prompt` chunk.
- Prompt history: `prompt_history.json` in App Support (edit/generate prompts + SAM3 mask texts, 50 each), written by
  `create_job` / `/api/mask`, read by `GET /api/prompt-history`. Top bar: CPU / GPU / RAM meters (`sysload.py`: the app part = this server + ComfyUI, RAM as phys footprint; GPU only as a whole), **Free** (`POST /api/comfy/free`) is enabled only while `MODELS["loaded"]` (set by a finished run or mask, cleared by Free or a new ComfyUI pid).
- Guidance (Generate, `graphs.apply_control`): Z-Image Fun ControlNet Union (`ZImageFunControlnet`) or Qwen-Image 2512
  DiffSynth patches (`QwenImageDiffsynthControlnet`) via `ModelPatchLoader` (models/model_patches); depth map from
  Depth Anything 3 small (models/geometry_estimation), edges from core `Canny`. The map is saved as `<run>/control.png`.
- Restarting the dev server stops a ComfyUI it started itself (and every run on it); check `:8188` after a restart.
- An edit with Upscale on runs as two results: the edit, then an upscale run of its (post-processed) result
  (`params.then_upscale` → `follow_up_upscale`, the upscale run has `upscale_of`).
