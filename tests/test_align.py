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


def test_whole_image_colours_fix_the_drift_but_keep_the_edit():
    import numpy as np
    from PIL import Image

    import align
    rng = np.random.default_rng(2)
    o = np.asarray(Image.fromarray(rng.integers(60, 190, (120, 160, 3), dtype=np.uint8)).resize((640, 480), Image.BICUBIC), float)
    edit = o * 0.85                                        # the model made everything darker ...
    edit[180:300, 260:380] = [200, 30, 30]                 # ... and painted a red square (the asked-for change)
    out, stats = align.compose(Image.fromarray(o.astype(np.uint8)), Image.fromarray(edit.astype(np.uint8)), None,
                               0, 0, 1.0, colors=True)
    out = np.asarray(out, float)
    assert np.abs(out[:150] - o[:150]).mean() < 4          # the drift is gone where nothing was asked for
    assert out[240, 320, 0] > 150 and out[240, 320, 1] < 80   # the red square stays red


def test_whole_image_compose_without_fixes_is_the_edit():
    import numpy as np
    from PIL import Image

    import align
    rng = np.random.default_rng(3)
    o = Image.fromarray(rng.integers(0, 255, (64, 64, 3), dtype=np.uint8))
    e = Image.fromarray(rng.integers(0, 255, (64, 64, 3), dtype=np.uint8))
    out, _ = align.compose(o, e, None, 0, 0, 1.0)
    assert np.abs(np.asarray(out, int) - np.asarray(e, int)).max() <= 1


def test_upscale_colours_fix_the_drift_at_full_size():
    import numpy as np
    from PIL import Image

    import align
    rng = np.random.default_rng(8)
    o = Image.fromarray(rng.integers(60, 190, (60, 80, 3), dtype=np.uint8)).resize((160, 120), Image.BICUBIC)
    up = np.asarray(o.resize((640, 480), Image.BICUBIC), float) + [-14, -10, 6]   # a ×4 upscale, darker and bluer
    out, stats = align.match_colors_scaled(o, Image.fromarray(np.clip(up, 0, 255).astype(np.uint8)))
    assert out.size == (640, 480)
    back = np.asarray(out.resize((160, 120), Image.BOX), float)
    assert np.abs(back - np.asarray(o, float)).mean() < 3 and stats["outside_diff"] < stats["unaligned_diff"]


def test_transform_stretch_and_corners():
    img = Image.new("L", (100, 80), 255)
    # stretch x only: the left and right edges move in, top and bottom stay covered
    out = np.asarray(align.transform(img, 0, 0, 1.0, sx=0.9))
    assert out[40, 2] == 0 and out[40, 50] == 255 and out[2, 50] == 255
    # one corner moved: only that corner of the frame is uncovered
    out = np.asarray(align.transform(img, 0, 0, 1.0, corners=[[10, 10], [0, 0], [0, 0], [0, 0]]))
    assert out[2, 2] == 0 and out[2, 97] == 255 and out[77, 2] == 255
    # no stretch, no corners: the plain affine path
    assert np.array_equal(np.asarray(align.transform(img, 3, 0, 1.0, corners=[[0, 0]] * 4)), np.asarray(align.transform(img, 3, 0, 1.0)))


def test_compose_force_wins_over_the_seam():
    o = np.full((64, 64, 3), 100, np.uint8)
    raw = np.full((64, 64, 3), 200, np.uint8)
    mask = np.zeros((64, 64), np.uint8)
    mask[16:48, 16:48] = 255
    force = np.zeros((64, 64), np.int8)
    force[16:48, 40:48] = -1   # erased afterwards
    force[4:10, 4:10] = 1      # added afterwards
    out, _ = align.compose(Image.fromarray(o), Image.fromarray(raw), Image.fromarray(mask), 0, 0, 1.0, poisson=True, force=force)
    a = np.asarray(out)
    assert a[32, 45, 0] < 110 and a[7, 7, 0] > 190 and a[32, 24, 0] > 190
