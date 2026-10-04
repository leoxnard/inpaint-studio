<picture>
  <source media="(prefers-color-scheme: dark)" srcset="design/app-icon-dark.svg">
  <img src="design/app-icon.svg" alt="" width="96" height="96">
</picture>

# Inpaint Studio

A small local web app for mask-based image editing with **Qwen-Image 2.1** through a running
**ComfyUI** (default `http://127.0.0.1:8188`).

The UI has two views, switched in the header (the address keeps the view, e.g. `#runs/<id>`):

- **Create** – on the left you pick the task (edit a photo or generate a new one), the model,
  the image, the size and the area to change (whole image, masked inpaint, or free edit + paste).
  The middle shows the image with the mask; under it you find the mask (SAM3: type what to mask,
  tune threshold / refine / expand / invert) and touch it up with a paint/erase brush. On the
  right are the prompt and settings and the button that adds the run to the queue. You can keep
  working while the run waits or renders.
- **Runs** – the queue on the left (running run with progress, waiting runs with Remove), the
  selected run in the middle (live preview while it renders, then the result with a before/after
  slider, the saved steps and all results with filters) and its details and actions on the right
  (load its settings in Create, use the result as new input, post-processing, download, delete).

The app predicts the working size and warns (or auto-fixes) when it would exceed ~4096 latent
tokens, the point where the edit turns into gray noise on Apple Silicon.

## Reference images
Qwen-Image 2.1 (UC, official, Viggle Turbo) takes up to 3 extra images, Qwen-Image-Edit 2511 up to 2.
Add them under **Reference images** in Create and refer to them in the prompt: when you edit, your
image is image 1 and the references are image 2, 3 …; when you generate, they start at image 1
("Place the red apple from image 2 on the sand"). They are scaled like the main image and saved
with the run. Next to each reference, write in a few words what to take from it ("face");
after your prompt the model then gets "Replace the face in <image1> with the face from <image3>, in the place
and at the size of the face in <image1>.", plus a line that anything on or around it (a cap, hair, glasses)
stays, so
this part is replaced even when the prompt does not mention it. When you edit, it also gets a hidden
instruction that image 1 is the image to edit (framing and the rest stay) and that only what the
instruction asks for comes from the references. Qwen 2.1 names the images `<image1>`, `<image2>` …, Edit 2511
"Picture 1", "Picture 2", and the instruction uses these names. Without it, a vague prompt ("Show this
dog in a meadow") made the result take over the reference's framing; without the take text, "remove
the t-shirt" ignored a torso reference, a "take only: face" line next to it did not replace the face, a broad "copy its shape" note copied the reference's arm
pose too (a plain "replace the torso" order did the same: the reference torso came with its arms, so the
note now forbids added body parts and the order keeps place and size), and a "never for pose" note blocked
a "pose" take. You can change or empty the general instruction under **Advanced → Reference instruction**.
Click a reference to crop it: drag a rectangle around the part to use (e.g. the face). Only that part
goes to the model, and it is not scaled up, so a small crop also makes every step much faster.
Every run also writes `config.json` next to its images (`output/InpaintStudio/<run>/`) with all
settings, the exact text the encoder got and the ComfyUI graph. Each reference makes the run much slower: one extra image at 0.95 MP took the
6-step Turbo edit from about 1 to about 7 minutes on a 32 GB Mac, without gray noise.

## Remove watermarks and text
The checkbox under the prompt adds "Remove all watermarks, logos, captions and overlaid text from the
image and restore what is behind them." to the prompt (when generating: "The image has no watermarks,
logos, captions or overlaid text."). With a mask, only the masked area can change.

## Batch (folder)
Open a folder or drop several images: they appear as a thumbnail grid and nothing starts on its
own. Go through them one by one (mask, refine, **Submit and next**; each image keeps its own mask),
or open **Batch…** and use **Mask all** first (masks only, nothing queued), review them with ← → and
submit them together with **Submit all masked**. Or use **Auto-mask and submit all** (current mask
settings; images where nothing is found are marked and skipped) or **Submit all without mask**.
The **+** tile adds more images to an open batch.

## Post-processing
**Post-processing** in Runs collects every fix a run can get; tick the steps, check the preview, **Apply**.
Free edit + paste and whole-image edits: shift and scale (Auto-align estimates them by phase correlation,
then fine-tune with the arrows, Shift = 5 px), colours & exposure and local warp; paste runs also seamless
edges. All edits and upscales: film grain (with a mask only inside it). Uncovered edges keep the original;
the plain result always stays. The same options sit in Create under **Post-processing** and run when the
job is done (only the ones that fit the chosen area mode are shown).

## Queue
"Add edit to queue" puts a job into a server-side queue, so you can keep preparing the next
edit. The Runs view shows each job (running with step x of y and the time left, waiting with its
position); the Runs switch in the header shows how many are active. Jobs keep running when the
page is reloaded or closed, and the elapsed time keeps counting. The backend cannot reorder or pause the queue.
Runs have no numbers: a run is labelled with what it is doing ("Running · 1m 23s", "Waiting") or how long it took.

The ×1 … ×8 menu next to the queue button adds **variations**: the same run several times, each with its
own seed (random, or seed, seed + 1, …). In Runs the variations of one batch show as a strip under the
image. **Compare** (next to the result filters) picks two or more results (dashed tiles, click to toggle)
and shows them side by side (`web/compare.js`): **Detail** (default) is a grid where every tile shows the
same part of its image (scroll/pinch to zoom, drag to pan, all tiles follow); **Split** cuts one frame into fixed strips. Drag a label to change the order. Labels name
the model plus every setting that differs; results from different source images can be mixed.

If the server restarts while ComfyUI keeps running (e.g. Comfy Desktop), unfinished runs are followed
again (without live progress, ComfyUI only reports that to the original connection). A run that crashed
can be queued again with **Retry**; every run keeps a `job.json` (all parameters and the graph) for that.

## Run history
Every finished run is stored in `~/Library/Application Support/Inpaint Studio/runs/` (override with
`INPAINT_STUDIO_DATA`) and listed again after a reload, including its step frames, before/after
and the raw edit. Runs that crashed (OOM, ComfyUI error, server restart) stay listed (filter **Failed**);
runs you cancel are not kept. The × on a result removes it from the history without deleting any file (its run.json gets `hidden: true`); **Delete run** removes its run folder. Deleting a
run keeps the images in the ComfyUI output folder.

## Output files
Per run in the ComfyUI output folder (`InpaintStudio/`):
- `<run>.png` – result, `<run>_raw.png` – raw edit before pasting (masked runs)
- `<run>/before.png`, `<run>/step_17_20.png`, `<run>/raw_step_17_20.png` (free edit + paste)
- `<run>_full.png` – crop & stitch: the edited crop pasted into the full-size original (`<run>.png` is the crop)

Steps are saved every N steps and/or for the last N steps, always including the final step.
ComfyUI's `_00001_` counter is removed by the server (output folder from the setup config, or
`COMFY_OUTPUT_DIR`).

## Models & downloads
Model presets (`presets.py`) bundle a GGUF diffusion model (all quantisations that run on Apple
Silicon) with its text encoder and VAE: Qwen-Image 2.1 UC / official / Viggle Turbo (edit +
generate), Qwen-Image-Edit 2511 (edit, experimental), Qwen-Image 2512 (generate, experimental),
Z-Image Turbo (generate, edit = img2img). The **Download Center** installs or deletes them,
picks a quant in an LM Studio style selector (format, RAM fit, recommended for this Mac, downloaded, size). The model
picker sits in the topbar; UNet/CLIP/VAE can still be overridden in Advanced. The Edit card has
an **Edit | Generate** switch (generate = text to image, no input image) and prompt presets
(built-in + own, saved in the browser).

## Paste fixes & upscaling
Free edit + paste and whole-image runs can be fixed automatically (Post-processing in Create) or later in
Runs: match colours & exposure (smooth Lab offset field measured outside the mask; whole image: on the pixels
whose colour moved about as much as most, so the asked-for change is left alone), fix
local warp (DIS optical flow, OpenCV) and seamless edges (graph-cut seam, OpenCV), on top of shift/scale alignment.
Everything that is on (fixes and grain) is saved as one `<run>_fixed.png`, overwritten on every Apply. An optional upscaler from the Download Center
(UltraSharp / RealESRGAN in pixel space, or SeedVR2) upscales the result and saves `<run>_x2.png` / `_x4.png`.
SeedVR2 comes as 1.4B sharp (community distillation, needs a small pinned ComfyUI node), 3B, 7B and
7B sharp in int8 / fp16; its memory estimate assumes a 4 MP result.

The **Upscale** tab upscales images on their own (one image or a whole folder, like in Edit). The size is a
factor (1–4×), a target width in px (each image of a folder gets that width) or a rough file size in MB
(estimated from how well the original compresses as PNG, `prepare.size_for_megabytes`; usually within ±25 %). Upscalers come out clean and
lose the camera's grain, so by default the original's grain is added back at its original size
(`prepare.add_grain`, saved as `<run>_fixed.png`; the clean upscale stays as `<run>.png`, "Clean" in Runs).
The grain is measured only on flat areas (edges and texture are not grain), separately for three frequency
bands and six brightness bands, and only what the result lacks per band is added: a VAE's fine pixel pattern
does not count as grain, and grain that is strongest in the midtones stays that way. Brightness bands with few
flat pixels are pulled towards the overall value, and coarser bands are capped at what grain can have (white noise
blurred by 1 px), so picture detail that slipped into the flat areas is not added as blotchy noise. **Grain strength**
(0–200 %, in Runs → Post-processing; runs start with 80 %) scales it.
Any finished edit or upscale can also get the grain afterwards: Post-processing → Film grain in Runs
(`POST /api/runs/{id}/post`, measured on the run's original; untick it to go back). Compare sorts the picked results by model, then
parameter count and quantisation.
Pixel-space upscalers to download: UltraSharp (V1/V2), UltraMix Balanced, Remacri, NMKD Siax, RealESRGAN 2x/4x.

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
`macos/build-app.sh [path]` builds **Inpaint Studio.app** (default `~/Applications`), a small native
Swift app (`macos/InpaintStudio.swift`, one WKWebView window) with the app code bundled in
`Contents/Resources/app`, so the .app alone can be sent to someone. First start installs `uv` and the
app's Python deps (venv in Application Support), then starts the server and shows the UI in its own
window (no browser tab). Downloads go to ~/Downloads, View → Open in Browser opens the same UI in the
browser. Closing the window keeps the server running (click the Dock icon to get it back); quitting
stops the server (and the ComfyUI it started) and warns first if jobs are still running. Rebuilding
quits a running copy and opens the new one. The app is only ad-hoc signed: on another Mac open it via
right-click → Open the first time. The icon (light + dark, switches with the system appearance) comes
from `design/app-icon*.svg`; after changing them run `macos/make-icons.sh` (needs Xcode) and commit its outputs.
Logs: `~/Library/Logs/InpaintStudio.log`, `~/Library/Logs/InpaintStudio-ComfyUI.log`.

## Run
```bash
./run.sh
```
Set `COMFY_URL` / `PORT` to override the defaults (see `.env.example`).

## Edit modes
Chosen under **Area to change** in Create.
- **Whole image** – no mask, the prompt can change everything.
- **Masked area, inpaint** – only the masked area is re-generated.
- **Masked area, free edit + paste** – the whole image is edited, then only the masked area is pasted into
  the original (starting from the original latent, so it stays aligned). By default it tells the
  model to keep everything else identical (editable or off under Advanced → Keep-identical instruction); the UI shows the raw edit and how much it differs
  from the original outside the mask. Try this when the model keeps redrawing the old content
  or the inpainted background looks out of context.
- **Extend canvas** – outpainting: pick the new shape with the aspect tiles under Size and drag the image
  to where it should sit. Drag a corner handle to make the image smaller (down to ¼, the opposite corner
  stays), so there is new area on more sides; double-click resets. The new area is filled with stretched, blurred edge colours and inpainted
  (with a small overlap into the old image so the border blends). By default the model redraws the whole
  canvas and the new area is pasted around the old image (like free edit + paste), with a seamless edge
  and, with **Match colours at the border** (on by default), the colours matched on the old image. This gave
  clean borders where generating only the new area (`outpaint.method: "inpaint"`) left a blurred band.
  The fixed result is saved as `<run>_fixed.png`. **Erase** (under the stage) removes parts of the image so
  they are generated new as well; a hidden prompt line tells the model to replace the blurred fill.

**Edit around the mask only** (under the area cards, for masked modes) is crop & stitch: only a crop around
the mask goes to the model, scaled to the full working size, and the result is pasted back into the
original at its full resolution. Outside the mask the original pixels stay exactly the same. Use it for
small changes in large photos: a 20 MP photo otherwise comes back at ~1 MP. **Context** sets how much
around the mask goes along (more context = better fit, less detail). The stage shows the crop as a dashed box. The
edit comes out without the photo's grain; **Film grain** under Post-processing (on by default) measures the grain around
the mask (strength and how much it is the same in all colour channels) and adds matching noise inside it.

The mask canvas zooms with ⌘/Ctrl + scroll or a pinch (1–8×); scroll or Space + drag pans, `0` resets.

**LoRAs** (Advanced): up to 3 LoRA files from ComfyUI's `models/loras`, each with a strength; they are
applied to the diffusion model in this order. Download Center → LoRAs offers a picked set per model line (speed,
styles, camera angles, upscale; `presets.LORA_GROUPS`). Known LoRAs show their model line in the picker,
start with their suggested strength, and a hint warns when one does not fit the selected model.
