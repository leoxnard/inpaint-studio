from pathlib import Path

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
