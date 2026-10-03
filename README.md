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
  (load its settings in Create, use the result as new input, align, download, delete).

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

## Align (advanced)
For free edit + paste runs, **Align to original** in Runs lines the generated image up with the
original before pasting: Auto-align estimates shift and scale from the area outside the mask
(phase correlation), then fine-tune with the arrows (Shift = 5 px) and scale. Uncovered edges keep
the original. The aligned result is saved next to the run; the original result stays.

## Queue
"Add edit to queue" puts a job into a server-side queue, so you can keep preparing the next
edit. The Runs view shows each job (running with step x of y and the time left, waiting with its
position); the Runs switch in the header shows how many are active. Jobs keep running when the
page is reloaded or closed. The backend cannot reorder or pause the queue.

## Run history
Every finished run is stored in `~/Library/Application Support/Inpaint Studio/runs/` (override with
`INPAINT_STUDIO_DATA`) and listed again after a reload, including its step frames, before/after
and the raw edit. Failed runs show up in the results only until the page is reloaded. The × on a result removes it from the history without deleting any file (its run.json gets `hidden: true`); **Delete run** removes its run folder. Deleting a
run keeps the images in the ComfyUI output folder. Runs are numbered by creation time ("Run 3"),
so the numbers shift when you delete an older run.

## Output files
Per run in the ComfyUI output folder (`InpaintStudio/`):
- `<run>.png` – result, `<run>_raw.png` – raw edit before pasting (masked runs)
- `<run>/before.png`, `<run>/step_17_20.png`, `<run>/raw_step_17_20.png` (free edit + paste)

Steps are saved every N steps and/or for the last N steps, always including the final step.
ComfyUI's `_00001_` counter is removed by the server (output folder from the setup config, or
`COMFY_OUTPUT_DIR`).

## Models & downloads
Model presets (`presets.py`) bundle a GGUF diffusion model (all quantisations that run on Apple
Silicon) with its text encoder and VAE: Qwen-Image 2.1 UC / official / Viggle Turbo (edit +
generate), Qwen-Image-Edit 2511 (edit, experimental), Qwen-Image 2512 (generate, experimental),
Z-Image Turbo (generate, edit = img2img). The **Downloads** page installs or deletes them,
shows a RAM-fit estimate per quant (like LM Studio) and recommends a quant for this Mac. The model
picker sits in the topbar; UNet/CLIP/VAE can still be overridden in Advanced. The Edit card has
an **Edit | Generate** switch (generate = text to image, no input image) and prompt presets
(built-in + own, saved in the browser).

## Paste fixes & upscaling
Free edit + paste runs can be fixed automatically after pasting (Edit card) or later via
**Adjust…**: match colours & exposure (smooth Lab offset field measured outside the mask), fix
local warp (DIS optical flow, OpenCV) and seamless edges (graph-cut seam, OpenCV), on top of shift/scale alignment.
The fixed result is saved as `<run>_fixed.png`. An optional upscaler (UltraSharp / RealESRGAN,
from the Downloads page) upscales the result in pixel space and saves `<run>_x2.png` / `_x4.png`.

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
right-click → Open the first time.
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
