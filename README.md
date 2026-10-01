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

## Requirements
- ComfyUI with ComfyUI-GGUF, the SAM3 checkpoint `sam3.1_multiplex_fp16.safetensors`,
  a Qwen-Image 2.1 model, `qwen3vl_8b_*` text encoder and the Qwen-Image 2.1 VAE.
- [uv](https://docs.astral.sh/uv/)

## Mac app
`macos/build-app.sh` builds **Inpaint Studio.app** into `~/Applications` (a stay-open AppleScript
applet, source in `macos/InpaintStudio.applescript`). Opening it starts the server and the UI;
quitting it (⌘Q / Dock) stops the server and warns first if jobs are still running.
Server log: `~/Library/Logs/InpaintStudio.log`.

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
