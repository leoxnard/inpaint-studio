# Adding a model

A checklist for adding a new diffusion model (or a new variant of one) to Inpaint Studio. Go through every step in
order. A step that does not apply is skipped on purpose, not forgotten: write down why in the commit message.

The FLUX.2 klein commit is a complete worked example (family, presets, uncensored twin, LoRA group, alternatives).

---

## 1. Research before touching code

Answer all of these first. If one answer is unknown, find it out; do not guess.

| Question | How to check |
|---|---|
| Is it really an image model (text to image / edit)? | Read the model card and the safetensors header (`__metadata__`, tensor names). Video, robotics or "action" models do not fit. |
| Which parts does it need? | Almost always three: **diffusion model**, **text encoder**, **VAE**. Write down the exact file for each. |
| Does ComfyUI support it? | `curl -s 127.0.0.1:8188/object_info` and look for its loader / latent / scheduler nodes and the `CLIPLoader` `type` value (e.g. `flux2`, `lumina2`, `qwen_image`). Update ComfyUI if missing. |
| Which formats run on Apple Silicon? | GGUF, BF16/F16 safetensors, int8 convrot. **No fp8, no fp4/NVFP4, no MLX** (MPS cannot run them). An fp8 text encoder only works with `device: cpu` (`graphs.CPU_TEXT_ENCODER`). |
| Is a repo gated? | `curl -s https://huggingface.co/api/models/<repo> \| python3 -c "import json,sys;print(json.load(sys.stdin)['gated'])"`. The app downloads without a Hugging Face account, so a gated file **cannot** be a normal download (see step 3, `gated`). Look for a non-gated mirror first (Comfy-Org, unsloth, city96 …) and check it with `curl -I -L <resolve URL>` → 200. |
| Exact file names, paths and sizes in bytes | `curl -s "https://huggingface.co/api/models/<repo>/tree/main?recursive=1"`. Never round sizes; the installer checks them. |
| Which repo holds the ComfyUI-ready files? | Prefer Comfy-Org `split_files/...` repos. A Diffusers layout (`transformer/`, `text_encoder/` folders) does not load in ComfyUI. |
| Variants of the same model | Quantisations (GGUF Q4 … Q8, BF16), base vs distilled, uncensored text encoders, fine-tunes. Decide which ones become presets, which become quants and which become alternatives (step 3). |
| Recommended settings | Steps, CFG, sampler, scheduler from the model card / official ComfyUI workflow. Distilled models usually mean few steps and CFG 1. |
| Licence | Apache / non-commercial / other. It goes into the preset `note`. |
| Memory on a 32 GB Mac | Diffusion model + text encoder + VAE. Above ~24 GB it swaps (GPU graph full of gaps, minutes per image). Make a quant that fits the default. GGUF on MPS needs ~1.5x the file size in Metal memory. If only model + encoder together are too big, add the family to `SPLIT_ENCODE_FAMILIES` (encode first, unload, then sample). Measure with `vmmap --summary <comfy pid>` (IOAccelerator) and `sysctl vm.swapusage`. |
| Which files does Leonard already have? | `ls ~/ComfyUI-Shared/models/{diffusion_models,text_encoders,vae}`. Files with other names must still be recognised (`alternatives`, step 3). |

## 2. Prove the raw graph works in ComfyUI

Before any app code: send a minimal API graph straight to `POST 127.0.0.1:8188/prompt` (loader → encode → latent →
sampler → decode → `SaveImage`) and look at the image. Note the time per step from
`~/Library/Logs/InpaintStudio-ComfyUI.log`. If this does not work, the app will not either.

## 3. `presets.py`

### Components (text encoder, VAE, nodes)
Add one entry per file to `COMPONENTS`, unless an identical file is already there (same file = reuse the key, e.g.
`qwen3_4b` is shared by Z-Image and klein 4B).

```python
"qwen3_8b": {"title": "Qwen3 8B text encoder", "repo": "Comfy-Org/...", "path": "split_files/text_encoders/qwen_3_8b.safetensors",
             "folder": "text_encoders", "size": 16_381_517_176},
```

- `path` is the path inside the repo; the local file name is its last part, or `save_as` when that name is too
  generic (`model.safetensors`!). Two components must never end up with the same local file name.
- `folder`: `text_encoders`, `vae`, `diffusion_models`, `loras`, `model_patches`, …
- `alternatives`: other local file names with **the same weights** (the original from a gated repo, a re-upload
  with another name, other quants of the same encoder). One of them present = installed, and the app uses it
  (`installer.item_path` / `installer.resolve`). Different weights (an uncensored encoder) are **not** an
  alternative: they are their own component and their own preset (twin, below).
- `gated: True` when the only source needs a Hugging Face account. The app then never downloads it
  (`installer.valid_item`, the install plan skips it) and the Download Center shows a Hugging Face link plus the file
  name to save it as.

### Preset
```python
"flux2_klein_4b": {
    "good_for": "...", "title": "FLUX.2 klein 4B", "family": "flux2", "modes": ["generate", "edit"],
    "note": "... licence ...", "repo": "unsloth/FLUX.2-klein-4B-GGUF", "default_quant": "BF16",
    "quants": {...}, "text_encoder": "qwen3_4b", "vae": "vae_flux2", "lora_family": "flux2_4b",
    "defaults": {"steps": 4, "cfg": 1.0, "sampler": "euler", "scheduler": "simple"},
},
```

- `family` picks the graph builder in `graphs.py` (step 4). Reuse an existing family only if the graph is really
  the same.
- `modes`: what the model can do, `edit` and/or `generate`.
- `quants`: `_quants(stem, {quant: size})` for GGUF files named `<stem>-<quant>.gguf` at the repo root. A quant from
  another repo or a sub-folder gets its own `repo` and `path` (`file` = local name). Quants may have
  `alternatives` too (`_quants(..., alternatives={"BF16": ["flux-2-klein-9b.safetensors"]})`).
- `default_quant`: one that fits 32 GB without swapping and ideally one that is already downloaded.
- `lora_family`: only when one graph family has several sizes whose LoRAs do not fit each other (klein 4B / 9B).
- `recommended` / `experimental` only when you mean it.

### Uncensored (or other encoder) twins
Same diffusion model, different text encoder = a **twin**, not a second card. Add it to the twin loop in
`presets.py` (`<id>_unc`, `twin_of`, its own `text_encoder`). Effects, already handled by the UI:
- Download Center: one card per model, with "Text encoder (original)" and "Text encoder (uncensored)".
- Model picker: both in the same group, "<model> · <quant>" and "<model> · <quant> · uncensored".

### LoRAs
- Add the model line to `LORA_GROUPS` (the import dialog's "Works with" list comes from there). **Without this a
  LoRA for the new model cannot be imported.**
- Picked LoRAs go into `COMPONENTS` via `_lora(...)` with their families.

## 4. `graphs.py`

Work through every family-aware place. `grep -n '"flux2"' graphs.py` shows a complete example.

- [ ] `CLIP_TYPES[family]`: the `CLIPLoader` type. GGUF encoders automatically use `CLIPLoaderGGUF`.
- [ ] `CPU_TEXT_ENCODER` if the encoder is fp8.
- [ ] `build_edit_graph`: an `elif family == ...` branch that sets `g["model"]`, `pos`, `neg` (and reference
      handling). Every node that samples uses `["model", 0]`; LoRAs are applied to `"unet"` before.
- [ ] Empty latent node for generate (`EmptySD3LatentImage` default, `EmptyLatentImage`, `EmptyFlux2LatentImage` …).
- [ ] Sampler: plain `KSampler` with the scheduler picker, or the chunked sampler with its own sigmas (Viggle,
      `Flux2Scheduler`). Own sigmas: add the family to the condition in `_sample_and_save` and a branch in
      `_chunked_sampler`, including denoise < 1.
- [ ] `MAX_REFS[family]` and `ref_labels` (how the prompt names image 1, 2 …) if it takes reference images.
- [ ] `REMOVE_BG_FAMILIES` (only if its VAE decodes RGBA), `CONTROL_FAMILIES` + `apply_control` (only if a control
      patch exists), the paste-mode exception (`family != "zimage"`) if it cannot do a free edit.
- [ ] Docstring of `build_edit_graph` lists the family.

## 5. `web/app.js` mirrors (must match `graphs.py`)

- [ ] `MAX_REFS` (same numbers as `graphs.MAX_REFS`).
- [ ] `refNoteFor` names (same as `graphs.ref_labels`) and the family list in `isDefaultRefNote`.
- [ ] `REMOVE_BG_FAMILIES`, `guideFamily()` (control guidance), the Z-Image / turbo special cases in `syncTaskUi`
      (`fam-zimage`, `fam-turbo`, steps min/max) and the model hint text, if they apply.

## 6. `server.py` and friends

- [ ] Family special cases in `create_job` (turbo clamp, remove background, control, `enhance.FAMILIES`).
- [ ] Anything new the UI needs goes into `/api/setup` (`setup_status`): components and presets are serialised there
      field by field, a new preset key is **not** sent automatically.
- [ ] `imports.guess_kind` knows the new text encoder / VAE file names.

## 7. Files the user already has

Leonard often downloads files himself. For every file of the model, check that the app recognises it under the name
it really has on disk:
- same weights, other name → `alternatives`;
- other weights (uncensored, fine-tune) → twin preset, or an import under Your files with "Runs like", text encoder
  and VAE picked in the dialog;
- a file that already sits in `models/<folder>` can be imported as is (no link is created, Remove leaves it alone).

Then open the Download Center and the model picker and confirm that every file he has shows as installed and the model
shows up in the picker. Nothing may say "missing" for a file that is on disk.

## 8. Tests

- [ ] `uv run pytest`, with ComfyUI running: `tests/test_graph_families.py` then builds every preset × task × mask ×
      refs graph and checks every node and input against `/object_info`.
- [ ] A unit test in `tests/test_graphs.py` for the new branch (node types, links, sigmas).
- [ ] `tests/test_installer.py`: alternatives / gated / twins if used. `test_loras_belong_to_a_group_of_known_families`
      must pass (every `LORA_GROUPS` entry matches a preset's `lora_family`).

## 9. Verify in the real app flow

On the dev server (`preview_start inpaint-studio-dev`, port 7381):
- [ ] Generate run with the new model → image looks right, time per step is sane (no swapping).
- [ ] Edit run (whole image, and with a mask) on the test picture (memory `test-example-image`).
- [ ] Download Center card: parts, sizes, installed states, download buttons / Hugging Face links, twins inside the
      card. Phone width too.
- [ ] Model picker: grouping and names ("<model> · <quant>[ · uncensored]").
- [ ] Your files → Import: the model appears under "Runs like", its encoder / VAE under "Text encoder" / "VAE", and its
      LoRA group under "Works with".
- [ ] Advanced → LoRAs: a LoRA of the new group shows as fitting, others as "made for …".

## 10. Docs

- [ ] `README.md`: model table under Download Center, plus any new user-visible behaviour.
- [ ] `CLAUDE.md`: the model list in the intro, a Rules bullet for the family's quirks.
- [ ] This file, if the process changed.

## 11. Build and hand over

- [ ] `./macos/build-app.sh`.
- [ ] `ps -eo command | grep MacOS/InpaintStudio` must show `~/Applications/Inpaint Studio.app`. A copy in
      `dist/dmg/` (left over from `macos/release.sh`) is not updated by the build: quit it and open the right one.
- [ ] Reload the app window (⌘R) once and check the change is visible there, not only on the dev server.
- [ ] Tell Leonard exactly which entry to pick in the model picker and which files are still missing (if any).
