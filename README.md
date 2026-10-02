# Inpaint Studio

A small local web app for mask-based image editing with **Qwen-Image 2.1** through a running
**ComfyUI** (default `http://127.0.0.1:8188`).

1. **Mask** – load an image, type what to mask (SAM3), tune threshold / expand / invert and
   see the mask in seconds. Refine it with a paint/erase brush.
2. **Edit** – enter the prompt and parameters and run. Every sampling step is shown live and
   kept as a filmstrip; the result comes with a before/after slider, download and
   "use result as new input" for chained edits.

The app predicts the working size and warns (or auto-fixes) when it would exceed ~4096 latent
tokens, the point where the edit turns into gray noise on Apple Silicon.

## Batch (folder)
Open a folder or drop several images: they appear as a thumbnail grid and nothing starts on its
own. Go through them one by one (mask, refine, **Submit & next**; each image keeps its own mask),
or **Mask all** first (masks only, nothing queued), review them with ← → and submit them
together with **Submit all masked**. Or use **Auto-mask & submit all** (current mask settings; images where nothing is found are
marked and skipped) or **Submit all without mask**.

## Align (advanced)
For free edit + paste runs, **Align…** in the viewer lines the generated image up with the
original before pasting: Auto-align estimates shift and scale from the area outside the mask
(phase correlation), then fine-tune with the arrows (Shift = 5 px) and scale. Uncovered edges keep
the original. The aligned result is saved next to the run; the original result stays.

## Queue
"Run edit" adds a job to a server-side queue, so you can keep preparing the next edit. The
Queue panel shows each job (queued / running step x of y) with a cancel button; jobs keep running
when the page is reloaded or closed.

## Run history
Every finished run is stored in `data/runs/` (gitignored) and listed again after a reload,
including its step frames, before/after and the raw edit. Deleting a run there keeps the
images in the ComfyUI output folder.

## Output files
Per run in the ComfyUI output folder (`InpaintStudio/`):
- `<run>.png` – result, `<run>_raw.png` – raw edit before pasting (masked runs)
- `<run>/before.png`, `<run>/step_17_20.png`, `<run>/raw_step_17_20.png` (free edit + paste)

Steps are saved every N steps and/or for the last N steps, always including the final step.
ComfyUI's `_00001_` counter is removed by the server (output folder from the setup config, or
`COMFY_OUTPUT_DIR`).

## Requirements & setup
Only macOS on Apple Silicon (32 GB RAM recommended). On first start the UI shows a **setup page**
that finds an existing Comfy Desktop install or installs what is missing (pick steps, folders and
quantisation): ComfyUI (pinned zip from GitHub + its own uv venv), ComfyUI-GGUF (patched for
`qwen_image21`), and the models from Hugging Face (Qwen-Image 2.1 UC GGUF, `qwen3vl_8b_int8_convrot`,
Qwen-Image 2.1 VAE). Masking needs SAM3 (`sam3.1_multiplex_fp16`) and is optional: without it the
UI hides all mask tools and only edits whole images. Config: `~/Library/Application Support/Inpaint
Studio/config.json` (run history lives there too, in `runs/`).

The server starts ComfyUI headless on 8188 when nothing answers there yet and stops it on shutdown.

## Mac app
`macos/build-app.sh [path]` builds **Inpaint Studio.app** (default `~/Applications`), a stay-open
AppleScript applet with the app code bundled in `Contents/Resources/app`, so the .app alone can be
sent to someone. First start installs `uv` and the app's Python deps (venv in Application Support),
then starts the server and opens the UI. Quitting stops the server (and the ComfyUI it started) and
warns first if jobs are still running. The applet is only ad-hoc signed: on another Mac open it via
right-click → Open the first time.
Logs: `~/Library/Logs/InpaintStudio.log`, `~/Library/Logs/InpaintStudio-ComfyUI.log`.

## Run
```bash
./run.sh
```
Set `COMFY_URL` / `PORT` to override the defaults (see `.env.example`).

## Edit modes
- **Inpaint (mask only)** – only the masked area is re-generated.
- **Free edit + paste** – the whole image is edited, then only the masked area is pasted into
  the original (starting from the original latent, so it stays aligned). Optionally tells the
  model to keep everything else identical; the UI shows the raw edit and how much it differs
  from the original outside the mask. Try this when the model keeps redrawing the old content
  or the inpainted background looks out of context.
