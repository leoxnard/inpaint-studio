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


def stitch(original: Image.Image, result: Image.Image, mask: Image.Image, box: dict[str, int],
           feather: float = 0) -> Image.Image:
    """Paste the edited crop back into the original. mask is the full-size mask (source pixels);
    feather is a blur radius in source pixels. Pixels outside the (feathered) mask stay untouched."""
    original = original.convert("RGB")
    mask = mask.convert("L").resize(original.size, Image.BILINEAR)
    m = crop(mask, box)
    if feather > 0:
        m = m.filter(ImageFilter.GaussianBlur(feather))
    region = result.convert("RGB").resize((box["w"], box["h"]), Image.LANCZOS)
    out = original.copy()
    out.paste(region, (box["x"], box["y"]), m)
    return out


def pad(img: Image.Image, x: int, y: int, canvas_w: int, canvas_h: int,
        overlap: int = OUTPAINT_OVERLAP) -> tuple[Image.Image, Image.Image]:
    """Place img at (x, y) on a canvas_w × canvas_h canvas. Returns the filled canvas and the mask
    (new area + overlap into the old image on the extended sides, feathered)."""
    img = img.convert("RGB")
    w, h = img.size
    if not (0 <= x <= canvas_w - w and 0 <= y <= canvas_h - h):
        raise ValueError("the image must lie inside the canvas")
    left, top, right, bottom = x, y, canvas_w - w - x, canvas_h - h - y
    a = np.asarray(img)
    filled = cv2.copyMakeBorder(a, top, bottom, left, right, cv2.BORDER_REPLICATE)
    sigma = max(canvas_w, canvas_h) / 40
    blurred = cv2.GaussianBlur(filled, (0, 0), sigma)
    inside = np.zeros((canvas_h, canvas_w), bool)
    inside[top:top + h, left:left + w] = True
    out = np.where(inside[..., None], filled, blurred)

    mask = np.full((canvas_h, canvas_w), 255, np.uint8)
    o = overlap
    mask[top + (o if top else 0):top + h - (o if bottom else 0), left + (o if left else 0):left + w - (o if right else 0)] = 0
    mask_img = Image.fromarray(mask)
    if o:
        mask_img = mask_img.filter(ImageFilter.GaussianBlur(o / 3))
        # the new area itself stays fully masked, only the overlap fades
        mask_img = Image.fromarray(np.where(inside, np.asarray(mask_img), 255).astype(np.uint8))
    return Image.fromarray(out), mask_img
