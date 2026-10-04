"""Input preparation before an edit and the matching step after it.

Crop & stitch: a masked edit can run on a crop around the mask instead of the whole image. The crop
is scaled to the working size like any image (small crops get more pixels, so more detail), and the
result is pasted back into the full-resolution original through the feathered mask. Outside the
mask the original pixels stay exactly as they were.

Outpaint: the image is placed on a larger canvas; the new area is filled with stretched, blurred
edge colours (a gray fill leaves a gray ghost, the edit models copy their input closely) and becomes
the mask, with a small overlap into the old image so the border blends.
"""

from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image, ImageFilter

MIN_CROP = 512      # px (source); smaller crops have too little context for the model
OUTPAINT_OVERLAP = 32


def mask_bbox(mask: Image.Image, threshold: int = 127) -> tuple[int, int, int, int] | None:
    """Bounding box (x0, y0, x1, y1), exclusive end, of the mask pixels above threshold."""
    m = np.asarray(mask.convert("L")) > threshold
    if not m.any():
        return None
    ys, xs = np.nonzero(m)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _span(a: int, b: int, pad: float, need: int, size: int) -> tuple[int, int]:
    """Grow [a, b) by pad on both sides and to at least need, then shift it into [0, size)."""
    a, b = a - pad, b + pad
    if b - a < need:
        grow = (need - (b - a)) / 2
        a, b = a - grow, b + grow
    length = min(round(b - a), size)
    start = min(max(round(a), 0), size - length)
    return start, length


def crop_box(bbox: tuple[int, int, int, int], src_w: int, src_h: int, context: float = 0.5,
             min_side: int = MIN_CROP) -> dict[str, int]:
    """Crop around a mask bbox: padded by context × the bbox size on each side, at least min_side
    per side (or the whole image side), kept inside the image."""
    x0, y0, x1, y1 = bbox
    x, w = _span(x0, x1, context * (x1 - x0), min_side, src_w)
    y, h = _span(y0, y1, context * (y1 - y0), min_side, src_h)
    return {"x": x, "y": y, "w": w, "h": h}


def crop(img: Image.Image, box: dict[str, int]) -> Image.Image:
    return img.crop((box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"]))


FREQ_BANDS = ((0.0, 0.7), (0.7, 1.5), (1.5, 3.0))   # grain is measured and rebuilt per spatial frequency band (DoG sigmas)
LUMA_EDGES = np.array([0, 40, 80, 120, 160, 200, 256], np.float32)   # ... and per brightness band
LUMA_CENTERS = (LUMA_EDGES[:-1] + LUMA_EDGES[1:]) / 2
LUMA_PRIOR = 2000            # pixels: a brightness band with this many flat pixels is half its own value, half the overall
GRAIN_MAX_SIGMA = 1.0        # the coarsest grain expected (blur of white noise), see plausible_grain
GRAIN_STRENGTH = 0.8         # default strength: grain on a smooth model result looks stronger than in the textured original


def _band(img: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Difference of Gaussians: the detail between sigma lo and hi (lo 0 = from single pixels up)."""
    return (cv2.GaussianBlur(img, (0, 0), lo) if lo else img) - cv2.GaussianBlur(img, (0, 0), hi)


def _robust_std(x: np.ndarray) -> np.ndarray:
    """Std per channel from the median absolute deviation: the few edges left in flat areas do not count."""
    if len(x) > 200_000:
        x = x[np.random.default_rng(0).choice(len(x), 200_000, replace=False)]
    return 1.4826 * np.median(np.abs(x - np.median(x, axis=0)), axis=0)


def _luma(img: np.ndarray) -> np.ndarray:
    return cv2.GaussianBlur(img.mean(axis=-1).astype(np.float32), (0, 0), 2)


def flat_pixels(img: np.ndarray, where: np.ndarray) -> np.ndarray:
    """Pixels where fine detail is grain and not picture: the quarter of `where` with the weakest local structure
    (gradient of the smoothed image, which the grain itself barely moves), without clipped highlights or shadows."""
    lum = _luma(img)
    grad = np.hypot(cv2.Sobel(lum, cv2.CV_32F, 1, 0), cv2.Sobel(lum, cv2.CV_32F, 0, 1))
    ok = where & (lum > 6) & (lum < 249)
    if ok.sum() < 400:
        return where
    return ok & (grad <= np.percentile(grad[ok], 25))


def grain_profile(img: np.ndarray, where: np.ndarray) -> np.ndarray:
    """Grain strength [frequency band, brightness band, channel], measured on the flat pixels of `where`.
    Per frequency band, because grain and the fine patterns a VAE or upscaler leaves behind sit at different
    scales (a total would let one stand in for the other); per brightness band, because film grain is strongest
    in the midtones and sensor noise in the shadows. Bands with too few pixels get the overall value."""
    out = np.zeros((len(FREQ_BANDS), len(LUMA_CENTERS), img.shape[-1]), np.float32)
    flat = flat_pixels(img, where)
    if not flat.any():
        return out
    lum = _luma(img)[flat]
    sel = [(lum >= LUMA_EDGES[i]) & (lum < LUMA_EDGES[i + 1]) for i in range(len(LUMA_CENTERS))]
    for f, (lo, hi) in enumerate(FREQ_BANDS):
        d = _band(img, lo, hi)[flat]
        overall = _robust_std(d)
        for i, m in enumerate(sel):
            # few pixels give a noisy value, and the noise only pushes the grain to add up (max(o² - r², 0)):
            # pulled towards the overall value, the fewer pixels the more
            n = int(m.sum())
            band = _robust_std(d[m]) if n >= 50 else overall
            out[f, i] = (n * band + LUMA_PRIOR * overall) / (n + LUMA_PRIOR)
    return out


def _white_band_ratios(sigma: float) -> np.ndarray:
    """Std of each frequency band relative to the next finer one, for white noise blurred by sigma."""
    noise = cv2.GaussianBlur(np.random.default_rng(0).standard_normal((256, 256), np.float32), (0, 0), sigma)
    std = np.array([_robust_std(_band(noise, lo, hi)[16:-16, 16:-16].reshape(-1, 1))[0] for lo, hi in FREQ_BANDS])
    return np.concatenate([[np.inf], std[1:] / std[:-1]])


def plausible_grain(profile: np.ndarray) -> np.ndarray:
    """Real film / sensor grain loses energy towards coarser bands, at most like white noise blurred by
    GRAIN_MAX_SIGMA. What a coarser band has beyond that is picture (skin pores, fabric, soft detail that slipped
    into the flat pixels), and adding it as noise looks blotchy: each band is capped at the finer one times that ratio."""
    out = profile.copy()
    for f in range(1, len(FREQ_BANDS)):
        out[f] = np.minimum(out[f], out[f - 1] * COARSE_RATIOS[f])
    return out


COARSE_RATIOS = _white_band_ratios(GRAIN_MAX_SIGMA)


def grain_std(img: np.ndarray, where: np.ndarray) -> np.ndarray:
    """Overall grain strength per channel (all frequency bands, all brightness)."""
    flat = flat_pixels(img, where)
    if not flat.any():
        return np.zeros(img.shape[-1], np.float32)
    return np.sqrt(sum(_robust_std(_band(img, lo, hi)[flat]) ** 2 for lo, hi in FREQ_BANDS))


def grain_need(o: np.ndarray, src: np.ndarray, r: np.ndarray, new: np.ndarray, strength: float = GRAIN_STRENGTH) -> np.ndarray:
    """Grain to add [frequency band, brightness band, channel]: what the original has (in src) minus what the result
    still has (in new), per band, times strength. The original's part is limited to a plausible grain spectrum."""
    po, pr = plausible_grain(grain_profile(o, src)), grain_profile(r, new)
    return strength * np.sqrt(np.maximum(po ** 2 - pr ** 2, 0))


def need_map(need: np.ndarray, r: np.ndarray) -> np.ndarray:
    """Per-pixel strength for one frequency band: the brightness band values interpolated at each pixel."""
    lum = _luma(r)
    return np.stack([np.interp(lum, LUMA_CENTERS, need[:, c]).astype(np.float32) for c in range(need.shape[1])], -1)


def grain_layer(o: np.ndarray, src: np.ndarray, need: np.ndarray, r: np.ndarray, seed: int = 0) -> np.ndarray:
    """Noise to add to r: white noise with the original's channel mix (film / sensor grain is mostly the same in
    all channels), split into the frequency bands, each scaled to what is missing there at each pixel's brightness.
    Built at the original's size (same grain at the same print size) and resized to r."""
    flat = flat_pixels(o, src)
    d = _band(o, *FREQ_BANDS[1])[flat]
    if len(d) > 200_000:
        d = d[np.random.default_rng(0).choice(len(d), 200_000, replace=False)]
    c = np.corrcoef(d.T) if len(d) > 10 else np.eye(3)
    corr = float(np.clip(np.nan_to_num((c[0, 1] + c[0, 2] + c[1, 2]) / 3), 0, 1))
    rng = np.random.default_rng(seed)
    white = (np.sqrt(corr) * rng.standard_normal(o.shape[:2] + (1,), np.float32)
             + np.sqrt(1 - corr) * rng.standard_normal(o.shape, np.float32))
    out = np.zeros(r.shape, np.float32)
    for f, (lo, hi) in enumerate(FREQ_BANDS):
        if need[f].max() <= 0.05:
            continue
        nb = _band(white, lo, hi)
        nb /= _robust_std(nb.reshape(-1, nb.shape[-1])).clip(1e-6)
        if nb.shape != r.shape:   # resizing loses some of the finest band: unit strength again, seen at o's size
            nb = cv2.resize(nb, (r.shape[1], r.shape[0]), interpolation=cv2.INTER_CUBIC)
            seen = cv2.resize(nb, (o.shape[1], o.shape[0]), interpolation=cv2.INTER_AREA)
            nb /= _robust_std(seen.reshape(-1, nb.shape[-1])).clip(1e-6)
        out += nb * need_map(need[f], r)
    return out


def add_grain(original: Image.Image, result: Image.Image, seed: int = 0, mask: Image.Image | None = None,
              strength: float = GRAIN_STRENGTH) -> Image.Image:
    """Upscalers and edit models come out clean: give `result` (same picture as `original`, any size) back the
    original's grain, at the original's grain size (so it looks the same at the same print size). The strength is
    what the original has minus what the result still has, per frequency and brightness band (grain_profile), both
    measured at the original's size on flat areas only (edges and texture are not grain), times strength.
    mask (masked edits): only the masked area is new, so the grain is measured outside it in the original, inside
    it in the result, and added only there (feathered by the mask)."""
    o = np.asarray(original.convert("RGB"), np.float32)
    r = np.asarray(result.convert("RGB"), np.float32)
    h, w = o.shape[:2]
    everywhere = np.ones((h, w), bool)
    src, new = everywhere, everywhere
    if mask is not None:
        hard = np.asarray(mask.convert("L").resize((w, h), Image.BILINEAR)) > 127
        if hard.any() and not hard.all():
            src, new = ~hard, hard
    small = cv2.resize(r, (w, h), interpolation=cv2.INTER_AREA)
    need = grain_need(o, src, small, new, strength)
    if need.max() <= 0.5:   # below that it is 8-bit banding, not grain
        return result.convert("RGB")
    noise = grain_layer(o, src, need, r, seed)
    if mask is not None:
        noise *= (np.asarray(mask.convert("L").resize((r.shape[1], r.shape[0]), Image.BILINEAR), np.float32) / 255)[..., None]
    r += noise
    return Image.fromarray(np.clip(r, 0, 255).astype(np.uint8))


# Upscaled PNGs came out at ~0.7-1.15x the source's PNG bytes per pixel (UltraSharp, RealESRGAN, SeedVR2, with grain)
UPSCALE_PNG_RATIO = 0.9


def png_bytes_per_pixel(img: Image.Image) -> float:
    """Bytes per pixel of `img` saved as PNG the way ComfyUI saves (compress level 4)."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", compress_level=4)
    return buf.tell() / (img.size[0] * img.size[1])


def size_for_megabytes(w: int, h: int, bpp: float, mb: float) -> tuple[int, int]:
    """Output size whose upscaled PNG is roughly `mb` MB (10^6 bytes), from the source's PNG bytes per pixel."""
    f = (mb * 1e6 / (UPSCALE_PNG_RATIO * bpp * w * h)) ** 0.5
    return max(1, round(w * f)), max(1, round(h * f))


def stitch(original: Image.Image, result: Image.Image, mask: Image.Image, box: dict[str, int],
           feather: float = 0, grain: bool = False, seed: int = 0, grain_strength: float = GRAIN_STRENGTH) -> Image.Image:
    """Paste the edited crop back into the original. mask is the full-size mask (source pixels);
    feather is a blur radius in source pixels. Pixels outside the (feathered) mask stay untouched.
    grain: the edit comes out clean, so add the original's grain (measured outside the mask in the crop)
    minus what the edit already has, as fine noise inside the mask."""
    original = original.convert("RGB")
    mask = mask.convert("L").resize(original.size, Image.BILINEAR)
    m = crop(mask, box)
    if feather > 0:
        m = m.filter(ImageFilter.GaussianBlur(feather))
    region = result.convert("RGB").resize((box["w"], box["h"]), Image.LANCZOS)
    if grain:
        o = np.asarray(crop(original, box), np.float32)
        r = np.asarray(region, np.float32)
        hard = np.asarray(crop(mask, box)) > 127
        need = grain_need(o, ~hard, r, hard, grain_strength)
        if need.max() > 0.5:
            noise = grain_layer(o, ~hard, need, r, seed)
            region = Image.fromarray(np.clip(r + noise, 0, 255).astype(np.uint8))
    out = original.copy()
    out.paste(region, (box["x"], box["y"]), m)
    return out


def hole_mask(holes: Image.Image | None, x: int, y: int, size: tuple[int, int], canvas_w: int, canvas_h: int,
              grow: int) -> np.ndarray:
    """Erased parts of the image (white = erased, any size, stretched to the image) on the canvas, grown by
    a few pixels so the new content blends into what is kept. 0/255, canvas size."""
    out = np.zeros((canvas_h, canvas_w), np.uint8)
    if holes is None:
        return out
    h = np.asarray(holes.convert("L").resize(size, Image.BILINEAR)) > 127
    out[y:y + size[1], x:x + size[0]] = h * 255
    if grow > 0 and out.any():
        out = cv2.dilate(out, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1)))
    return out


def pad(img: Image.Image, x: int, y: int, canvas_w: int, canvas_h: int,
        overlap: int = OUTPAINT_OVERLAP, holes: Image.Image | None = None) -> tuple[Image.Image, Image.Image]:
    """Place img at (x, y) on a canvas_w × canvas_h canvas. Returns the filled canvas and the mask
    (new area + overlap into the old image on the extended sides, feathered). holes: parts of the image
    that were erased (white); they are filled from their surroundings (so the model does not copy what was
    there) and masked like the new area."""
    img = img.convert("RGB")
    w, h = img.size
    if not (0 <= x <= canvas_w - w and 0 <= y <= canvas_h - h):
        raise ValueError("the image must lie inside the canvas")
    left, top, right, bottom = x, y, canvas_w - w - x, canvas_h - h - y
    a = np.asarray(img)
    filled = cv2.copyMakeBorder(a, top, bottom, left, right, cv2.BORDER_REPLICATE)
    sigma = max(canvas_w, canvas_h) / 40
    hole = hole_mask(holes, x, y, (w, h), canvas_w, canvas_h, max(2, overlap // 4))
    if hole.any():   # fill the holes from their edges first, so the blur below has no old content to smear
        filled = cv2.inpaint(filled, hole, 5, cv2.INPAINT_TELEA)
    blurred = cv2.GaussianBlur(filled, (0, 0), sigma)
    inside = np.zeros((canvas_h, canvas_w), bool)
    inside[top:top + h, left:left + w] = True
    out = np.where((inside & (hole == 0))[..., None], filled, blurred)

    mask = np.full((canvas_h, canvas_w), 255, np.uint8)
    o = overlap
    mask[top + (o if top else 0):top + h - (o if bottom else 0), left + (o if left else 0):left + w - (o if right else 0)] = 0
    mask_img = Image.fromarray(mask)
    if o:
        mask_img = mask_img.filter(ImageFilter.GaussianBlur(o / 3))
        # the new area itself stays fully masked, only the overlap fades
        mask_img = Image.fromarray(np.where(inside, np.asarray(mask_img), 255).astype(np.uint8))
    if hole.any():
        soft = cv2.GaussianBlur(hole, (0, 0), max(1.0, o / 6))
        mask_img = Image.fromarray(np.maximum(np.asarray(mask_img), np.maximum(soft, hole)))
    return Image.fromarray(out), mask_img
