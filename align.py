"""Post-hoc alignment of a free edit ("raw") to the original before pasting the masked area.

The edit model sometimes shifts or slightly rescales the whole picture. We estimate that
transform from the area OUTSIDE the mask (where both images should match), then paste only the
masked area of the transformed raw image into the original. Pixels the transformed image no
longer covers fall back to the original.

Optional fixes, all estimated from the area outside the mask (where raw and original should be
identical): a local warp (dense optical flow, smoothed and continued into the mask), a colour /
exposure correction field (Lab offsets, smoothly continued into the mask) and a seamless edge: the
cut between original and edit is moved, within a band around the mask edge, to where both agree.
"""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

ANALYSIS_WIDTH = 512
FINE_WIDTH = 1536
SEAM_OUT, SEAM_IN, SEAM_FEATHER = 40, 8, 2.0  # seam band (px) and feather (sigma)
WARP_MAX_LOCAL = 2.0  # px (analysis scale) a flow vector may differ from the large-scale field


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


def _fill_smooth(field: np.ndarray, weight: np.ndarray, sigma: float) -> np.ndarray:
    """Normalized convolution: smooth `field` using only pixels with weight > 0 and continue it into
    the zero-weight area (the mask). Far from any known pixel it falls back to the weighted mean."""
    w = weight.astype(np.float32)
    if field.ndim == 3:
        w3 = w[..., None]
    else:
        w3 = w
    num = cv2.GaussianBlur(field * w3, (0, 0), sigma)
    den = cv2.GaussianBlur(w, (0, 0), sigma)
    den3 = den[..., None] if field.ndim == 3 else den
    mean = (field * w3).reshape(-1, *field.shape[2:]).sum(0) / max(float(w.sum()), 1e-6)
    conf = np.clip(den3 / 0.05, 0, 1)  # low support -> blend towards the global mean
    return np.where(den3 > 1e-6, num / np.maximum(den3, 1e-6), mean) * conf + mean * (1 - conf)


def _flow_reliable(g0: np.ndarray, g1: np.ndarray, flow: np.ndarray, back: np.ndarray) -> np.ndarray:
    """Pixels whose flow can be trusted: forward and backward flow agree (< 1 px round trip) and the
    warped detail (high-pass, so a colour drift doesn't count) matches the original. Where the edit
    drew something different, DIS still returns a vector, often a large wrong one."""
    h, w = g0.shape
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    mx, my = gx + flow[..., 0], gy + flow[..., 1]
    round_trip = flow + cv2.remap(back, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    hp = lambda g: g.astype(np.float32) - cv2.GaussianBlur(g.astype(np.float32), (0, 0), 3)
    detail = cv2.GaussianBlur(np.abs(hp(g0) - cv2.remap(hp(g1), mx, my, cv2.INTER_LINEAR)), (0, 0), 2)
    return ((np.linalg.norm(round_trip, axis=-1) < 1.0) & (detail < 6)).astype(np.float32)


def fix_warp(original: np.ndarray, raw: np.ndarray, keep: np.ndarray) -> np.ndarray:
    """Undo small local distortions of raw: dense optical flow (DIS) original -> raw, measured outside
    the mask, smoothed and continued into it, then raw is resampled onto the original's grid."""
    h, w = original.shape[:2]
    f = min(1.0, 1024 / max(h, w))
    small = (max(16, round(w * f)), max(16, round(h * f)))
    g0 = cv2.cvtColor(cv2.resize(original, small, interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
    g1 = cv2.cvtColor(cv2.resize(raw, small, interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    flow, back = dis.calc(g0, g1, None), dis.calc(g1, g0, None)
    k = cv2.resize(keep.astype(np.uint8), small, interpolation=cv2.INTER_NEAREST)
    k = cv2.erode(k, np.ones((9, 9), np.uint8))  # patches touching the mask compare different content
    k = k.astype(np.float32) * _flow_reliable(g0, g1, flow, back)
    # a warp is a smooth distortion; vectors far off the large-scale field belong to things the model
    # moved or redrew (a hand placed differently) and would stretch them when continued into the mask
    coarse = _fill_smooth(flow, k, sigma=max(small) / 10)
    k = k * (np.linalg.norm(flow - coarse, axis=-1) < WARP_MAX_LOCAL)
    flow = _fill_smooth(flow, k, sigma=max(small) / 40)
    flow = cv2.resize(flow, (w, h), interpolation=cv2.INTER_LINEAR) / f
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    return cv2.remap(raw, gx + flow[..., 0], gy + flow[..., 1], cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)


def match_colors(original: np.ndarray, raw: np.ndarray, keep: np.ndarray) -> np.ndarray:
    """Correct exposure / colour drift: Lab offset original - raw outside the mask, as a smooth field
    (so a gradient like 'darker on the left' is fixed too), continued into the mask."""
    lab0 = cv2.cvtColor(original.astype(np.float32) / 255, cv2.COLOR_RGB2LAB)
    lab1 = cv2.cvtColor(raw.astype(np.float32) / 255, cv2.COLOR_RGB2LAB)
    diff = _fill_smooth(lab0 - lab1, keep.astype(np.float32), sigma=max(original.shape[:2]) / 12)
    out = cv2.cvtColor(lab1 + diff.astype(np.float32), cv2.COLOR_LAB2RGB)
    return np.clip(out * 255, 0, 255)


def _smoothstep(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0, 1)
    return t * t * (3 - 2 * t)


def outpaint_blend(original: Image.Image, raw: Image.Image, box: tuple[int, int, int, int], overlap: int,
                   colors: bool = True, holes: np.ndarray | None = None) -> Image.Image:
    """Outpainting: paste the generated image (raw) around the old one (box = x0, y0, x1, y1 in raw pixels).
    The model draws a lighter or darker halo right where its regenerated area starts (overlap px inside the
    old image), so the blend starts after that halo and ends just outside the old image, where the
    original is only a blurred fill. colors: also shift the generated area, per line along each extended
    edge, by the Lab difference between a strip of the old image and a strip of the new area (median
    filtered, so a tree or a shadow at the edge doesn't count), fading out with the distance."""
    w, h = raw.size
    o = np.asarray(original.convert("RGB").resize((w, h), Image.BICUBIC), np.float32)
    r = np.asarray(raw.convert("RGB"), np.float32)
    x0, y0, x1, y1 = box
    inside = np.zeros((h, w), np.uint8)
    inside[y0:y1, x0:x1] = 1
    signed = cv2.distanceTransform(1 - inside, cv2.DIST_L2, 5) - cv2.distanceTransform(inside, cv2.DIST_L2, 5)
    start, end = -0.4 * overlap, 0.15 * overlap + 1   # signed distance from the old image's edge
    if colors:
        lab0 = cv2.cvtColor(o / 255, cv2.COLOR_RGB2LAB)
        lab1 = cv2.cvtColor(r / 255, cv2.COLOR_RGB2LAB)
        strip = max(8, round(0.02 * max(h, w)))
        falloff = max(8 * strip, round(0.3 * min(h, w)))
        a, b = round(-start), round(end)
        xs, ys = np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32)
        sides = [  # extended?, old strip (original), new strip (raw), distance beyond the edge, lines along axis
            (x0 > b + strip, lab0[:, x0 + a:x0 + a + strip], lab1[:, x0 - b - strip:x0 - b], (x0 - xs)[None, :], 1),
            (w - x1 > b + strip, lab0[:, x1 - a - strip:x1 - a], lab1[:, x1 + b:x1 + b + strip], (xs - x1)[None, :], 1),
            (y0 > b + strip, lab0[y0 + a:y0 + a + strip], lab1[y0 - b - strip:y0 - b], (y0 - ys)[:, None], 0),
            (h - y1 > b + strip, lab0[y1 - a - strip:y1 - a], lab1[y1 + b:y1 + b + strip], (ys - y1)[:, None], 0),
        ]
        for extended, old, new, dist, axis in sides:
            if not extended or old.size == 0 or new.size == 0:
                continue
            diff = np.median(old, axis=axis) - np.median(new, axis=axis)   # one Lab offset per line
            diff = np.stack([cv2.medianBlur(np.ascontiguousarray(diff[:, c:c + 1]), 5)[:, 0] for c in range(3)], -1)
            diff = cv2.GaussianBlur(diff[:, None, :], (1, 0), sigmaX=0.1, sigmaY=strip)[:, 0, :]
            fade = _smoothstep(1 - np.maximum(dist, 0) / falloff) * (dist >= start)
            lab1 = lab1 + (diff[:, None, :] if axis == 1 else diff[None, :, :]) * fade[..., None]
        r = np.clip(cv2.cvtColor(lab1.astype(np.float32), cv2.COLOR_LAB2RGB) * 255, 0, 255)
    wgt = _smoothstep((signed - start) / (end - start))
    if holes is not None and holes.any():
        # erased parts come from the generated image (a wider grown hole was tried: the model then copied
        # more of the blurred fill, worse than the faint edge this leaves)
        wgt = np.maximum(wgt, cv2.GaussianBlur(holes.astype(np.float32) / 255, (0, 0), max(1.0, overlap / 6)))
    wgt = wgt[..., None]
    return Image.fromarray(np.rint(o * (1 - wgt) + r * wgt).astype(np.uint8))


def compose(original: Image.Image, raw: Image.Image, mask: Image.Image,
            dx: float, dy: float, scale: float,
            colors: bool = False, warp: bool = False, poisson: bool = False) -> tuple[Image.Image, dict]:
    """Paste the masked area of the transformed (and optionally fixed) raw image into the original."""
    original = original.convert("RGB")
    raw = raw.convert("RGB").resize(original.size, Image.BICUBIC)
    mask_l = mask.convert("L").resize(original.size, Image.BILINEAR)
    moved = transform(raw, dx, dy, scale)
    valid = transform(Image.new("L", original.size, 255), dx, dy, scale)
    o = np.asarray(original, dtype=np.float64)
    m = np.asarray(moved, dtype=np.float64)
    mk = np.asarray(mask_l)
    keep = (mk < 20) & (np.asarray(valid) > 250)
    if warp and keep.any():
        m = fix_warp(o.astype(np.uint8), np.clip(m, 0, 255).astype(np.uint8), keep).astype(np.float64)
    if colors and keep.any():
        m = match_colors(o, m, keep).astype(np.float64)
    weight = mk / 255.0
    if poisson:
        weight = _seam_weight(o, m, mk > 127)
    weight = weight * (np.asarray(valid, dtype=np.float64) / 255.0)
    out = o * (1 - weight[..., None]) + m * weight[..., None]
    diff = float(np.abs(o - m).mean(axis=-1)[keep].mean()) if keep.any() else 0.0
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)), {"outside_diff": round(diff, 2)}


def _seam_weight(o: np.ndarray, m: np.ndarray, hard: np.ndarray) -> np.ndarray:
    """Blend weight for the edit with an optimal seam: within a band around the mask edge (SEAM_OUT px
    outside, SEAM_IN px inside) a graph cut puts the boundary where original and edit look alike, so a
    limb the model drew a bit wider or shifted continues cleanly instead of being cut off at the mask
    edge. The seam is then feathered by a few pixels."""
    if not hard.any():
        return hard.astype(np.float64)
    disk = lambda r: cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    outer = cv2.dilate(hard.astype(np.uint8), disk(SEAM_OUT))
    core = cv2.erode(hard.astype(np.uint8), disk(SEAM_IN))
    ys, xs = np.nonzero(outer)  # graph cut only over the bounding box of the band
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    crop = lambda a: np.ascontiguousarray(a[y0:y1, x0:x1])
    masks = [crop((1 - core) * 255).astype(np.uint8), crop(outer * 255).astype(np.uint8)]
    try:
        finder = cv2.detail_GraphCutSeamFinder("COST_COLOR")  # not COLOR_GRAD: that one cuts along the original's edges, leaving a double contour
        res = finder.find([crop(o).astype(np.float32), crop(m).astype(np.float32)], [(0, 0), (0, 0)], masks)
        sel = np.zeros(hard.shape, np.float32)
        sel[y0:y1, x0:x1] = (res[1].get() if hasattr(res[1], "get") else res[1]) > 0
    except cv2.error:
        sel = hard.astype(np.float32)
    w = cv2.GaussianBlur(sel, (0, 0), SEAM_FEATHER)
    return np.where(core > 0, 1.0, np.where(outer > 0, w, 0.0)).astype(np.float64)
