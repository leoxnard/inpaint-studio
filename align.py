"""Post-hoc alignment of a free edit ("raw") to the original before pasting the masked area.

The edit model sometimes shifts or slightly rescales the whole picture. We estimate that
transform from the area OUTSIDE the mask (where both images should match), then paste only the
masked area of the transformed raw image into the original. Pixels the transformed image no
longer covers fall back to the original.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

ANALYSIS_WIDTH = 512
FINE_WIDTH = 1536


def _gray(img: Image.Image, size: tuple[int, int]) -> np.ndarray:
    return np.asarray(img.convert("L").resize(size, Image.BILINEAR), dtype=np.float64)


def transform(img: Image.Image, dx: float, dy: float, scale: float, fill=0) -> Image.Image:
    """Scale around the image centre, then shift by (dx, dy) pixels."""
    w, h = img.size
    cx, cy = w / 2, h / 2
    inv = 1.0 / scale
    coeffs = (inv, 0, cx - (cx + dx) * inv, 0, inv, cy - (cy + dy) * inv)
    return img.transform(img.size, Image.AFFINE, coeffs, resample=Image.BICUBIC, fillcolor=fill)


def _phase_shift(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float]:
    """Shift (dx, dy) that moves b onto a, plus peak strength."""
    fa, fb = np.fft.fft2(a), np.fft.fft2(b)
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-9
    corr = np.fft.ifft2(r).real
    h, w = corr.shape
    py, px = np.unravel_index(np.argmax(corr), corr.shape)

    def sub(c_m, c_0, c_p):  # parabolic sub-pixel refinement around the peak
        d = c_m - 2 * c_0 + c_p
        return 0.0 if abs(d) < 1e-12 else 0.5 * (c_m - c_p) / d

    dx = px + sub(corr[py, (px - 1) % w], corr[py, px], corr[py, (px + 1) % w])
    dy = py + sub(corr[(py - 1) % h, px], corr[py, px], corr[(py + 1) % h, px])
    if dy > h / 2:
        dy -= h
    if dx > w / 2:
        dx -= w
    return float(dx), float(dy), float(corr.max())


def estimate(original: Image.Image, raw: Image.Image, mask: Image.Image,
             scales: np.ndarray | None = None) -> dict:
    """Estimate dx, dy (full-resolution pixels) and scale that map raw onto original."""
    w, h = original.size
    f = ANALYSIS_WIDTH / w
    size = (ANALYSIS_WIDTH, max(16, round(h * f)))
    keep = _gray(mask, size) < 20  # compare only clearly unmasked pixels
    window = np.outer(np.hanning(size[1]), np.hanning(size[0]))

    def prep(arr: np.ndarray) -> np.ndarray:
        arr = arr.copy()
        mean = arr[keep].mean() if keep.any() else arr.mean()
        arr[~keep] = mean
        return (arr - mean) * window

    a = prep(_gray(original, size))
    raw_small = raw.convert("L").resize(size, Image.BILINEAR)
    if scales is None:
        scales = np.round(np.arange(0.96, 1.0401, 0.005), 4)
    best = None
    for s in scales:
        b = prep(np.asarray(transform(raw_small, 0, 0, float(s), fill=0), dtype=np.float64))
        dx, dy, strength = _phase_shift(a, b)
        if best is None or strength > best[3]:
            best = (dx, dy, float(s), strength)
    dx, dy, s, strength = best
    dx, dy = dx / f, dy / f

    # fine pass at (up to) full resolution: remaining translation after the coarse transform
    fw = min(w, FINE_WIDTH)
    ff = fw / w
    fsize = (fw, max(16, round(h * ff)))
    fkeep = _gray(mask, fsize) < 20
    fwin = np.outer(np.hanning(fsize[1]), np.hanning(fsize[0]))

    def fprep(arr: np.ndarray) -> np.ndarray:
        arr = arr.copy()
        valid = fkeep & (arr > -1)
        mean = arr[valid].mean() if valid.any() else arr.mean()
        arr[~fkeep] = mean
        return (arr - mean) * fwin

    coarse = transform(raw.convert("L").resize(original.size, Image.BILINEAR), dx, dy, s, fill=0)
    fdx, fdy, fstrength = _phase_shift(fprep(_gray(original, fsize)), fprep(_gray(coarse, fsize)))
    if abs(fdx) / ff < 8 and abs(fdy) / ff < 8:  # ignore implausible jumps
        dx, dy = dx + fdx / ff, dy + fdy / ff
    return {"dx": round(dx, 1), "dy": round(dy, 1), "scale": s, "confidence": round(max(strength, fstrength), 4)}


def compose(original: Image.Image, raw: Image.Image, mask: Image.Image,
            dx: float, dy: float, scale: float) -> tuple[Image.Image, dict]:
    """Paste the masked area of the transformed raw image into the original."""
    original = original.convert("RGB")
    raw = raw.convert("RGB").resize(original.size, Image.BICUBIC)
    mask_l = mask.convert("L").resize(original.size, Image.BILINEAR)
    moved = transform(raw, dx, dy, scale)
    valid = transform(Image.new("L", original.size, 255), dx, dy, scale)
    o = np.asarray(original, dtype=np.float64)
    m = np.asarray(moved, dtype=np.float64)
    weight = (np.asarray(mask_l, dtype=np.float64) / 255.0) * (np.asarray(valid, dtype=np.float64) / 255.0)
    out = o * (1 - weight[..., None]) + m * weight[..., None]
    outside = (np.asarray(mask_l) < 20) & (np.asarray(valid) > 250)
    diff = float(np.abs(o - m).mean(axis=-1)[outside].mean()) if outside.any() else 0.0
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)), {"outside_diff": round(diff, 2)}
