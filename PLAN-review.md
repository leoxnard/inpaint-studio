# Plan: review fixes + features (2026-10-04)

Scope (Leonard): review items 1–5, 6 (shortcuts only, undo stays at 5), 7, 8, 9, 10, 11 (+ mask prompt
history), 12, 13. Work in milestones; each one ends with checks + a test on the dev server and its own commits.
Test only on the dev server (7381, Browser pane), test image = Desktop turf-house screenshot. Never touch the
installed app until the very end (rebuild only when `GET :7380/api/jobs` is empty).

## M1 – Job lifecycle (backend)  · server.py, tests/
1. `finish_job` runs once per job (`job["finished"]` flag) → cancel/complete race can't finish twice. `cancel_job`
   awaits the cancelled task.
2. `delete_run`: 409 while the run is in `JOBS`; `shutil.rmtree` instead of unlink loop.
3. `run_job`: `cws.recv()` wrapped in `wait_for(…, 30)`; on timeout check `/queue` + `/history`: prompt gone →
   error "Prompt disappeared from ComfyUI", finished → `complete_run`. Socket closed → fall back to `follow_job`
   polling instead of erroring.
4. `follow_job`: tolerate up to 5 failed polls; `wait_history` raises on `execution_interrupted`; `complete_run`
   errors when `out_result` is missing. `reattach_runs` waits up to 30 s for ComfyUI after start.

**Check:** `uv run pytest` with new tests (fake ComfyUI via `httpx.MockTransport`): finish once, delete 409,
interrupted history → cancelled, missing result → error. Dev server: queue a run, cancel while running and while
queued; delete a running run → refused.

## M2 – Event loop + validation (backend)  · server.py, prepare.py, align.py
1. Item 7: `crop_input`, `outpaint_input`, `stitch_result`, `load_input`, `fix_image` work in one
   `asyncio.to_thread` call each; `fix_image` caches the unaligned baseline per run, float32, unique preview file
   name. `system_ram()` once at import.
2. Item 8: pydantic model for `POST /api/jobs` (required fields, `gt=0` on sizes/steps), `Field(gt=0)` on
   `SizeReq`/`UpscaleReq`; `/api/upload` checks the image with PIL → 400 with a readable message.

**Check:** pytest (validation → 400, non-image upload → 400). Dev server: crop & stitch run on the test image
while a second tab shows live previews without stalls; post-processing sliders still update.

## M3 – Frontend robustness, thumbnails, shortcuts  · web/app.js, server.py
1. Item 1: after a ws `snapshot`, re-point `state.run` to the new job object (or update jobs in place).
2. Item 5: `GET /api/runs/{id}/thumb` → ~256 px JPEG, cached on disk next to `run.json`,
   `Cache-Control: immutable`. Tiles use it; `renderHistory` keys tiles by run id and only adds/removes/patches
   classes instead of rebuilding.
   Hover compare (Results grid tiles in Runs only, not the Compare view): while the mouse is over a tile, the
   left half shows the result and the right half the original (`thumb?which=before`). Runs without an original
   show no split. Nothing changes without hover.
3. Item 6: zoom/pan keys also work when a button has focus. New: B paint, E erase, W wand, G bucket, `[` `]`
   brush size, X invert, ⌘↵ add to queue. Short hint in the Touch up header tooltip. Undo stays at 5.

**Check:** Browser pane: restart dev server during a run while viewing another run → viewer resumes. Runs grid
loads thumbs (network tab: small JPEGs). Every shortcut once, also right after clicking a button.

## M4 – Small features  · server.py, web/
1. Item 9 "Open in ComfyUI": `GET /api/runs/{id}/download?comfy=1` writes the stored API graph as PNG `prompt`
   tEXt chunk (ComfyUI loads API-format PNGs by drag & drop). Button "Download for ComfyUI" in Runs → Actions.
2. Item 10 memory: `/api/status` adds RAM/VRAM from ComfyUI `/system_stats`; top bar shows it next to "ComfyUI
   running"; "Free memory" button → `POST /free {unload_models, free_memory}`, disabled while a job runs.
3. Item 11 prompt history: server-side `prompt_history.json` in the data dir (shared by app and dev server), two
   lists: edit/generate prompts and mask prompts (SAM3 text), last 50 each, deduped, newest first. Saved when a
   job is queued / a mask is computed. UI: ↑/↓ in an empty or unchanged field cycles, plus a small history
   dropdown next to Preset / next to the mask field.

**Check:** pytest for the PNG chunk + history dedupe. Dev server: drag a downloaded PNG into ComfyUI
(127.0.0.1:8188) → graph appears; Free memory → RAM drops; history survives a server restart.

## M5 – Create layout  · web/index.html, web/styles.css, web/app.js
Item 13: wider left column (~300 px at ≥1280), task tabs no longer cramped, sections (Reference images, Size,
Area) collapsible with state kept in localStorage; Image + Area open by default. No layout shift elsewhere.

**Check:** `/verify-ui` at 1440, 1280, 1024 and phone width, light + dark; full flow: load image → mask → queue.

## M6 – Control guidance (ControlNet)  · presets.py, installer.py, graphs.py, web/
Available in this ComfyUI: `ModelPatchLoader` + `QwenImageDiffsynthControlnet` (Qwen-Image DiffSynth patches:
canny/depth/inpaint) and `ZImageFunControlnet` (Z-Image Fun ControlNet Union); preprocessors `Canny` and
`DA3Inference`/`DA3Render` (Depth Anything 3) are core nodes.
1. Research gate first (scout): exact HF files + sha256 for Z-Image Fun ControlNet Union and the Qwen-Image
   DiffSynth patches, which presets they work with (2512 likely, 2.1 / Edit 2511 unknown), DA3 model file.
   Only families with a confirmed patch get the option.
2. presets: components `kind: control` with `families`; Download Center group "Control".
3. graphs: optional control chain: source image → Canny or DA3 depth (or an uploaded control image) →
   patch node on the model, `strength`, `end_percent`.
4. UI: Generate task (and Edit if supported): "Guidance" Off / Edges / Depth / Own image, image picker,
   strength slider; depth/edge preview thumbnail.

**Check:** graph unit tests; one real Z-Image generate with depth from the test image → same layout, new
content; same with edges.

## End
Run all checks, update CLAUDE.md (new endpoints, history file, control chain) and the brain note, rebuild the
app if `:7380/api/jobs` is empty.
