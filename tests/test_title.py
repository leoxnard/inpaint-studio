"""Add title: layout (position, colour) and rendering of the title layer."""
import numpy as np
from PIL import Image

import title


def _busy_top() -> Image.Image:
    """Noise in the upper half, a plain dark lower half."""
    rng = np.random.default_rng(0)
    a = np.full((400, 400, 3), 30, np.uint8)
    a[:200] = rng.integers(0, 255, (200, 400, 3), dtype=np.uint8)
    return Image.fromarray(a)


def test_auto_position_finds_the_calm_area():
    l = title.layout(_busy_top(), {"text": "Late Night Jazz", "position": "auto"})
    assert l["y"] > 0.5 and l["x"] == 0.5


def test_fixed_position_size_name_and_defaults():
    l = title.layout(_busy_top(), {"text": "A", "position": "top", "size": "large", "font": "No Such Font"})
    assert l["y"] == title.Y["top"] and l["size"] == title.SIZES["large"] and l["font"] == title.DEFAULT_FONT


def test_kept_values_are_not_recomputed():
    l = title.layout(_busy_top(), {"text": "A", "x": 0.2, "y": 0.3, "color": "#123456"})
    assert (l["x"], l["y"], l["color"]) == (0.2, 0.3, "#123456")


def test_colour_has_contrast_with_the_background():
    img = Image.new("RGB", (300, 300), (20, 20, 40))
    c, shadow = title.pick_color(img, (0, 0, 300, 300))
    rgb = np.array([int(c[i:i + 2], 16) for i in (1, 3, 5)], np.float32)
    assert title.contrast(rgb, np.array([20, 20, 40], np.float32)) >= title.MIN_CONTRAST and not shadow


def test_render_draws_in_the_layer_colour_and_keeps_size_and_mode():
    img = Image.new("RGB", (300, 200), (0, 0, 0))
    out = title.render(img, {"text": "HELLO", "font": title.DEFAULT_FONT, "size": 15, "x": 0.5, "y": 0.5,
                             "color": "#ff0000"})
    a = np.asarray(out)
    assert out.size == img.size and out.mode == "RGB" and (a[..., 0] > 200).sum() > 100
    assert title.render(img, {"text": ""}) is img


def test_long_text_is_set_smaller_to_fit():
    img = Image.new("RGB", (200, 200))
    font, (w, _), _ = title._fit(img, {"text": "A very very long playlist title indeed", "size": 20})
    assert w <= 200 * title.MAX_WIDTH


def test_prompt_note():
    assert "across the top" in title.prompt_note("top") and "No text" in title.prompt_note("auto")


def test_generate_prompt_gets_the_room_note():
    import graphs
    p = graphs.edit_prompt({"prompt": "A record player", "task": "generate", "title": {"text": "Jazz", "position": "top"}})
    assert p.startswith("A record player") and "across the top" in p and "Jazz" not in p
    assert "No text" not in graphs.edit_prompt({"prompt": "A record player", "task": "generate"})


def test_text_is_centred_on_its_ink_box():
    img = Image.new("RGB", (400, 400))
    out = title.render(img, {"text": "gyMH", "font": title.DEFAULT_FONT, "size": 20, "x": 0.5, "y": 0.5, "color": "#ffffff"})
    ys, xs = np.nonzero(np.asarray(out.convert("L")) > 128)
    assert abs((ys.min() + ys.max()) / 2 - 200) <= 2 and abs((xs.min() + xs.max()) / 2 - 200) <= 2
    w, h = title.box(img, {"text": "gyMH", "size": 20})
    assert 0 < h < w < 1


def test_font_sample_is_a_png():
    assert title.sample(title.DEFAULT_FONT)[:8] == b"\x89PNG\r\n\x1a\n"


def test_max_size_makes_the_widest_line_span_the_picture():
    img = Image.new("RGB", (400, 300), "black")
    for text in ("Late Night Jazz", "Late\nNight Jazz"):
        m = title.max_size(img, {"text": text})
        _, (w, _), _ = title._fit(img, {"text": text, "size": m})
        assert 0.97 * 400 <= w <= 400
    assert title.max_size(img, {"text": "Late\nNight Jazz"}) > title.max_size(img, {"text": "Late Night Jazz"})


def test_oversized_title_stays_inside_the_picture():
    img = Image.new("RGB", (200, 100), "black")
    out = title.render(img, {"text": "WIDE TITLE", "size": 60, "x": 0.9, "y": 0.5, "color": "#ffffff"})
    px = np.asarray(out.convert("L"))
    assert px[:, 0].max() > 0 and px[:, -1].max() > 0


def _ink_box(layer, size=(400, 300)):
    img = Image.new("RGB", size, "black")
    out = np.asarray(title.render(img, {"font": title.DEFAULT_FONT, "x": 0.5, "y": 0.5, "color": "#ffffff", "size": 8, **layer}).convert("L"))
    ys, xs = np.where(out > 0)
    return xs.min(), xs.max(), ys.min(), ys.max()


def test_line_spacing_tracking_and_caps_change_the_box():
    base = _ink_box({"text": "ab\ncd"})
    assert _ink_box({"text": "ab\ncd", "line_spacing": 2})[3] - _ink_box({"text": "ab\ncd", "line_spacing": 2})[2] > base[3] - base[2]
    assert _ink_box({"text": "abcd", "tracking": 30})[1] - _ink_box({"text": "abcd", "tracking": 30})[0] > \
        _ink_box({"text": "abcd"})[1] - _ink_box({"text": "abcd"})[0]
    assert _ink_box({"text": "abcd", "caps": True}) != _ink_box({"text": "abcd"})


def test_alignment_puts_the_short_line_left_or_right():
    img = Image.new("RGB", (400, 300), "black")
    def short_line_x(align):
        out = np.asarray(title.render(img, {"text": "WWWWWWWW\nI", "align": align, "size": 8, "x": 0.5, "y": 0.5,
                                            "color": "#ffffff"}).convert("L"))
        rows = np.where(out.max(axis=1) > 0)[0]
        last = out[rows[-12:]]   # the bottom rows belong to the short second line
        return np.where(last.max(axis=0) > 0)[0].mean()
    assert short_line_x("left") < short_line_x("center") < short_line_x("right")


def test_opacity_and_outline():
    img = Image.new("RGB", (200, 100), "black")
    layer = {"text": "HI", "size": 30, "x": 0.5, "y": 0.5, "color": "#ffffff"}
    assert np.asarray(title.render(img, {**layer, "opacity": 50}).convert("L")).max() < 140
    thick = np.asarray(title.render(img, {**layer, "color": "#808080", "outline": 10}).convert("L"))
    assert thick.max() > 200   # the outline is the opposite end of the scale (white around mid grey)
