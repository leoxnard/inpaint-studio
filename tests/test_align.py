from pathlib import Path

import cv2
import numpy as np
from PIL import Image

import align

SRC = Path.home() / "ComfyUI-Shared/input/B491A4F1-7B92-4FA5-AA43-681AE15CB30C_1_105_c.jpeg"


def _setup(dx, dy, scale):
    if SRC.exists():
        orig = Image.open(SRC).convert("RGB").resize((1344, 736))
    else:  # deterministic texture fallback
        rng = np.random.default_rng(0)
        orig = Image.fromarray((rng.random((736, 1344, 3)) * 255).astype(np.uint8)).resize((1344, 736))
    raw = align.transform(orig, dx, dy, scale)  # pretend the edit model shifted the picture
    mask = Image.new("L", orig.size, 0)
    mask.paste(255, (560, 200, 800, 700))  # a "person" in the middle
    return orig, raw, mask


def test_estimate_recovers_shift():
    orig, raw, mask = _setup(9, -6, 1.0)
    est = align.estimate(orig, raw, mask)
    assert abs(est["dx"] + 9) <= 3 and abs(est["dy"] - 6) <= 3 and est["scale"] == 1.0


def test_estimate_recovers_scale_and_shift():
    orig, raw, mask = _setup(5, 4, 1.02)
    est = align.estimate(orig, raw, mask)
    assert abs(est["scale"] - 1 / 1.02) < 0.012


def test_compose_reduces_outside_difference():
    orig, raw, mask = _setup(9, -6, 1.0)
    _, before = align.compose(orig, raw, mask, 0, 0, 1.0)
    est = align.estimate(orig, raw, mask)
    out, after = align.compose(orig, raw, mask, est["dx"], est["dy"], est["scale"])
    assert after["outside_diff"] < before["outside_diff"] / 2
    assert out.size == orig.size


def _scene(w=320, h=240):
    rng = np.random.default_rng(0)
    base = cv2.GaussianBlur(rng.uniform(0, 255, (h, w, 3)).astype(np.float32), (0, 0), 3)
    return np.clip((base - base.mean()) * 4 + 128, 0, 255).astype(np.uint8)


def test_fixes_undo_warp_and_colour_drift():
    o = _scene()
    h, w = o.shape[:2]
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    bend = 2.5 * np.sin(gx / w * np.pi)  # smooth local distortion, up to 2.5 px
    raw = cv2.remap(o, gx + bend, gy + bend * 0.5, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
    raw = np.clip(raw.astype(np.float32) * 1.15 + np.array([12, 0, -10]), 0, 255).astype(np.uint8)  # brighter, warmer
    mask = np.zeros((h, w), np.uint8)
    mask[90:150, 130:200] = 255
    imgs = [Image.fromarray(a) for a in (o, raw, mask)]
    inside = mask > 0

    def err(**kw):
        out, _ = align.compose(*imgs, 0, 0, 1.0, **kw)
        return float(np.abs(np.asarray(out, np.float32) - o)[inside].mean())

    plain = err()
    assert err(colors=True) < plain * 0.75
    assert err(colors=True, warp=True) < plain * 0.3
    assert err(colors=True, warp=True, poisson=True) < plain
