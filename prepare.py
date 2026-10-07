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

import functools
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
GRAIN_STRENGTH = 1.0         # default strength: all the result lacks compared with the original


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


def grain_std(img: np.ndarray, where: np.ndarray) -> np.ndarray:
    """Overall grain strength per channel (all frequency bands, all brightness)."""
    flat = flat_pixels(img, where)
    if not flat.any():
        return np.zeros(img.shape[-1], np.float32)
    return np.sqrt(sum(_robust_std(_band(img, lo, hi)[flat]) ** 2 for lo, hi in FREQ_BANDS))


GRAIN_TILE = 32         # px: the grain spectrum is measured on tiles of this size (at the original's size)
GRAIN_MIN_PERIOD = 16   # px: slower waves are shading (picture), not grain
GRAIN_TILE_PRIOR = 20   # tiles: a brightness band with this many tiles is half its own value, half the overall
GRAIN_MAX_TILES = 4000


@functools.cache
def _tile_basis() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Hann window, the plane fit removed from every tile (a gradient is shading) and the bins slow enough to be picture."""
    w = np.hanning(GRAIN_TILE).astype(np.float32)
    yy, xx = np.mgrid[0:GRAIN_TILE, 0:GRAIN_TILE].astype(np.float32)
    plane = np.stack([np.ones_like(yy), yy, xx], -1).reshape(-1, 3)
    f = np.fft.fftfreq(GRAIN_TILE)
    slow = np.hypot(*np.meshgrid(f, f, indexing="ij")) < 1 / GRAIN_MIN_PERIOD
    return np.outer(w, w), plane @ np.linalg.pinv(plane), slow


def grain_tiles(img: np.ndarray, where: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Power spectra [tile, channel, y, x] (mean = variance) and mean brightness of the tiles (half overlapping, fully
    inside `where`) with the least picture structure: the flatter half by the gradient of the smoothed image, without
    clipped highlights or shadows."""
    t = GRAIN_TILE
    lum = _luma(img)
    smooth = cv2.GaussianBlur(lum, (0, 0), 3)
    grad = np.hypot(cv2.Sobel(smooth, cv2.CV_32F, 1, 0), cv2.Sobel(smooth, cv2.CV_32F, 0, 1))
    h, w = lum.shape
    ys, xs = np.meshgrid(np.arange(0, h - t + 1, t // 2), np.arange(0, w - t + 1, t // 2), indexing="ij")
    ys, xs = ys.ravel(), xs.ravel()
    box = lambda a: cv2.integral(a.astype(np.float64))
    mean = lambda ii: (ii[ys + t, xs + t] - ii[ys, xs + t] - ii[ys + t, xs] + ii[ys, xs]) / (t * t)
    inside = mean(box(where)) > 0.999
    tl, tg = mean(box(lum)), mean(box(grad))
    ok = inside & (tl > 8) & (tl < 247)
    if ok.sum() < 4:
        return np.zeros((0, img.shape[-1], t, t), np.float32), np.zeros(0, np.float32)
    ok &= tg <= np.median(tg[ok])
    idx = np.flatnonzero(ok)
    if len(idx) > GRAIN_MAX_TILES:
        idx = idx[np.argsort(tg[idx])[:GRAIN_MAX_TILES]]
    win, proj, _ = _tile_basis()
    tiles = np.stack([img[y:y + t, x:x + t] for y, x in zip(ys[idx], xs[idx])]).reshape(len(idx), t * t, -1)
    tiles = (tiles - proj @ tiles).reshape(len(idx), t, t, -1) * win[None, :, :, None]
    psd = np.abs(np.fft.fft2(tiles, axes=(1, 2))) ** 2 / (t * t * (win ** 2).mean())
    return np.moveaxis(psd, -1, 1).astype(np.float32), tl[idx].astype(np.float32)


def _spectrum(psd: np.ndarray) -> np.ndarray:
    """Typical spectrum of the tiles: the median per frequency (a tile with picture texture is an outlier, not part of
    the grain); one bin of noise is exponentially distributed, its mean is the median / ln 2."""
    return np.median(psd, axis=0) / np.log(2) if len(psd) else 0


def grain_need(o: np.ndarray, src: np.ndarray, r: np.ndarray, new: np.ndarray,
               strength: float = GRAIN_STRENGTH) -> tuple[np.ndarray, np.ndarray] | None:
    """Grain to add: its spectrum [channel, y, x] (what the original has in src minus what the result, at the
    original's size, still has in new, per frequency, times strength²; shading slower than GRAIN_MIN_PERIOD left out)
    and a gain per brightness band (LUMA_CENTERS; film grain is strongest in the midtones, sensor noise in the
    shadows). None when the original has no flat tiles to measure."""
    po, lo = grain_tiles(o, src)
    if not len(po):
        return None
    pr, lr = grain_tiles(r, new)
    _, _, slow = _tile_basis()
    need = np.maximum(_spectrum(po) - _spectrum(pr), 0) * strength ** 2
    need[:, slow] = 0
    var = lambda p: p[:, :, ~slow].sum((1, 2)) / GRAIN_TILE ** 2   # per tile and channel
    vo, vr = var(po), var(pr)
    overall = np.maximum(np.median(vo, 0) - (np.median(vr, 0) if len(vr) else 0), 0)
    gain = np.ones((len(LUMA_CENTERS), o.shape[-1]), np.float32)
    for i in range(len(LUMA_CENTERS)):
        mo = (lo >= LUMA_EDGES[i]) & (lo < LUMA_EDGES[i + 1])
        mr = (lr >= LUMA_EDGES[i]) & (lr < LUMA_EDGES[i + 1])
        if not mo.any():
            continue
        band = np.maximum(np.median(vo[mo], 0) - (np.median(vr[mr], 0) if mr.sum() >= 4 else np.median(vr, 0) if len(vr) else 0), 0)
        band = (mo.sum() * band + GRAIN_TILE_PRIOR * overall) / (mo.sum() + GRAIN_TILE_PRIOR)
        gain[i] = np.sqrt(band / np.maximum(overall, 1e-6)).clip(0, 3)
    return need, gain


def _channel_mix(o: np.ndarray, src: np.ndarray) -> float:
    """How much of the original's grain is the same in all channels (film / sensor grain mostly is)."""
    d = _band(o, *FREQ_BANDS[1])[flat_pixels(o, src)]
    if len(d) > 200_000:
        d = d[np.random.default_rng(0).choice(len(d), 200_000, replace=False)]
    c = np.corrcoef(d.T) if len(d) > 10 else np.eye(3)
    return float(np.clip(np.nan_to_num((c[0, 1] + c[0, 2] + c[1, 2]) / 3), 0, 1))


def _shape(white_f: np.ndarray, spec: np.ndarray, H: int, W: int, k: float = 1.0) -> np.ndarray:
    """Filters white noise (its rfft2, H x W) to the tile spectrum `spec` (cycles per original px; the noise is
    k times larger than the original)."""
    fy, fx = np.fft.fftfreq(H)[:, None], np.fft.rfftfreq(W)[None, :]
    u = np.broadcast_to((fx * k + 0.5) * GRAIN_TILE, (H, fx.shape[1])).astype(np.float32)   # on the shifted tile grid
    v = np.broadcast_to((fy * k + 0.5) * GRAIN_TILE, (H, fx.shape[1])).astype(np.float32)
    s = cv2.remap(np.fft.fftshift(spec).astype(np.float32), u, v, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    s = s * ((np.abs(fy * k) < 0.5) & (np.abs(fx * k) < 0.5))
    if k > 1:   # seen at the original's size, k pixels are averaged: what that damps is given more
        box = lambda f: np.abs(np.sinc(f * k) / np.sinc(f)).clip(0.3)
        s = s / (box(fy) * box(fx)) ** 2
    return np.fft.irfft2(white_f * np.sqrt(s), s=(H, W)).astype(np.float32)


def _unblur(spec: np.ndarray, seed: int = 0) -> np.ndarray:
    """The tile window blurs a measured spectrum, and grain made from it is blurred again when it is measured: noise
    made from `spec` is measured the same way once, and each frequency is corrected by what it lacks or has too much."""
    n = 256
    white_f = np.fft.rfft2(np.random.default_rng(seed).standard_normal((n, n), np.float32))
    every = np.ones((n, n), bool)
    _, _, slow = _tile_basis()
    out = spec.copy()
    for c in range(len(spec)):
        if spec[c].max() <= 0:
            continue
        test = _shape(white_f, spec[c], n, n)[..., None] + 128
        got = _spectrum(grain_tiles(test, every)[0])[0]
        out[c] = np.where(slow, 0, spec[c] * np.clip(spec[c] / np.maximum(got, 1e-9), 0.25, 4))
    return out


def grain_layer(o: np.ndarray, src: np.ndarray, need: tuple[np.ndarray, np.ndarray], r: np.ndarray,
                seed: int = 0) -> np.ndarray:
    """Noise to add to r: white noise with the original's channel mix, filtered in r's own size to the spectrum in
    `need` (measured at the original's size, so the grain has the same size at the same print size, coarse or fine),
    then scaled per pixel by the brightness gain."""
    spec, gain = need
    (h, w), (H, W) = o.shape[:2], r.shape[:2]
    k = W / w
    corr = _channel_mix(o, src)
    rng = np.random.default_rng(seed)
    shaped = _unblur(spec, seed)
    shared_f = np.fft.rfft2(rng.standard_normal((H, W), np.float32))
    out = np.empty((H, W, r.shape[-1]), np.float32)
    for c in range(r.shape[-1]):
        white_f = np.sqrt(corr) * shared_f + np.sqrt(1 - corr) * np.fft.rfft2(rng.standard_normal((H, W), np.float32))
        out[..., c] = _shape(white_f, shaped[c], H, W, k)
    seen = cv2.resize(out, (w, h), interpolation=cv2.INTER_AREA) if (H, W) != (h, w) else out
    out *= np.sqrt(spec.mean((1, 2))) / np.maximum(seen.reshape(-1, out.shape[-1]).std(0), 1e-6)
    lum = _luma(r)
    return out * np.stack([np.interp(lum, LUMA_CENTERS, gain[:, c]) for c in range(out.shape[-1])], -1).astype(np.float32)


def _enough(need: tuple[np.ndarray, np.ndarray] | None) -> bool:
    return need is not None and np.sqrt(need[0].mean((1, 2))).max() > 0.5   # below that it is 8-bit banding, not grain


def add_grain(original: Image.Image, result: Image.Image, seed: int = 0, mask: Image.Image | None = None,
              strength: float = GRAIN_STRENGTH) -> Image.Image:
    """Upscalers and edit models come out clean: give `result` (same picture as `original`, any size) back the
    original's grain, with the original's grain spectrum (so coarse grain stays coarse) at the original's grain size
    (so it looks the same at the same print size). Added is what the original has minus what the result still has,
    per frequency (grain_need), both measured at the original's size on flat tiles only, times strength.
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
    if not _enough(need):
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
    minus what the edit already has, as noise with its spectrum inside the mask."""
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
        if _enough(need):
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
