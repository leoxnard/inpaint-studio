<picture>
  <source media="(prefers-color-scheme: dark)" srcset="design/app-icon-dark.svg">
  <img src="design/app-icon.svg" alt="" width="96" height="96">
</picture>

# Inpaint Studio

A local Mac app for editing, generating and upscaling images with **Qwen-Image** models.
It runs on top of **ComfyUI** (`http://127.0.0.1:8188`) and starts it for you if it is not running.

- **Edit** a photo: whole image, only a masked area, or extend the canvas
- **Generate** an image from text
- **Upscale** single images or whole folders
- Masks with **SAM3** (type what to mask), then touch them up with a brush
- Live preview of every step, a server-side queue and a full run history

New to AI images? **How it works** in the top bar opens a visual guide (`/guide.html`): the parts, the three
model files, the node graph, the sampling loop (real frames of a 20-step run), quantisations, a first edit and a
**Download** button for the recommended model (Qwen-Image 2.1 UC Q4_K_M). On a fresh install it opens once instead
of the app; after that only from the top bar.

## Contents

- [Quick start](#quick-start)
- [The two views](#the-two-views)
- [Edit modes](#edit-modes)
- [Prompt options](#prompt-options)
- [Reference images](#reference-images)
- [Folders (batch)](#folders-batch)
- [Upscaling](#upscaling)
- [Post-processing](#post-processing)
- [Queue, variations and compare](#queue-variations-and-compare)
- [Models and Download Center](#models-and-download-center)
- [Files and folders](#files-and-folders)
- [Mac app](#mac-app)
- [Development](#development)

## Quick start

**Requirements:** macOS on Apple Silicon, 32 GB RAM recommended.

1. Get the app. The easiest way is this line in Terminal (it also updates an existing install):

   ```bash
   curl -fsSL https://raw.githubusercontent.com/leoxnard/inpaint-studio/main/install.sh | bash
   ```

   Or download `InpaintStudio.dmg` from the [latest release](https://github.com/leoxnard/inpaint-studio/releases/latest).
   The app is not notarized, so macOS blocks the first start: open it, click **Done**, then
   **System Settings → Privacy & Security → Open Anyway**. You can also build it yourself with
   `macos/build-app.sh` (see [Mac app](#mac-app)) or run from source with `./run.sh`.
2. On the first start a **setup page** opens. It finds an existing Comfy Desktop install or installs what is
   missing. You can pick the steps, folders and quantisation:
   - ComfyUI (pinned zip from GitHub, with its own uv venv)
   - ComfyUI-GGUF (patched for `qwen_image21`)
   - Qwen-Image 2.1 UC (GGUF), its text encoder `qwen3vl_8b_int8_convrot` and the 2.1 VAE
   - optional: SAM3 (`sam3.1_multiplex_fp16`) for masks. Without it the mask tools are hidden and you
     can only edit whole images.
3. While ComfyUI boots, a loading screen shows its progress. Then pick an image, write a prompt and press
   **Add edit to queue**.

The UI opens at `http://127.0.0.1:7380`.

## The two views

The header switches between them. The address keeps the view, so a reload stays where you were
(`#create`, `#runs/<id>`).

| View | Left | Middle | Right |
|---|---|---|---|
| **Create** | Task (Edit / Generate / Upscale), image, size, area to change, reference images | Image with the mask, mask tools under it | Prompt, settings, **Add edit to queue** |
| **Runs** | Queue (running and waiting runs) | Live preview, then the result with a before/after slider, saved steps and all results | Details and actions of the selected run |

You can keep working in Create while runs wait or render.

**Actions on a run:** load its settings in Create, use the shown image as new input, post-processing,
download, retry (failed runs), delete.

## Edit modes

Chosen under **Area to change** in Create.

| Mode | What happens | Good for |
|---|---|---|
| **Whole image** | No mask, the prompt can change everything | Global changes, style, removing the background |
| **Masked area, inpaint** | Only the masked area is generated again | Replacing or removing one object |
| **Masked area, free edit + paste** | The whole image is edited, then only the masked area is pasted into the original | When inpaint keeps redrawing the old content or the new part looks out of context |
| **Extend canvas** | Outpainting: the image gets a new shape and the new area is filled | Wider or taller crops |

**Free edit + paste**
- Starts from the original latent, so the result stays aligned.
- By default the model is told to keep everything else identical (edit or turn off under
  **Advanced → Keep-identical instruction**).
- The UI shows the raw edit and how much it differs from the original outside the mask.

**Extend canvas**
- Pick the new shape with the aspect tiles under **Size** and drag the image to where it should sit.
- Drag a corner handle to make the image smaller (down to ¼) so there is new space on more sides.
  Double-click resets.
- **Erase** (under the image) removes parts so they are generated new as well.
- The model redraws the whole canvas, then the new area is pasted around the old image.
  **Match colours at the border** (on by default) fixes colour steps at the edge.

**Edit around the mask only** (crop & stitch, for masked modes)
- Only a crop around the mask goes to the model, at the full working size. The result is pasted back
  into the original at full resolution, the pixels outside the mask stay the same.
- Use it for small changes in big photos. Without it a 20 MP photo comes back at about 1 MP.
- **Context** sets how much around the mask goes along: more context fits better, less context gives more detail.

**Mask tools**
- **Find mask:** type what to mask (SAM3), then tune threshold, refine, expand, feather or invert.
- **Touch up:** Paint, Erase, Wand (similar colours), Bucket (fill an outline), plus Undo, Clear and Fill.
- Zoom with ⌘/Ctrl + scroll or pinch (1–8×). Scroll or Space + drag pans, `0` resets.

**Size limit:** on Apple Silicon the edit turns into gray noise above about 4096 latent tokens. The app
predicts the working size and lowers it automatically (**Auto-fix size**, on by default).

## Prompt options

Checkboxes under the prompt:

| Option | Effect |
|---|---|
| **Remove watermarks and text** | Adds an instruction to remove watermarks, logos, captions and text (when generating: to create none). With a mask, only the masked area changes. |
| **Keep everything else identical** | Whole image only. Adds the keep-identical instruction after your prompt. |
| **Remove background** | Qwen-Image 2.1 / Turbo, whole image only. Uses ComfyUI's background removal template, the result is a transparent PNG. Upscale and post-processing are off for these runs. |

**Prompt presets:** built-in ones plus your own, saved in the browser.

**Advanced** holds the negative prompt, sampler, scheduler, steps, CFG, seed, the model files
(UNet / CLIP / VAE) and up to 3 **LoRAs** with a strength each.

## Reference images

*Experimental.* Extra images the model can take things from.

- Up to **3** for Qwen-Image 2.1 (UC, official, Turbo), up to **2** for Qwen-Image-Edit 2511.
- Numbering in the prompt: when editing, your image is image 1 and the references are image 2, 3 …;
  when generating they start at image 1. Example: "Place the red apple from image 2 on the sand".
- Next to each reference, write in a few words what to take from it (e.g. "face"). The app then adds
  "Replace the face in image 1 with the face from image 2", and keeps place and size of the part in image 1.
- **Crop:** click a reference and drag a box around the part you need. Only that part goes to the model
  and it is not scaled up, so a small crop is also much faster.
- The general instruction can be changed under **Advanced → Reference instruction**.
- References are slow: one extra image at 0.95 MP took a 6-step Turbo edit from about 1 to about
  7 minutes on a 32 GB Mac.

## Folders (batch)

Click **Open folder…** or drop several images. They show up as a thumbnail grid and nothing starts on its own.

- Go through them with ← →. Each image keeps its own mask.
- **Add edit to queue** queues all open images, **Only this image** just the current one.
- **Skip** leaves an image out of the batch.
- **Mask all** computes masks for every image (nothing is queued), so you can check them first.
- **Submit all masked**, **Auto-mask and submit all** (images without a match are skipped) or
  **Submit all without mask**.
- The **+** tile adds more images (also to a single open image, which then becomes a batch). Hover a tile and click **−** to remove that image. **Clear batch** closes the folder.

## Upscaling

The **Upscale** task upscales images on their own (one image or a whole folder).

**Target size**, one of:
- a factor (1–4×)
- a long side in px (presets from Full HD 1920 to 8K 7680)
- a rough file size in MB (estimated from how well the original compresses, usually within ±25 %)

**Upscalers** (from the Download Center):
- Pixel space: UltraSharp V1/V2, UltraMix Balanced, Remacri, NMKD Siax, RealESRGAN 2x/4x
- **SeedVR2:** 1.4B sharp, 3B, 7B, 7B sharp in int8 or fp16. The app warns when a run needs more
  memory than the Mac has and shows the largest factor that fits.

**Grain:** upscalers come out clean and lose the camera's grain. **Keep the original's grain** (on by
default) adds it back. The clean upscale stays as the result (`<run>.png`, "Clean" in Runs), the
grained one is saved as `<run>_fixed.png`.

Edits can also upscale their own result (choose an upscaler in the run settings), saved as `<run>_x2.png` / `_x4.png`.

## Post-processing

**Runs → Post-processing** has every fix a run can get. Tick the steps, check the preview, press **Apply**.
The same options are in Create and run automatically when the job is done.

| Fix | Free edit + paste | Whole image | Upscale | Other edits |
|---|:-:|:-:|:-:|:-:|
| Shift & scale (Auto-align, then the arrows, Shift = 5 px) | ✓ | ✓ | | |
| Colours & exposure | ✓ | ✓ | ✓ | |
| Local warp | ✓ | ✓ | | |
| Clean edges at the mask | ✓ | | | |
| Film grain (with strength 0–200 %) | ✓ | ✓ | ✓ | ✓ |

- Everything that is on goes into one `<run>_fixed.png`, which is overwritten on every Apply.
  The plain result always stays.
- With a mask, grain is only added inside it.

<details>
<summary>How the fixes work</summary>

- **Shift & scale:** phase correlation between result and original.
- **Colours & exposure:** a smooth Lab offset field, measured outside the mask. For whole-image edits
  it is measured on the pixels the edit did not change, so the asked-for change stays. For upscales it
  is measured at the original's size and applied at full size.
- **Local warp:** DIS optical flow (OpenCV).
- **Clean edges:** a graph-cut seam in a band around the mask edge (OpenCV).
- **Film grain:** measured only on flat areas of the original, for three frequency bands and six
  brightness bands. Only what the result lacks per band is added, so the VAE's fine pattern does not
  count as grain and the grain stays strongest where it was. Bands with few flat pixels are pulled
  towards the overall value and coarse bands are capped, so picture detail is not added as blotchy noise.

</details>

## Queue, variations and compare

**Queue**
- **Add edit to queue** sends a job to a queue on the server, so you can prepare the next one right away.
- Runs shows each job with step x of y and the time left. The Runs switch in the header shows how many are active.
- Jobs keep running when you reload or close the page. The queue cannot be reordered or paused.
- If the server restarts while ComfyUI keeps running, unfinished runs are followed again (without live
  progress). A crashed run can be queued again with **Retry**.
- Shortcuts in Create: **B** paint, **E** erase, **W** wand, **G** bucket, **[ ]** brush size, **⌘↵** add to queue.
- **Recent…** above the prompt and the mask field remember your last prompts and mask texts.
- Hover a result tile to see the original on its right half. **Download for ComfyUI** saves the result with its
  workflow inside: drop it onto ComfyUI to open the exact graph.

**Variations:** the ×1 … ×8 menu next to the queue button runs the same edit several times, each with
its own seed (random, or seed, seed + 1, …). They show as a strip under the image in Runs.

**Select:** next to the result filters. Click tiles to pick them, Shift+click picks a range,
**All** / **None** work on the current filter. The icons then work on all picked results:
- compare (two or more)
- post-process (every fix that fits each run, plus grain)
- use as input (a new batch in Create)
- download
- remove: hide from the results (files kept, see filter **Removed**) or delete the files

The × on a single result tile hides just that one.

**Compare**
- **Detail** (default): a grid where every tile shows the same part. Scroll or pinch to zoom, drag to pan, all tiles follow.
- **Split:** one frame cut into strips, the borders can be dragged.
- Labels show the model and every setting that differs. Drag a label to change the order.

## Models and Download Center

The model picker is in the top bar. The **Download Center** (top bar; while it is open the button reads **Back to app**) installs and deletes models, upscalers and LoRAs.
For each model you pick a quantisation in an LM Studio style list (format, RAM fit, recommended for
this Mac, downloaded, size). Q4 (Q4_K_M where there is one) is preselected; models without a Q4 file get the largest
quantisation that fits. A bar at the top jumps between the sections (Components, Models, Upscalers, Control,
LoRAs, Your files).

| Model | Edit | Generate |
|---|:-:|:-:|
| Qwen-Image 2.1 UC / official / Viggle Turbo | ✓ | ✓ |
| Qwen-Image-Edit 2511 (experimental) | ✓ | |
| Qwen-Image 2512 (experimental) | | ✓ |
| Z-Image Turbo | img2img | ✓ |

- Each preset brings the matching text encoder and VAE (`presets.py`).
- **LoRAs:** a picked set per model line (speed, styles, camera angles, upscale). The picker shows which
  model line a LoRA is for, starts with its suggested strength and warns when it does not fit.
- **Upscalers** and **Control** (guidance patches, Depth Anything) have their own sections.
- **Your files:** import a model, LoRA, upscaler or other model file that is already on your Mac. It is
  linked into the models folder, not copied, and **Remove** deletes only the link.
- **Free memory** in the top bar unloads ComfyUI's models; next to it you see how much RAM is free.

## Guidance (Generate)

With Z-Image or Qwen-Image 2512, **Guidance** in Create keeps the layout of another image while generating:
**Edges** (Canny) or **Depth** (Depth Anything 3). You can also give your own edge or depth map. The map is
saved with the run.

## Files and folders

**Results** (ComfyUI output folder, `InpaintStudio/`):

| File | What |
|---|---|
| `<run>.png` | The result (for crop & stitch: the crop) |
| `<run>_raw.png` | Raw edit before pasting (masked runs) |
| `<run>_full.png` | Crop & stitch: the crop pasted into the full original |
| `<run>_x2.png` / `_x4.png` | Upscaled result |
| `<run>_fixed.png` | Result with all post-processing |
| `<run>/before.png` | The input |
| `<run>/step_17_20.png`, `raw_step_…` | Saved steps (every N steps and/or the last N) |
| `<run>/config.json` | All settings, the exact text the encoder got and the ComfyUI graph |

**App data** (`~/Library/Application Support/Inpaint Studio/`):
- `config.json`: setup config
- `runs/<id>/`: run history (`run.json`, `job.json`, live previews). Failed runs stay listed (filter
  **Failed**), cancelled runs are not kept. **Delete run** removes this folder but keeps the images in
  the ComfyUI output folder.

**Logs:** `~/Library/Logs/InpaintStudio.log` and `~/Library/Logs/InpaintStudio-ComfyUI.log`.

## Mac app

```bash
macos/build-app.sh            # builds ~/Applications/Inpaint Studio.app
macos/build-app.sh /some/dir  # or somewhere else
```

- A small native Swift app (`macos/InpaintStudio.swift`) with one window. The app code is bundled
  inside, so you can send the .app to someone else.
- First start installs `uv` and the Python dependencies (venv in Application Support).
- Closing the window keeps the server running (click the Dock icon to get it back). Quitting stops the
  server and the ComfyUI it started, and warns first if jobs are still running.
- **View → Open in Browser** opens the same UI in your browser. Downloads go to `~/Downloads`.
- Rebuilding quits a running copy and opens the new one.
- The app is only ad-hoc signed. Installed with `install.sh` or built yourself, it opens without a warning
  (curl and git do not set the quarantine flag). A downloaded DMG needs **Open Anyway** once (see
  [Quick start](#quick-start)).
- Release: `macos/release.sh 1.1.0` builds `dist/InpaintStudio.zip` (for `install.sh`) and
  `dist/InpaintStudio.dmg` and uploads both as GitHub release `v1.1.0`. `--dry-run` only builds.
- Icon: `design/app-icon*.svg` (light + dark). After changing them run `macos/make-icons.sh` (needs Xcode)
  and commit its outputs.

## Development

```bash
./run.sh        # serves the UI on http://127.0.0.1:7380
uv run pytest   # tests (the queue test needs a running ComfyUI, otherwise it is skipped)
```

**Environment variables** (see `.env.example`):

| Variable | Default |
|---|---|
| `COMFY_URL` | `http://127.0.0.1:8188` |
| `PORT` | `7380` |
| `COMFY_OUTPUT_DIR` | from the setup config |
| `INPAINT_STUDIO_DATA` | `~/Library/Application Support/Inpaint Studio` |
| `INPAINT_STUDIO_ALLOWED_HOSTS` | empty: only local hosts may call the API. Comma-separated extra hosts. |

**Code layout**

| File | Role |
|---|---|
| `server.py` | FastAPI server: jobs, queue, run history, ComfyUI proxy |
| `graphs.py` | Size math and the ComfyUI graph builders |
| `prepare.py` | Crop & stitch, outpaint padding, film grain |
| `align.py` | Post-processing fixes (align, colours, warp, edges) |
| `presets.py` | Model presets, components, LoRAs, RAM-fit estimate |
| `installer.py` | Setup, download queue, headless ComfyUI |
| `web/` | Plain HTML/JS/CSS, no build step |
| `macos/` | Swift app and build scripts |
