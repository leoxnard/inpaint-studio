"""Add title: the app writes a title into a finished picture as real type (not the image model's lettering).

A layer is {text, font, size, x, y, color, shadow}: font is a name from FONTS, size the font size in % of the image
width, x / y the centre of the text in fractions of the image, color "#rrggbb". `layout` fills what is missing
(position from `position`, colour from the picture), `render` draws it. Pure functions, unit-tested; the server
renders the layer in post-processing (server.post_process), so it stays editable and the untouched image is kept.
"""
from __future__ import annotations

import functools
import glob
import io
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIRS = ("/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts",
             str(Path.home() / "Library/Fonts"))
# macOS faces that read well as a title; the language model of Prompt ideas picks from these names
FONTS = ("Futura Bold", "Futura Medium", "Futura Condensed ExtraBold", "Didot Regular", "Didot Bold",
         "Avenir Next Demi Bold", "Avenir Next Ultra Light", "Bodoni 72 Book", "Bodoni 72 Bold", "Gill Sans SemiBold",
         "Gill Sans Light", "Helvetica Neue Condensed Black", "Helvetica Neue Thin", "Optima Regular", "Copperplate Bold",
         "Baskerville SemiBold Italic", "Big Caslon Medium", "Palatino Italic", "Hoefler Text Black", "Cochin Italic",
         "Rockwell Bold", "American Typewriter Bold", "Impact Regular", "Phosphate Solid", "Snell Roundhand Bold",
         "Savoye LET Plain", "Bradley Hand Bold", "Marker Felt Wide", "Chalkduster Regular", "Trattatello Regular")
DEFAULT_FONT = "Futura Bold"
POSITIONS = ("auto", "top", "center", "bottom")
SIZES = {"small": 6.0, "medium": 9.0, "large": 13.0}   # font size in % of the image width
DEFAULT_SIZE = SIZES["medium"]
ROOM = {"top": "across the top", "center": "in the middle", "bottom": "across the bottom"}
Y = {"top": 0.14, "center": 0.5, "bottom": 0.86}
MAX_WIDTH = 0.9      # a longer title is set smaller so it fits
MIN_CONTRAST = 4.5   # WCAG contrast ratio between the text and the picture behind it


@functools.cache
def font_files() -> dict[str, tuple[str, int]]:
    """FONTS that exist on this Mac: name -> (file, face index in the collection)."""
    found: dict[str, tuple[str, int]] = {}
    for d in FONT_DIRS:
        for f in sorted(glob.glob(f"{d}/*.tt[cf]") + glob.glob(f"{d}/*.otf")):
            for i in range(32):
                try:
                    family, style = ImageFont.truetype(f, 10, index=i).getname()
                except (OSError, ValueError):
                    break
                name = f"{family} {style}"
                if name in FONTS and name not in found:
                    found[name] = (f, i)
    return found


def fonts() -> list[str]:
    files = font_files()
    return [n for n in FONTS if n in files]


def load_font(name: str, px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    files = font_files()
    path, index = files.get(name) or files.get(DEFAULT_FONT) or (None, 0)
    if path is None:
        return ImageFont.load_default(px)
    return ImageFont.truetype(path, max(4, px), index=index)


def prompt_note(position: str) -> str:
    """Added to the image prompt: no lettering by the model, and room for the title where it will go."""
    room = f" Leave a calm, plain area {ROOM[position]} of the picture where a title can be added later." \
        if position in ROOM else ""
    return f"{room} No text, letters or logos in the picture.".strip()


def _fit(img: Image.Image, layer: dict) -> tuple[Any, tuple[int, int], tuple[int, int]]:
    """The font at the layer's size (smaller when the text would be wider than MAX_WIDTH), the size of the text's
    ink box in px and that box's offset from the drawing origin (so x / y can be its exact centre)."""
    w = img.width
    px = round(w * float(layer.get("size") or DEFAULT_SIZE) / 100)
    draw = ImageDraw.Draw(img)
    for _ in range(40):
        font = load_font(layer.get("font") or DEFAULT_FONT, px)
        l, t, r, b = draw.multiline_textbbox((0, 0), layer["text"], font=font, align="center")
        if r - l <= w * MAX_WIDTH or px <= 6:
            break
        px = int(px * 0.92)
    return font, (r - l, b - t), (l, t)


def box(img: Image.Image, layer: dict) -> tuple[float, float]:
    """Width and height of the title's ink box as fractions of the picture (the editor's snap positions)."""
    if not layer.get("text"):
        return 0.0, 0.0
    _, (tw, th), _ = _fit(img, layer)
    return tw / img.width, th / img.height


@functools.lru_cache(maxsize=64)
def sample(name: str) -> bytes:
    """A PNG of the font's own name set in that font (the editor's font list; browsers cannot pick a face by name)."""
    font = load_font(name, 30)
    draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    l, t, r, b = draw.textbbox((0, 0), name, font=font)
    img = Image.new("RGBA", (r - l + 8, b - t + 8), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((4 - l, 4 - t), name, font=font, fill=(150, 150, 150, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _detail(img: Image.Image) -> tuple[np.ndarray, float]:
    """Edge strength of a small grey copy (what makes text hard to read) and its scale to the image."""
    s = min(1.0, 256 / max(img.size))
    g = np.asarray(img.convert("L").resize((max(1, round(img.width * s)), max(1, round(img.height * s)))), np.float32)
    gy, gx = np.gradient(g)
    return np.hypot(gx, gy), s


def calm_y(img: Image.Image, box: tuple[int, int]) -> float:
    """The height (centre, fraction) where a centred box of this size covers the least detail."""
    edges, s = _detail(img)
    h, w = edges.shape
    bw, bh = max(1, round(box[0] * s)), max(1, round(box[1] * s))
    x0 = max(0, (w - bw) // 2)
    cols = edges[:, x0:x0 + bw].mean(axis=1)
    sums = np.convolve(cols, np.ones(bh) / bh, mode="valid")   # mean over each band of bh rows
    margin = max(1, round(h * 0.06))
    lo, hi = margin, max(margin + 1, len(sums) - margin)
    top = lo + int(np.argmin(sums[lo:hi])) if hi > lo else int(np.argmin(sums))
    return (top + bh / 2) / h


def _lum(c: np.ndarray) -> float:
    c = c / 255.0
    c = np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return float(0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2])


def contrast(a: np.ndarray, b: np.ndarray) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def pick_color(img: Image.Image, box: tuple[int, int, int, int]) -> tuple[str, bool]:
    """A colour of the picture's own palette that stands out from what is behind the text, measured against its
    darkest and its brightest part (a box over dark and light areas needs both): the most saturated one with enough
    contrast. Without one: white or black, whichever is better, plus a shadow (the second value)."""
    rgb = img.convert("RGB")
    px = np.asarray(rgb.crop(box), np.float32).reshape(-1, 3)
    lum = px @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    lo, hi = np.percentile(lum, 10), np.percentile(lum, 90)
    dark, light = px[lum <= lo].mean(axis=0), px[lum >= hi].mean(axis=0)
    worst = lambda c: min(contrast(c, dark), contrast(c, light))
    small = rgb.copy()
    small.thumbnail((128, 128))
    pal = np.asarray(small.quantize(8).convert("RGB"), np.float32).reshape(-1, 3)
    cols = list(np.unique(pal.round(), axis=0))
    good = [c for c in cols if worst(c) >= MIN_CONTRAST]
    hexed = lambda c: "#{:02x}{:02x}{:02x}".format(*(int(round(v)) for v in c))
    if good:
        return hexed(max(good, key=lambda c: float(c.max() - c.min()))), False
    for c in sorted(cols, key=worst, reverse=True)[:3]:   # the best palette colours pushed to white or black
        mean = (dark + light) / 2
        end = np.array([255.0, 255, 255]) if _lum(mean) < 0.4 else np.zeros(3)
        for t in np.linspace(0, 0.8, 9):
            if worst(c * (1 - t) + end * t) >= MIN_CONTRAST:
                return hexed(c * (1 - t) + end * t), False
    white, black = np.full(3, 255.0), np.zeros(3)
    return ("#ffffff" if worst(white) >= worst(black) else "#000000"), True


def layout(img: Image.Image, layer: dict) -> dict:
    """The layer with everything filled in: font and size defaults, x / y from `position` (auto: the calmest
    band), the colour from the picture. Values the layer already has are kept (an edited layer stays as it is)."""
    out = {"font": DEFAULT_FONT, "size": DEFAULT_SIZE, "shadow": False, **{k: v for k, v in layer.items() if v is not None}}
    if out.get("font") not in fonts():
        out["font"] = DEFAULT_FONT
    if isinstance(out["size"], str):
        out["size"] = SIZES.get(out["size"], DEFAULT_SIZE)
    if not out.get("text"):
        return out
    _, (tw, th), _ = _fit(img, out)
    if out.get("x") is None or out.get("y") is None:
        pos = out.get("position") or "auto"
        out["x"] = 0.5
        out["y"] = Y[pos] if pos in Y else calm_y(img, (tw, th))
    if not out.get("color"):
        cx, cy = out["x"] * img.width, out["y"] * img.height
        box = (max(0, int(cx - tw / 2)), max(0, int(cy - th / 2)),
               min(img.width, int(cx + tw / 2) + 1), min(img.height, int(cy + th / 2) + 1))
        out["color"], shadow = pick_color(img, box)
        out["shadow"] = bool(out.get("shadow")) or shadow
    return out


def render(img: Image.Image, layer: dict) -> Image.Image:
    """The picture with the title drawn on it (centred on x / y)."""
    if not layer.get("text"):
        return img
    base = img.convert("RGBA")
    font, (tw, th), (l, t) = _fit(base, layer)
    # x / y is the centre of the ink box, not of the line (anchor "mm" sits between ascender and descender)
    pos = (layer["x"] * base.width - l - tw / 2, layer["y"] * base.height - t - th / 2)
    text = Image.new("RGBA", base.size, (0, 0, 0, 0))
    if layer.get("shadow"):
        shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
        off = max(1, round(th * 0.04))
        ImageDraw.Draw(shadow).multiline_text((pos[0] + off, pos[1] + off), layer["text"], font=font,
                                              fill=(0, 0, 0, 150), align="center")
        text = Image.alpha_composite(text, shadow.filter(ImageFilter.GaussianBlur(max(1, th * 0.05))))
    ImageDraw.Draw(text).multiline_text(pos, layer["text"], font=font, fill=layer.get("color") or "#ffffff",
                                        align="center")
    out = Image.alpha_composite(base, text)
    return out if img.mode == "RGBA" else out.convert(img.mode)
