"""Draw a venture carousel: 3-5 slides, 1080x1350 JPEG each.

The layout follows the funding-announcement carousels this account was asked
to match:

1. Cover: a full-bleed photograph (no people -- see venture.py), the company
   name in green and the action in white, a one-line tagline, and an
   optional "powered by" credit beside the 404 wordmark.
2. The number: black, with the figure set huge on a white band and its
   label run vertically in a black box beside it.
3-5. Statements: the cover photograph sunk almost to black, an eyebrow, and
   a short statement with its key phrases in green.

Only the cover costs an image generation; the rest is type, so a five-slide
carousel costs the same as a single card.
"""

import base64
import io
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image

from . import cards

automate = cards.automate

W, H = automate.CANVAS_W, automate.CANVAS_H
M = 64                                    # side margin
GREEN = "#A6E36E"
INK = "#050607"
BOLD = automate.HEADLINE_FONT_STACK
TEXT = automate.BODY_FONT_STACK

BRAND_DIR = Path(__file__).resolve().parent.parent / "brands"
# brand key -> (logo file or None for the drawn 404 tile, name as printed)
BRANDS = {
    "404": (None, "404"),
    "aethvra": ("aethvra.png", "aethvra"),
    "editsz": ("editsz.png", "editsz"),
}


# --- text helpers ----------------------------------------------------------
def _w(text: str, key: str, size: float) -> float:
    return automate._run_width(text, key, size)


def _wrap_runs(words, key: str, size: float, max_width: float):
    """Wrap words (each a list of (text, colour) segments) to `max_width`."""
    space = _w(" ", key, size)
    lines, line, used = [], [], 0.0
    for word in words:
        width = sum(_w(t, key, size) for t, _ in word)
        advance = width + (space if line else 0.0)
        if line and used + advance > max_width:
            lines.append(line)
            line, used = [word], width
        else:
            line.append(word)
            used += advance
    if line:
        lines.append(line)
    return lines


def _runs_to_words(runs: list[tuple[str, str]]):
    """Split coloured runs on spaces only, keeping colour changes mid-word."""
    words, current = [], []
    for text, colour in runs:
        for i, chunk in enumerate(text.split(" ")):
            if i > 0 and current:
                words.append(current)
                current = []
            if chunk:
                current.append((chunk, colour))
    if current:
        words.append(current)
    return words


def _words(text: str, colour: str):
    return _runs_to_words([(text, colour)])


def _fit(runs, key, base, minimum, max_width, max_lines):
    """Largest size from `base` down whose wrap fits in `max_lines`."""
    size = base
    while size > minimum:
        lines = _wrap_runs(runs, key, size, max_width)
        if len(lines) <= max_lines:
            return size, lines
        size -= 2
    return minimum, _wrap_runs(runs, key, minimum, max_width)


def _line_svg(line, x, y, size, family, colours) -> str:
    spans = []
    for i, word in enumerate(line):
        for j, (text, colour) in enumerate(word):
            lead = " " if i and not j else ""
            spans.append(f'<tspan fill="{colours[colour]}">'
                         f'{escape(lead + text)}</tspan>')
    return (f'<text x="{x}" y="{y:.1f}" xml:space="preserve" '
            f'font-family="{family}" font-size="{size:.1f}">{"".join(spans)}</text>')


# --- shared pieces ---------------------------------------------------------
def _png_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def _tile_404(x: float, y: float, size: float) -> str:
    return (f'<rect x="{x:.0f}" y="{y:.0f}" width="{size:.0f}" height="{size:.0f}" '
            f'rx="{size * 0.27:.0f}" fill="#FFFFFF"/>'
            f'<text x="{x + size / 2:.1f}" y="{y + size * 0.64:.1f}" '
            f'text-anchor="middle" font-family="{BOLD}" '
            f'font-size="{size * 0.38:.1f}" letter-spacing="-0.8" '
            f'fill="#0A0B0D">404</text>')


def _footer(brand: str | None, y: float = 1290) -> str:
    """'powered by <logo> <name>' on the left, the 404 wordmark on the right."""
    out = (f'<text x="{W - M}" y="{y}" text-anchor="end" font-family="{BOLD}" '
           f'font-size="50" letter-spacing="-1" fill="#FFFFFF">404</text>')
    if not brand:
        return out
    logo_file, name = BRANDS[brand]
    label = "powered by"
    lx = M
    out += (f'<text x="{lx}" y="{y}" font-family="{BOLD}" font-size="36" '
            f'fill="{GREEN}">{label}</text>')
    lx += _w(label, "bold", 36) + 16
    icon = 44
    if logo_file:
        out += (f'<image href="{_png_uri(BRAND_DIR / logo_file)}" x="{lx:.0f}" '
                f'y="{y - icon + 6:.0f}" width="{icon}" height="{icon}"/>')
    else:
        out += _tile_404(lx, y - icon + 6, icon)
    lx += icon + 12
    out += (f'<text x="{lx:.0f}" y="{y}" font-family="{BOLD}" font-size="36" '
            f'fill="#FFFFFF">{escape(name)}</text>')
    return out


def _svg(body: str, defs: str = "") -> str:
    return (f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
            f'xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink">'
            f'<defs>{defs}<style>{automate.embed_font_css()}</style></defs>'
            f'<rect width="{W}" height="{H}" fill="{INK}"/>{body}</svg>')


def _photo(image, opacity: float = 1.0) -> str:
    if not image:
        return f'<rect width="{W}" height="{H}" fill="#14231C"/>'
    b64, mime = image
    return (f'<image href="data:{mime};base64,{b64}" x="0" y="0" width="{W}" '
            f'height="{H}" preserveAspectRatio="xMidYMid slice" '
            f'opacity="{opacity}"/>')


# --- slides ----------------------------------------------------------------
def cover(c: dict, image, brand: str | None) -> str:
    max_w = W - 2 * M
    title = _words(c["company"], "green") + _words(c["action"], "white")
    t_size, t_lines = _fit(title, "bold", 96, 56, max_w, 2)
    s_size, s_lines = _fit(_words(c["tagline"], "white"), "text", 38, 28, max_w, 3)

    footer_y = 1290
    tag_last = footer_y - 78
    s_step = s_size * 1.28
    tag_first = tag_last - s_step * (len(s_lines) - 1)
    t_step = t_size * 1.04
    title_last = tag_first - s_size - 30
    title_first = title_last - t_step * (len(t_lines) - 1)
    fade_top = max(300, title_first - t_size - 160)

    colours = {"green": GREEN, "white": "#FFFFFF"}
    body = _photo(image)
    body += (f'<rect x="0" y="{fade_top:.0f}" width="{W}" height="{H - fade_top:.0f}" '
             f'fill="url(#fade)"/>')
    pill = c["category"]
    pw = _w(pill, "bold", 24) + 3 * len(pill) + 44   # + letter-spacing, padding
    body += (f'<rect x="{M}" y="60" width="{pw:.0f}" height="48" rx="24" '
             f'fill="#000000" fill-opacity="0.55"/>'
             f'<text x="{M + 22}" y="92" font-family="{BOLD}" font-size="24" '
             f'letter-spacing="3" fill="#FFFFFF">{escape(pill)}</text>')
    for i, line in enumerate(t_lines):
        body += _line_svg(line, M, title_first + i * t_step, t_size, BOLD, colours)
    for i, line in enumerate(s_lines):
        body += _line_svg(line, M, tag_first + i * s_step, s_size, TEXT,
                          {"white": "#EDEDED"})
    body += _footer(brand, footer_y)
    defs = ('<linearGradient id="fade" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#000" stop-opacity="0"/>'
            '<stop offset="38%" stop-color="#000" stop-opacity="0.78"/>'
            '<stop offset="100%" stop-color="#000" stop-opacity="0.96"/>'
            '</linearGradient>')
    return _svg(body, defs)


def hero_text(c: dict) -> str:
    """The figure as the slide sets it: "$15 million" -> "$15,000,000"."""
    raw = c["hero_number"].strip()
    from .venture import values
    vals = values(raw)
    if raw.startswith("$") and vals:
        v = vals[0]
        if v >= 1e9:
            return f"${v / 1e9:,.2f}".rstrip("0").rstrip(".") + "B"
        if v >= 1e3:
            return f"${v:,.0f}"
    return raw


def number(c: dict) -> str:
    body = ""
    # Quiet wireframe texture, after the instrument-panel slide in the brief.
    for r in (180, 260, 340, 420):
        body += (f'<circle cx="{W - 160}" cy="420" r="{r}" fill="none" '
                 f'stroke="#1C2B23" stroke-width="2"/>')
    for gx in range(M, W, 120):
        body += (f'<line x1="{gx}" y1="760" x2="{gx}" y2="960" '
                 f'stroke="#141D18" stroke-width="2"/>')

    c_size, c_lines = _fit(_words(c["company"], "white"), "bold", 84, 52, W - 2 * M - 90, 2)
    for i, line in enumerate(c_lines):
        body += _line_svg(line, M, 170 + i * c_size * 1.05, c_size, BOLD,
                          {"white": "#FFFFFF"})
    body += (f'<text x="{M}" y="{170 + (len(c_lines) - 1) * c_size * 1.05 + 62:.0f}" '
             f'font-family="{BOLD}" font-size="26" letter-spacing="3" '
             f'fill="{GREEN}">{escape(c["category"])}</text>')
    h_size, h_lines = _fit(_words(c["hero_line"], "white"), "bold", 64, 40,
                           W - 2 * M, 4)
    h_step = h_size * 1.16
    h_first = 640 - h_step * (len(h_lines) - 1) / 2
    for i, line in enumerate(h_lines):
        body += _line_svg(line, M, h_first + i * h_step, h_size, BOLD,
                          {"white": "#FFFFFF"})

    # The band: label box, then the figure as large as fits.
    band_y, band_h, label_w = 1020, 200, 78
    figure = hero_text(c)
    room = W - 2 * M - label_w - 56
    size = 170
    while size > 60 and _w(figure, "bold", size) > room:
        size -= 4
    label = c["hero_label"].upper()[:10]
    lsize = min(34, (band_h - 30) / max(1, len(label)) * 1.6)
    body += (f'<rect x="{M}" y="{band_y}" width="{W - 2 * M}" height="{band_h}" '
             f'fill="#FFFFFF"/>'
             f'<rect x="{M}" y="{band_y}" width="{label_w}" height="{band_h}" '
             f'fill="{INK}"/>'
             f'<text transform="translate({M + label_w / 2 + lsize * 0.36:.1f},'
             f'{band_y + band_h - 18}) rotate(-90)" font-family="{BOLD}" '
             f'font-size="{lsize:.1f}" letter-spacing="3" fill="#FFFFFF">'
             f'{escape(label)}</text>'
             f'<text x="{M + label_w + 28}" y="{band_y + band_h / 2 + size * 0.36:.1f}" '
             f'font-family="{BOLD}" font-size="{size}" letter-spacing="-3" '
             f'fill="{INK}">{escape(figure)}</text>')
    body += (f'<text x="{W - M}" y="1300" text-anchor="end" font-family="{BOLD}" '
             f'font-size="40" fill="#FFFFFF">404</text>')
    return _svg(body)


def statement(c: dict, s: dict, image, brand: str | None) -> str:
    body = _photo(image, opacity=0.30)
    body += (f'<rect width="{W}" height="{H}" fill="url(#sink)"/>')
    body += (f'<text x="{M}" y="130" font-family="{automate.SERIF_BOLD_FONT_STACK}" '
             f'font-size="44" fill="#FFFFFF">{escape(c["company"])}</text>')
    body += (f'<text x="{M}" y="196" font-family="{BOLD}" font-size="26" '
             f'letter-spacing="3" fill="#CFE8D8">{escape(s["eyebrow"].upper())}</text>')
    runs = _runs_to_words([(p["text"], p["color"]) for p in s["parts"]])
    size, lines = _fit(runs, "bold", 72, 40, W - 2 * M, 9)
    step = size * 1.18
    # Centred in the band between the eyebrow and the footer, so a short
    # statement doesn't leave the bottom half of the slide empty.
    block = step * (len(lines) - 1) + size * 0.75
    first = 250 + max(0.0, (1200 - 250 - block) / 2) + size * 0.75
    for i, line in enumerate(lines):
        body += _line_svg(line, M, first + i * step, size, BOLD,
                          {"green": GREEN, "white": "#FFFFFF"})
    body += _footer(brand)
    defs = ('<linearGradient id="sink" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#06120E" stop-opacity="0.80"/>'
            '<stop offset="100%" stop-color="#000" stop-opacity="0.95"/>'
            '</linearGradient>')
    return _svg(body, defs)


# --- render ----------------------------------------------------------------
def _jpeg(svg: str, quality: int = 90) -> bytes:
    png_path = Path(automate.SCRIPT_DIR) / "_carousel_tmp.png"
    if not automate.render_png(svg, png_path, "poster"):
        raise RuntimeError("SVG rasterization failed for a carousel slide.")
    try:
        with Image.open(png_path) as im:
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()
    finally:
        png_path.unlink(missing_ok=True)


def render(content: dict, brand: str | None, use_batch: bool = True,
           image=None) -> list[bytes]:
    """All slides as JPEG bytes, cover first. `image` skips generation (tests)."""
    automate.ensure_fonts()
    if image is None:
        got = automate.generate_backgrounds([content["image_prompt"]],
                                            use_batch=use_batch, aspect="4:5")
        image = got[0]
    if image:
        image = automate.brighten_photo(*image)
    svgs = [cover(content, image, brand), number(content)]
    svgs += [statement(content, s, image, brand) for s in content["slides"][:3]]
    return [_jpeg(s) for s in svgs]
