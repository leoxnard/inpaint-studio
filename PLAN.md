# PLAN: UI redesign into Create + Runs views (Ollama style)

## Goal
Restructure the web UI (`web/`) into two views and restyle it, without changing what the app can do:
- **Create**: set up a run (task, model, image, area/mask, prompt, settings) and add it to the queue.
- **Runs**: watch the queue, view running and finished runs, compare, align, download.

Users: Leonard only, local Mac app (http://127.0.0.1:7380 or the bundled .app).

## Non-goals
- No backend changes: `server.py`, `graphs.py`, `align.py`, the `/api/*` routes and `/ws/jobs` stay as they are.
  If something really needs an API change, stop and note it under Open questions.
- No new features beyond the regrouping below (no reordering of the queue on the server if it does not exist yet,
  see Open questions).
- No build step, no framework. Stay vanilla HTML/JS/CSS.
- The first-run setup view (`web/setup.js`, `#setupView`) only gets the new tokens, not a new layout.

## References
- `design/create.dc.html`, `design/runs.dc.html`: the approved mockups (Claude Design canvas, "Ollama style" boards).
  They are `.dc.html` files: the markup inside `<x-dc>` and the inline styles are the reference; `{{holes}}`,
  `<sc-if>`/`<sc-for>` and the `DCLogic` script are mockup logic only. They do not run standalone.
- `design/ollama-DESIGN.md`: the style spec the mockups follow.
- Canvas with all explored directions: https://claude.ai/artifact/4wrF6C8fARf6kFdg4jmgMP (private).

## Design tokens (put them as CSS variables in `web/styles.css`)
- Colors: canvas `#FFFFFF`, soft surface `#FAFAFA`, hairline `#E5E5E5`, hairline strong `#D4D4D4`,
  ink `#000000`, charcoal `#525252`, body `#737373`, selected fill `#EDEDED`, status green `#27C93F`,
  focus ring `rgba(59,130,246,0.5)` as `box-shadow: 0 0 0 3px`.
  No accent color. Primary actions are black pills with white text.
- Mask overlay on the image: black at 40% with a white dashed outline (no colour accent in this style).
- Radius: every button, input, select and segmented control is a pill (`9999px`). Text areas, option cards
  (Area to change, aspect ratio tiles), panels, queue cards and result tiles use `12px`; images `8px`.
- No shadows, no gradients in chrome.
- Type: headings `ui-rounded, 'Nunito', system-ui` weight 600; everything else
  `ui-sans-serif, system-ui, -apple-system, sans-serif`. Nunito from Google Fonts only as fallback
  (check that the app works offline without it).
- Dark mode: not designed. See Open question 4.

## Layout

### Header (both views)
`Inpaint Studio` | pill switch **Create / Runs** (Runs shows a black badge with the number of running + waiting
runs) | spacer | ComfyUI status (green dot) | Downloads button (opens existing setup view).

### Create view
Three columns: left sidebar (~300px), center (flexible), right sidebar (~340px). Wraps on narrow widths.

Left sidebar, top to bottom:
1. Task switch **Edit a photo / Generate new** (two equal buttons). Replaces `#taskTabs`.
2. **Model** select (existing `#modelSel` logic incl. quant) + small hint which models are listed +
   "Get more" link (`#moreModels`). Moves out of the header.
3. Edit only: **Image** (current file, batch thumbnails grid, add images / open folder, Submit and next, Skip,
   the other batch buttons behind a "Batch…" menu or a collapsible row).
4. Edit only: **Size** (megapixels, reference px, auto-fix, match reference, size info + a token bar
   "3626 of 4096 tokens").
5. Edit only: **Area to change**, three option cards (radio group), each with a one-line explanation:
   - Whole image: "No mask. The prompt can change everything." (`mode=none`)
   - Masked area, inpaint: "Only the masked pixels are regenerated." (`mode=inpaint`)
   - Masked area, free edit + paste: "Edits freely, then pastes the masked area back." (`mode=paste`)
   Replaces the `#mode` select and `#modeHint`.
6. Generate only: **Size** with aspect ratio tiles (1:1, 4:3, 3:4, 3:2, 2:3, 16:9, 9:16 with a small
   rectangle icon each; replaces `#aspect`), megapixels, resulting size + tokens. Below: hint
   "To keep working on a result, open it in Runs and choose Use as new input."

Center:
- Edit: the stage (`#stage`, `#display` canvas) with the image and mask overlay; size info line above it.
- Generate: an empty dashed frame in the chosen aspect ratio ("The new image will have this shape.").
- Edit with a mask mode: **Mask panel** under the stage, two columns:
  - *Find mask*: text input + "Compute mask" on one line; below in one row Threshold (slider),
    Refine, Expand, Invert. Mask time / stale warning as small text.
  - *Touch up*: Paint / Erase / Off segmented control, then Undo, Clear, Fill as one group that only
    wraps as a whole; below Brush size and Overlay opacity sliders.
  The panel is hidden for "Whole image" and for Generate.

Right sidebar (settings):
- Preset select + Save as preset / Delete preset, Prompt (textarea, 12px radius).
- "Keep everything else identical" checkbox: only for free edit + paste.
- Steps, CFG, Seed + Random; Edit only: Denoise; mask modes only: Feather.
- "After pasting, fix" (colours, local warp, Poisson): only for free edit + paste.
- Output: Save every N, Save last N, Upscale, Upscaler. **Upscaler is disabled and greyed out while Upscale is Off.**
- Advanced (collapsed `<details>`): negative prompt, sampler, scheduler, UNet / CLIP / VAE.
- Bottom: black pill "Add edit to queue" / "Add image to queue" + "Cancel" if a run is active, and the hint
  "Queued runs start one after another. Follow them in Runs; you can keep working here."
  After queueing show a toast "Queued as Run N" with a link to Runs (do not switch view automatically).

### Runs view
Three columns.

Left: **Queue**
- Running run card: thumbnail, "Run N", prompt (one line, ellipsis), progress bar, "Step 12 of 20, about 40 s left".
- Waiting runs: position, Remove (and Up only if the backend supports reordering, see Open questions).
- Pause/Resume queue only if the backend supports it (Open questions); otherwise leave it out.
- Hint with link back to Create.

Center: **Viewer** for the selected run
- Title "Run N", status text, file name.
- Toolbar above the image:
  - **Pasted result / Raw (full generated image)**: only for paste-mode runs (replaces `#viewRaw`).
  - **Compare with original** checkbox: finished edit runs (existing compare slider `#compare`).
- Image area by status: running = live preview (`#liveImg`), finished edit = result (+ compare slider),
  finished generate = result only, waiting = "Waiting. N runs ahead of this one.", failed = the error message
  with what to do (e.g. gray output / token limit).
- Steps strip under the image: title "Saved steps" or "Live previews", **Live previews** checkbox
  (replaces `#viewLive`), hint "← → to step through", keyboard stepping as today (`#filmstrip`).
- **Results** section below: filter pills All / Edits / Generated, count, grid of finished and failed runs
  (replaces `#history`); clicking a tile selects it in the viewer.

Right: **Details**
- Prompt, settings list (task + area, model, size, steps, seed, CFG, time).
- Actions: "Load settings in Create" (primary, switches to Create with the run's settings),
  "Use result as new input" (existing `#useResult`), "Align to original" (edit runs; opens the existing align
  panel `#alignPanel` in the viewer), downloads (`#downloadBtn`, `#downloadUpscaled`, `#downloadSteps`),
  "Cancel run" (running), "Remove from queue" (waiting), "Delete run" (finished/failed, `DELETE /api/runs/{id}`).

## Implementation approach
- One `index.html` with two view containers (`#createView`, `#runsView`); switch with a small router on
  `location.hash` (`#create`, `#runs`, `#runs/<id>`) so reloads keep the view and selected run.
- **Keep the existing element ids** wherever the element survives, so `app.js` logic keeps working; move
  markup instead of rewriting logic. New ids only for new elements (task switch, area cards, aspect tiles,
  view switch, results filter).
- `mode`/`task`/`aspect` stay the single source of truth: the new controls write into the existing hidden
  inputs or call the same setters, then the existing `change` handlers run.
- Visibility rules (edit-only, gen-only, needs-mask, paste-only) already exist as classes; reuse them.

## Milestones
Each milestone: `uv run pytest` green, app checked in the Browser pane (`preview_start` name `inpaint-studio`)
at desktop width and ~390px, console clean, one commit per milestone.

- **M1 Tokens + base styles.** CSS variables, pill/12px radius rules, fonts, buttons, inputs, focus ring.
  No layout change yet. Check: every existing control still works and looks like the Ollama style.
- **M2 Header + view switch.** Create / Runs switch with hash router, Runs badge from the jobs websocket,
  model picker moved out of the header (temporarily stays in the left column top).
  Check: reload on `#runs` stays on Runs; badge updates while a run is active.
- **M3 Create left column.** Task switch on top, model, image/batch, size, Area cards, generate size with
  aspect tiles. Check: switching task/area shows/hides the right sections; queued runs use the chosen mode
  and aspect (compare the job payload in the network tab with the old UI).
- **M4 Create center + mask panel.** Stage, generate placeholder frame, mask panel below the stage with the
  two-column layout. Check: compute mask, paint/erase, undo/clear/fill, overlay all work; panel hidden for
  whole image and generate.
- **M5 Create right column.** Settings order, conditional fields, upscaler disabled when Upscale is Off,
  queue button + toast. Check: a run with every option set produces the same job payload as before.
- **M6 Runs view: queue + viewer.** Queue list from `/ws/jobs`, viewer by status, Pasted/Raw toggle,
  compare checkbox, steps strip with live toggle and arrow keys, align panel. Check: start two runs and watch
  both through to done; reload mid-run and the view recovers.
- **M7 Runs view: results + details.** Results grid with filters, details panel and all actions incl.
  "Load settings in Create" and delete. Check: delete removes the run folder entry from `GET /api/runs`.
- **M8 Cleanup + docs.** Remove dead markup/CSS, update README (screens, how the two views work) and the
  brain note `~/brain/Projects/inpaint-studio.md`. Rebuild the Mac app with `macos/build-app.sh` and open it once.

## Deploy
None. Local only. The Mac app bundles `web/`, so rebuild it in M8.

## Open questions (decide while implementing, note the answer in the commit)
1. Does the backend support reordering or pausing the queue? If not, drop "Up" and "Pause queue" for now
   (no backend changes in this plan).
2. Does the model list already filter by task (edit vs. generate models)? If not, show all and keep the hint out.
3. Mockup used "Show in Finder"; the app has download buttons. Keep the downloads (works in browser and app);
   Finder only if the Mac app already has a bridge for it.
4. Does `styles.css` have a dark mode today? If yes, keep a minimal dark token set.
