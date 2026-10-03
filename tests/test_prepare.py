import numpy as np
import pytest
from PIL import Image

import prepare


def noise(w, h, seed=0):
    return Image.fromarray(np.random.default_rng(seed).integers(0, 255, (h, w, 3), dtype=np.uint8))


def test_mask_bbox():
    m = np.zeros((100, 200), np.uint8)
    m[10:20, 30:50] = 255
    assert prepare.mask_bbox(Image.fromarray(m)) == (30, 10, 50, 20)
    assert prepare.mask_bbox(Image.new("L", (10, 10))) is None


def test_crop_box_pads_by_context_and_keeps_a_minimum_size():
    box = prepare.crop_box((1000, 1000, 1400, 1200), 4000, 3000, context=0.5, min_side=0)
    assert box == {"x": 800, "y": 900, "w": 800, "h": 400}
    box = prepare.crop_box((1000, 1000, 1010, 1010), 4000, 3000, context=0.5)
    assert box["w"] == box["h"] == prepare.MIN_CROP
    assert box["x"] <= 1000 and box["x"] + box["w"] >= 1010


def test_crop_box_stays_inside_the_image():
    box = prepare.crop_box((0, 0, 50, 50), 4000, 3000)
    assert box["x"] == 0 and box["y"] == 0
    box = prepare.crop_box((3990, 2990, 4000, 3000), 4000, 3000)
    assert box["x"] + box["w"] == 4000 and box["y"] + box["h"] == 3000
    small = prepare.crop_box((10, 10, 20, 20), 300, 200)   # image smaller than the minimum crop
    assert small == {"x": 0, "y": 0, "w": 300, "h": 200}


def test_stitch_keeps_original_pixels_outside_the_mask():
    orig = noise(800, 600, 1)
    m = np.zeros((600, 800), np.uint8)
    m[250:350, 350:450] = 255
    mask = Image.fromarray(m)
    box = prepare.crop_box(prepare.mask_bbox(mask), 800, 600, min_side=256)
    result = Image.new("RGB", (1000, 1000), (255, 0, 0))   # an edit at working size (any size)
    out = prepare.stitch(orig, result, mask, box, feather=0)
    o, r = np.asarray(orig), np.asarray(out)
    assert out.size == orig.size
    assert (r[m == 0] == o[m == 0]).all()
    assert (r[m == 255] == [255, 0, 0]).all()


def test_stitch_with_feather_changes_nothing_far_from_the_mask():
    orig = noise(800, 600, 2)
    m = np.zeros((600, 800), np.uint8)
    m[250:350, 350:450] = 255
    out = prepare.stitch(orig, Image.new("RGB", (64, 64), (0, 255, 0)), Image.fromarray(m),
                         prepare.crop_box((350, 250, 450, 350), 800, 600, min_side=256), feather=8)
    far = np.ones((600, 800), bool)
    far[200:400, 300:500] = False
    assert (np.asarray(out)[far] == np.asarray(orig)[far]).all()


def test_pad_places_the_image_and_masks_the_new_area():
    img = noise(400, 300, 3)
    canvas, mask = prepare.pad(img, 0, 50, 600, 400)
    c, m = np.asarray(canvas), np.asarray(mask)
    assert canvas.size == (600, 400) and mask.size == (600, 400)
    assert (c[50:350, 0:400] == np.asarray(img)).all()          # old pixels unchanged
    assert (m[:50] == 255).all() and (m[350:] == 255).all() and (m[:, 400:] == 255).all()
    assert (m[50 + 64:350 - 64, 0:400 - 64] == 0).all()           # inside, away from the extended sides (fade ≤ 2 × overlap)
    assert m[200, 0] == 0                                         # left edge was not extended: no overlap there
    assert m[200, 399] > 0                                        # overlap into the image on the right


def test_pad_rejects_an_image_outside_the_canvas():
    with pytest.raises(ValueError):
        prepare.pad(noise(400, 300), 300, 0, 600, 400)
