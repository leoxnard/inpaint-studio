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


def grain_std(img: np.ndarray, where: np.ndarray) -> np.ndarray:
    """Strength of the fine noise (film grain, sensor noise) per channel: std of the high-pass where True."""
    hp = img - cv2.GaussianBlur(img, (0, 0), 1.5)
    return hp[where].std(axis=0) if where.any() else np.zeros(img.shape[-1], np.float32)


def grain_noise(o: np.ndarray, where: np.ndarray, shape: tuple, seed: int = 0) -> np.ndarray:
    """Unit-strength noise shaped like the grain of `o` (measured where True). Film / sensor grain is mostly
    the same in all channels: shared and per-channel noise are mixed like in the original."""
    hp = (o - cv2.GaussianBlur(o, (0, 0), 1.5))[where]
    c = np.corrcoef(hp.T) if len(hp) > 10 else np.eye(3)
    corr = float(np.clip(np.nan_to_num((c[0, 1] + c[0, 2] + c[1, 2]) / 3), 0, 1))
    rng = np.random.default_rng(seed)
    noise = (np.sqrt(corr) * rng.standard_normal(shape[:2] + (1,), np.float32)
             + np.sqrt(1 - corr) * rng.standard_normal(shape, np.float32))
    noise = cv2.GaussianBlur(noise, (0, 0), 0.6)   # grain is a little coarser than single pixels
    return noise / grain_std(noise, np.ones(noise.shape[:2], bool)).clip(1e-6)


def add_grain(original: Image.Image, result: Image.Image, seed: int = 0, mask: Image.Image | None = None) -> Image.Image:
    """Upscalers and edit models come out clean: give `result` (same picture as `original`, any size) back the
    original's grain, at the original's grain size (so it looks the same at the same print size). The strength is
    what the original has minus what the result still has, both measured at the original's size.
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
    need = np.sqrt(np.maximum(grain_std(o, src) ** 2 - grain_std(small, new) ** 2, 0))
    if need.max() <= 0.5:
        return result.convert("RGB")
    noise = cv2.resize(grain_noise(o, src, o.shape, seed), (r.shape[1], r.shape[0]), interpolation=cv2.INTER_CUBIC)
    noise /= grain_std(cv2.resize(noise, (w, h), interpolation=cv2.INTER_AREA), everywhere).clip(1e-6)
    if mask is not None:
        noise *= (np.asarray(mask.convert("L").resize((r.shape[1], r.shape[0]), Image.BILINEAR), np.float32) / 255)[..., None]
    r += noise * need
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
           feather: float = 0, grain: bool = False, seed: int = 0) -> Image.Image:
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
        need = np.sqrt(np.maximum(grain_std(o, ~hard) ** 2 - grain_std(r, hard) ** 2, 0))
        if need.max() > 0.5:
            region = Image.fromarray(np.clip(r + grain_noise(o, ~hard, r.shape, seed) * need, 0, 255).astype(np.uint8))
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
