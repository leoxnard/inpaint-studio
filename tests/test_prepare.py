import cv2
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


def test_size_endpoint_reports_the_crop():
    from fastapi.testclient import TestClient

    import server
    c = TestClient(server.app)
    rep = c.post("/api/size", json={"width": 4000, "height": 3000, "megapixels": 0.95, "resolution": 1008,
                                    "mask_bbox": [1000, 1000, 1400, 1200], "crop_context": 0.5}).json()
    assert rep["crop"]["x"] == 800 and rep["crop"]["w"] == 800 and rep["crop"]["h"] == 512
    assert rep["crop"]["scale"] > 1   # a small crop is scaled up to the working size


def test_stitch_adds_the_originals_grain_inside_the_mask():
    rng = np.random.default_rng(5)
    base = np.full((400, 400, 3), 128, np.float32)
    grainy = Image.fromarray(np.clip(base + rng.normal(0, 8, base.shape), 0, 255).astype(np.uint8))
    m = np.zeros((400, 400), np.uint8)
    m[150:250, 150:250] = 255
    box = {"x": 0, "y": 0, "w": 400, "h": 400}
    clean = Image.new("RGB", (400, 400), (128, 128, 128))
    hp = lambda img: prepare.grain_std(np.asarray(img, np.float32), m > 127).mean()
    assert hp(prepare.stitch(grainy, clean, Image.fromarray(m), box)) < 0.5
    with_grain = prepare.stitch(grainy, clean, Image.fromarray(m), box, grain=True, grain_strength=1.0)
    target = prepare.grain_std(np.asarray(grainy, np.float32), m == 0).mean()
    assert abs(hp(with_grain) - target) < 0.15 * target
    assert (np.asarray(with_grain)[m == 0] == np.asarray(grainy)[m == 0]).all()


def test_outpaint_input_shrinks_a_huge_canvas(monkeypatch):
    import asyncio

    import server
    uploads = []

    async def load_input(name):
        return Image.new("RGB", (4000, 3000), (10, 20, 30))

    async def upload(data, name, sub):
        uploads.append(Image.open(__import__("io").BytesIO(data)).size)
        return f"{sub}/{name}"
    monkeypatch.setattr(server, "load_input", load_input)
    monkeypatch.setattr(server, "upload_to_comfy", upload)
    p = {"image": "a.png", "outpaint": {"canvas_w": 16000, "canvas_h": 12000, "x": 12000, "y": 0},
         "megapixels": 0.95, "resolution": 1008}
    asyncio.run(server.outpaint_input(p, "r1"))
    cw, ch = p["src_w"], p["src_h"]
    assert cw * ch <= 40_000_000 * 1.01 and uploads[0] == (cw, ch)
    o, (ow, oh) = p["outpaint"], p["orig_size"]
    assert o["x"] + ow == cw and o["y"] == 0          # still in the top right corner


def test_pad_masks_and_fills_erased_holes():
    img = noise(400, 300, 4)
    holes = np.zeros((150, 200), np.uint8)   # any size: stretched to the image
    holes[50:100, 50:100] = 255               # -> image px 100..200 x 100..200
    canvas, mask = prepare.pad(img, 0, 0, 600, 300, holes=Image.fromarray(holes))
    c, m, o = np.asarray(canvas), np.asarray(mask), np.asarray(img)
    assert (m[110:190, 110:190] == 255).all()                 # the hole is regenerated
    assert (m[250:290, 20:80] == 0).all()                     # far from hole and border: kept
    assert (c[250:290, 20:80] == o[250:290, 20:80]).all()
    assert np.abs(c[110:190, 110:190].astype(int) - o[110:190, 110:190]).mean() > 20   # old content is gone


def test_add_grain_gives_an_upscale_the_originals_grain_back():
    rng = np.random.default_rng(1)
    base = np.tile(np.linspace(40, 200, 96, dtype=np.float32)[None, :, None], (64, 1, 3))
    grainy = Image.fromarray(np.clip(base + rng.normal(0, 8, base.shape), 0, 255).astype(np.uint8))
    clean = Image.fromarray(base.astype(np.uint8)).resize((192, 128), Image.BICUBIC)   # a "clean" ×2 upscale
    out = prepare.add_grain(grainy, clean, seed=3, strength=1.0)
    assert out.size == clean.size
    everywhere = np.ones((64, 96), bool)
    small = lambda img: np.asarray(img.resize((96, 64), Image.BOX), np.float32)
    target = prepare.grain_std(np.asarray(grainy, np.float32), everywhere).mean()
    assert abs(prepare.grain_std(small(out), everywhere).mean() - target) < 0.15 * target
    assert prepare.add_grain(clean.resize((96, 64)), clean).tobytes() == clean.tobytes()   # nothing to add


def test_size_for_megabytes_scales_with_the_square_root_of_the_target():
    w, h = prepare.size_for_megabytes(1000, 500, 1.0, 4.5)   # 4.5 MB / (0.9 B/px) = 5 MP
    assert abs(w * h - 5e6) < 5e3 and abs(w / h - 2) < 0.01
    assert abs(prepare.size_for_megabytes(1000, 500, 1.0, 18)[0] - 2 * w) <= 1
    img = Image.new("RGB", (64, 64), (10, 20, 30))
    assert 0 < prepare.png_bytes_per_pixel(img) < 0.5   # a flat image compresses well


def test_add_grain_with_mask_only_touches_the_masked_area():
    rng = np.random.default_rng(4)
    flat = np.full((200, 200, 3), 128, np.float32)
    original = Image.fromarray(np.clip(flat + rng.normal(0, 8, flat.shape), 0, 255).astype(np.uint8))
    result = np.asarray(original).copy()
    result[50:150, 50:150] = 128                           # the edit came out clean inside the mask
    m = np.zeros((200, 200), np.uint8)
    m[50:150, 50:150] = 255
    out = np.asarray(prepare.add_grain(original, Image.fromarray(result), mask=Image.fromarray(m)), np.float32)
    assert (out[:40] == result[:40]).all()                 # outside the mask nothing changes
    assert 5 < out[60:140, 60:140].std() < 11              # inside it gets about the original's grain back


def test_texture_is_not_mistaken_for_grain():
    rng = np.random.default_rng(5)
    flat = np.full((240, 240, 3), 120, np.float32)
    flat[:, :120] += np.sin(np.arange(120) * 1.3)[None, :, None] * 40   # fine stripes (picture detail) on the left half
    original = Image.fromarray(np.clip(flat + rng.normal(0, 3, flat.shape), 0, 255).astype(np.uint8))
    assert abs(prepare.grain_std(np.asarray(original, np.float32), np.ones((240, 240), bool)).mean()
               - prepare.grain_std(np.asarray(original, np.float32)[:, 130:], np.ones((240, 110), bool)).mean()) < 0.5


def test_grain_strength_scales_the_added_grain():
    rng = np.random.default_rng(6)
    base = np.full((160, 160, 3), 128, np.float32)
    original = Image.fromarray(np.clip(base + rng.normal(0, 6, base.shape), 0, 255).astype(np.uint8))
    clean = Image.fromarray(base.astype(np.uint8))
    ev = np.ones((160, 160), bool)
    full = prepare.grain_std(np.asarray(prepare.add_grain(original, clean, strength=1.0), np.float32), ev).mean()
    half = prepare.grain_std(np.asarray(prepare.add_grain(original, clean, strength=0.5), np.float32), ev).mean()
    assert abs(half / full - 0.5) < 0.1
    assert prepare.add_grain(original, clean, strength=0).tobytes() == clean.tobytes()


@pytest.mark.parametrize("blur", [0.6, 2.0])
def test_add_grain_keeps_the_grain_size(blur):
    # the old band model capped coarse grain and made it fine pixel noise; the spectrum must survive an upscale
    rng = np.random.default_rng(2)
    base = np.tile(np.linspace(60, 190, 256, dtype=np.float32)[None, :, None], (192, 1, 3))
    noise = cv2.GaussianBlur(rng.standard_normal(base.shape).astype(np.float32), (0, 0), blur)
    grainy = np.clip(base + noise / noise.std() * 6, 0, 255).astype(np.uint8)
    clean = Image.fromarray(base.astype(np.uint8)).resize((512, 384), Image.BICUBIC)
    out = np.asarray(prepare.add_grain(Image.fromarray(grainy), clean, seed=3).resize((256, 192), Image.BOX), np.float32)
    everywhere = np.ones((192, 256), bool)
    spectrum = lambda img: prepare._spectrum(prepare.grain_tiles(img, everywhere)[0]).mean(0)
    f = np.fft.fftfreq(prepare.GRAIN_TILE)
    rad = np.hypot(*np.meshgrid(f, f, indexing="ij"))
    want, got = spectrum(grainy.astype(np.float32)), spectrum(out)
    centroid = lambda p: (p * rad).sum() / p.sum()      # the grain size, as a mean frequency
    assert abs(centroid(got) / centroid(want) - 1) < 0.15, (centroid(got), centroid(want))
    grain = ~prepare._tile_basis()[2]                   # and its strength (slower waves are shading, left out)
    assert abs(got[grain].sum() / want[grain].sum() - 1) < 0.2
