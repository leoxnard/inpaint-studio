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


def test_outpaint_paste_mask_fades_only_into_the_old_image():
    import numpy as np
    from PIL import Image

    import align
    m = np.zeros((100, 300), np.uint8)
    m[:, :100] = 255                       # new area on the left, old image from x = 100
    out = np.asarray(align.outpaint_paste_mask(Image.fromarray(m), 40))
    assert (out[:, :100] == 255).all()     # the new area stays fully generated
    assert out[50, 110] > 128 > out[50, 130] and (out[:, 145:] == 0).all()


def test_outpaint_align_moves_the_original_to_where_the_model_put_it():
    import numpy as np
    from PIL import Image

    import align
    rng = np.random.default_rng(1)
    big = Image.fromarray(rng.integers(0, 255, (300, 400, 3), dtype=np.uint8)).resize((800, 600), Image.BICUBIC)
    original = big.crop((0, 0, 800, 600))
    raw = align.transform(original, 0, -30, 1.0)          # the model drew everything 30 px higher
    m = np.zeros((600, 800), np.uint8)
    m[:, :200] = 255                                       # new area on the left
    moved, moved_mask, est = align.outpaint_align(original, raw, Image.fromarray(m))
    assert est["moved"] and abs(est["dy"] - 30) < 2       # the shift that maps the model image back onto the original
    a, b = np.asarray(moved, float)[100:500, 300:700], np.asarray(raw, float)[100:500, 300:700]
    assert np.abs(a - b).mean() < 6                        # the moved original now lines up with the model's image
    assert (np.asarray(moved_mask)[-20:, 300:700] == 255).all()   # uncovered bottom counts as generated
