"""
404-style stat card generator
==============================
Produces a self-contained 1080x1080 SVG (plus a rendered PNG) styled like a
"404 Media" social card:

    404                                <- wordmark, top-left
    [photo band]                       <- AI-generated with Gemini
    CONSUMER x SPENDING                <- category tag + underline
    AMERICA'S $166B BET                <- big bold all-caps headline
    Americans now spend more on        <- body paragraph, white text
    sports betting each year than      <- with blue / red / green
    they do on movies, arts...         <- highlighted phrases

Setup (once):
    pip install -r requirements.txt
    # .env next to this script:
    #   ANTHROPIC_API_KEY=sk-ant-...
    #   GEMINI_API_KEY=...

Run:
    python automate.py "topic you want a card about"
    python automate.py "topic one" "topic two" "topic three"   # one batch job
    python automate.py --no-batch "topic"                      # instant, costs 2x

Output:
    404_card.svg + 404_card.png   (or 404_card_1.*, 404_card_2.* for many topics)

Images come from Gemini's Batch API by default: every topic's image prompt is
submitted as ONE batch job, which is ~50% cheaper than realtime calls at the
cost of latency (usually a couple of minutes here, up to 24h by contract).
Use --no-batch when you want the image right now.
"""

import os
import re
import sys
import json
import time
import base64
import argparse
from pathlib import Path
from typing import NamedTuple
from xml.sax.saxutils import escape

import requests
from dotenv import load_dotenv
from anthropic import Anthropic

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

# --------------------------------------------------------------------------
# CONFIG - tweak these to fine-tune the look without touching the logic
# --------------------------------------------------------------------------
MODEL = "claude-sonnet-5"
IMAGE_MODEL = "models/gemini-3.1-flash-lite-image"
IMAGE_ASPECT_RATIO = "16:9"   # photo band is 1080x620, close enough to slice

CANVAS = 1080                 # square canvas, like the reference image
PHOTO_HEIGHT = 455            # height of the top photo band
OUTPUT_PATH = "404_card.svg"

# Batch polling. A batch job is asynchronous by design; these bounds only
# decide how long WE are willing to sit and wait before giving up.
BATCH_POLL_SECONDS = 10
BATCH_TIMEOUT_SECONDS = 30 * 60

MARGIN_X = 90                 # left margin used by every text block
LOGO_Y = 90
CATEGORY_Y = 500
HEADLINE_FONT_SIZE = 84
HEADLINE_MIN_FONT_SIZE = 46
BODY_FONT_SIZE = 48
BODY_MIN_FONT_SIZE = 38                # preferred floor (~1.2x the old size)
BODY_HARD_MIN_FONT_SIZE = 26           # absolute floor, only to avoid overflow
BODY_LINE_HEIGHT_RATIO = 1.44          # line height as a multiple of font size
BODY_CHAR_WIDTH_RATIO = 0.478          # tuned so lines fill the full text width

# Vertical rhythm below the photo. The headline is NOT placed at a fixed y:
# it hangs off the bottom of the category underline by CATEGORY_UNDERLINE_GAP
# measured to the top of its capitals, so a 2-line headline can never end up
# struck through by the underline. See layout_blocks().
SCRIM_HEIGHT = 190            # top gradient keeping the wordmark readable
CATEGORY_UNDERLINE_GAP = 26
HEADLINE_BODY_GAP = 55
BODY_BOTTOM_MARGIN = 55

# System-font stacks used if no embedded font files are found (see EMBED_FONTS
# below). 'Anton' is named explicitly because the PNG renderer loads the .ttf
# by its real family name rather than through @font-face -- see render_png().
HEADLINE_FONT_STACK = "'Anton', Impact, 'Arial Black', 'Helvetica Neue', sans-serif"
BODY_FONT_STACK = "'Helvetica Neue', Arial, sans-serif"

# --- optional: embed a real font file for a pixel-perfect headline match ---
# The script auto-downloads Anton (a bold condensed display font, close to
# the reference image's headline style) into the working folder on first
# run and embeds it directly in the SVG, so the standalone .svg looks the
# same in any browser. If the download fails (e.g. offline), it just falls
# back to the system stack above -- the auto-fit sizing logic (see
# fit_headline_sizes) stays safe either way.
#
# Body text is left on the system stack: at normal paragraph size a generic
# sans renders close enough to the reference that embedding isn't worth the
# extra fragile dependency.
EMBED_FONTS = True
FONT_FILES = {
    "headline": str(SCRIPT_DIR / "Anton-Regular.ttf"),
    "body": str(SCRIPT_DIR / "Inter-Bold.ttf"),  # optional: drop your own file here
}
FONT_DOWNLOAD_URLS = {
    "headline": "https://raw.githubusercontent.com/google/fonts/main/ofl/anton/Anton-Regular.ttf",
}


def ensure_fonts():
    """Best-effort auto-download of the headline font. Never raises --
    on any failure we just proceed with the system font fallback."""
    if not EMBED_FONTS:
        return
    for key, url in FONT_DOWNLOAD_URLS.items():
        path = FONT_FILES.get(key)
        if not path or os.path.exists(path):
            continue
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)
            print(f"Downloaded {os.path.basename(path)}")
        except Exception as e:
            print(f"Could not auto-download {path} ({e}); using system font fallback.")


COLORS = {
    "white": "#FFFFFF",
    "blue": "#4C6EF5",
    "red": "#F0533D",
    "green": "#2ECC71",
}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")


# --------------------------------------------------------------------------
# STEP 1 - ask Claude for the copy, in the exact JSON shape we need
# --------------------------------------------------------------------------
def generate_content(topic: str) -> dict:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    system = (
        "You write copy for '404 Media' style stat cards. You always reply "
        "with ONLY valid JSON, no markdown fences, no commentary, matching "
        "this exact schema:\n\n"
        "{\n"
        '  "category_left": "ONE OR TWO WORDS",\n'
        '  "category_right": "ONE OR TWO WORDS",\n'
        '  "headline": "SHORT PUNCHY ALL-CAPS HEADLINE, under 30 characters, '
        'often includes a number/stat",\n'
        '  "description": [\n'
        '    {"text": "...", "color": "white"},\n'
        '    {"text": "...", "color": "blue|red|green"},\n'
        "    ... more parts ...\n"
        "  ],\n"
        '  "image_prompt": "one or two sentences describing a photograph for '
        'an AI image model"\n'
        "}\n\n"
        "Rules for the description array:\n"
        "- Concatenating every part's text must read as ONE natural "
        "paragraph (3-4 sentences worth), so include spaces at the edges "
        "of each part where a space belongs.\n"
        "- color 'blue' = the main subject/behavior being highlighted.\n"
        "- color 'red' = whatever it's being unfavorably compared against.\n"
        "- color 'green' = the key hard number/statistic.\n"
        "- color 'white' = connective/neutral text.\n"
        "- Only highlight short, specific phrases -- not whole sentences.\n\n"
        "Rules for image_prompt:\n"
        "- Describe a real-looking editorial PHOTOGRAPH, concrete and visual, "
        "shot on a wide 16:9 frame with cinematic lighting and a moody, "
        "desaturated palette.\n"
        "- The top of the frame carries the subject; keep the composition "
        "simple enough to survive being cropped to a wide band.\n"
        "- Absolutely no text, letters, numbers, logos, watermarks or "
        "captions anywhere in the image."
    )

    example_user = "Topic: Americans spending more on sports betting than on movies, arts, museums and music combined ($166B/year)"
    example_assistant = json.dumps({
        "category_left": "CONSUMER",
        "category_right": "SPENDING",
        "headline": "AMERICA'S $166B BET",
        "description": [
            {"text": "Americans now spend more on ", "color": "white"},
            {"text": "sports betting", "color": "blue"},
            {"text": " each year than they do on ", "color": "white"},
            {"text": "movies, arts, museums, and music combined", "color": "red"},
            {"text": "—about ", "color": "white"},
            {"text": "$166 billion", "color": "green"},
            {"text": " in wagers alone.", "color": "white"},
        ],
        "image_prompt": (
            "A wide cinematic editorial photograph of a dim sportsbook floor at "
            "night: rows of glowing screens washing blue light over anonymous "
            "silhouetted spectators, shallow depth of field, desaturated moody "
            "color grade, no text or logos anywhere."
        ),
    })

    resp = client.messages.create(
        model=MODEL,
        max_tokens=900,
        system=system,
        messages=[
            {"role": "user", "content": example_user},
            {"role": "assistant", "content": example_assistant},
            {"role": "user", "content": f"Topic: {topic}"},
        ],
    )

    # skip any thinking blocks the model may emit and take the text block
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    # be defensive in case the model wraps the JSON in ```json fences anyway
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[4:] if raw.lower().startswith("json") else raw
    return json.loads(raw)


# --------------------------------------------------------------------------
# STEP 2 - generate the background photos with Gemini
# --------------------------------------------------------------------------
# Two paths, same return shape: a list (parallel to `prompts`) of
# (base64_string, mime_type) tuples, with None where generation failed.
# The batch path is the default because it is ~50% cheaper; --no-batch exists
# for when you'd rather pay full price than wait.
IMAGE_GEN_CONFIG = {
    "response_modalities": ["IMAGE"],
    "image_config": {"aspect_ratio": IMAGE_ASPECT_RATIO},
}


def _gemini_client():
    from google import genai
    return genai.Client(api_key=GEMINI_API_KEY)


def _extract_image(response):
    """Pull the first inline image out of a Gemini response, or None."""
    if not response or not response.candidates:
        return None
    for part in response.candidates[0].content.parts or []:
        if part.inline_data and part.inline_data.data:
            data = part.inline_data.data
            mime = part.inline_data.mime_type or "image/png"
            return base64.b64encode(data).decode("utf-8"), mime
    return None


def generate_backgrounds_batch(prompts: list[str]) -> list[tuple[str, str] | None]:
    """Submit every image prompt as ONE Gemini batch job, then wait for it."""
    client = _gemini_client()

    job = client.batches.create(
        model=IMAGE_MODEL,
        src=[
            {
                "contents": [{"role": "user", "parts": [{"text": p}]}],
                "config": IMAGE_GEN_CONFIG,
            }
            for p in prompts
        ],
        config={"display_name": f"404-cards-{int(time.time())}"},
    )
    print(f"Batch job submitted: {job.name} ({len(prompts)} image(s))")

    pending = {"JOB_STATE_PENDING", "JOB_STATE_QUEUED", "JOB_STATE_RUNNING"}
    started = time.time()
    while job.state.name in pending:
        if time.time() - started > BATCH_TIMEOUT_SECONDS:
            print(
                f"Batch job still {job.state.name} after "
                f"{BATCH_TIMEOUT_SECONDS // 60} min -- giving up on the photos.\n"
                f"The job is NOT cancelled; results land at {job.name}."
            )
            return [None] * len(prompts)
        time.sleep(BATCH_POLL_SECONDS)
        job = client.batches.get(name=job.name)
        print(f"  [{int(time.time() - started):>4}s] {job.state.name}")

    if job.state.name != "JOB_STATE_SUCCEEDED":
        print(f"Batch job ended as {job.state.name} ({job.error}) -- using plain backgrounds.")
        return [None] * len(prompts)

    responses = (job.dest.inlined_responses or []) if job.dest else []
    results: list[tuple[str, str] | None] = []
    for i in range(len(prompts)):
        item = responses[i] if i < len(responses) else None
        if item is None:
            results.append(None)
            continue
        if item.error:
            print(f"  image {i + 1} failed: {item.error}")
            results.append(None)
            continue
        results.append(_extract_image(item.response))
    return results


def generate_backgrounds_sync(prompts: list[str]) -> list[tuple[str, str] | None]:
    """Realtime generateContent, one call per prompt. Fast, full price."""
    client = _gemini_client()
    results = []
    for i, prompt in enumerate(prompts, 1):
        print(f"  generating image {i}/{len(prompts)} ...")
        try:
            resp = client.models.generate_content(
                model=IMAGE_MODEL, contents=prompt, config=IMAGE_GEN_CONFIG
            )
            results.append(_extract_image(resp))
        except Exception as e:
            print(f"  image {i} failed: {e}")
            results.append(None)
    return results


def generate_backgrounds(prompts: list[str], use_batch: bool):
    if not GEMINI_API_KEY:
        print("No GEMINI_API_KEY set -- using plain dark backgrounds instead.")
        return [None] * len(prompts)
    try:
        if use_batch:
            return generate_backgrounds_batch(prompts)
        return generate_backgrounds_sync(prompts)
    except Exception as e:
        print(f"Image generation failed ({e}) -- using plain dark backgrounds instead.")
        return [None] * len(prompts)


# --------------------------------------------------------------------------
# STEP 3 - word-wrap the colored description into lines of tspans
# --------------------------------------------------------------------------
# NOTE: this works at the character level (not just per-word) so that
# punctuation glued directly onto a highlighted phrase -- e.g. a red part
# "...combined" immediately followed by a white part "." with NO space --
# renders glued together with no phantom space, while words that really are
# separated by whitespace in the source still get exactly one rendered space.
def build_color_map(parts):
    """Flatten description parts into one string + (start, end, color) spans."""
    text, spans, pos = "", [], 0
    for part in parts:
        t = part["text"]
        color = part.get("color", "white")
        spans.append((pos, pos + len(t), color))
        text += t
        pos += len(t)
    return text, spans


def _color_at(spans, idx):
    for start, end, color in spans:
        if start <= idx < end:
            return color
    return "white"


def get_word_tokens(text, spans):
    """Split into whitespace-delimited tokens. Each token is a list of
    (subtext, color) runs with NO gaps between them -- so a token that
    crosses a color boundary (like 'billion.') still renders as one
    contiguous unit, just in two colors."""
    tokens = []
    for m in re.finditer(r"\S+", text):
        word = m.group()
        start = m.start()
        runs, cur_color, cur_text = [], _color_at(spans, start), ""
        for i, ch in enumerate(word):
            c = _color_at(spans, start + i)
            if c != cur_color and cur_text:
                runs.append((cur_text, cur_color))
                cur_text = ""
                cur_color = c
            cur_text += ch
        if cur_text:
            runs.append((cur_text, cur_color))
        tokens.append(runs)
    return tokens


def _token_len(token):
    return sum(len(t) for t, _ in token)


def wrap_tokens(tokens, max_chars):
    lines, current, current_len = [], [], 0
    for token in tokens:
        add_len = _token_len(token) + 1
        if current and current_len + add_len > max_chars:
            lines.append(current)
            current, current_len = [], 0
        current.append(token)
        current_len += add_len
    if current:
        lines.append(current)
    return lines


def line_to_tspans(line_tokens):
    out = []
    for i, token in enumerate(line_tokens):
        if i > 0:
            out.append(" ")  # a real space between whitespace-separated words
        for subtext, color in token:
            out.append(f'<tspan fill="{COLORS[color]}">{escape(subtext)}</tspan>')
    return "".join(out)


# --------------------------------------------------------------------------
# STEP 3b - font embedding + headline auto-fit (prevents overflow/clipping)
# --------------------------------------------------------------------------
def headline_font_available() -> bool:
    path = FONT_FILES.get("headline")
    return bool(EMBED_FONTS and path and os.path.exists(path))


def embed_font_css() -> str:
    """Return @font-face CSS for any font files that exist next to the
    script. Returns '' if none are found (system fallback is used instead).

    This is what makes the standalone .svg render correctly in a browser.
    The PNG renderer ignores @font-face entirely and loads the same .ttf
    files directly instead -- see render_png()."""
    if not EMBED_FONTS:
        return ""
    css = ""
    families = {"headline": "Headline404", "body": "Body404"}
    for key, family in families.items():
        path = FONT_FILES.get(key)
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            css += (
                f"@font-face{{font-family:'{family}';"
                f"src:url(data:font/ttf;base64,{b64}) format('truetype');}}"
            )
    return css


def font_families():
    """Pick embedded font names if the files were found, else fall back."""
    headline = (
        f"'Headline404', {HEADLINE_FONT_STACK}"
        if headline_font_available()
        else HEADLINE_FONT_STACK
    )
    body = (
        f"'Body404', {BODY_FONT_STACK}"
        if EMBED_FONTS and os.path.exists(FONT_FILES["body"])
        else BODY_FONT_STACK
    )
    return headline, body


def split_headline(text):
    """Split a long headline into at most 2 lines (by word boundaries)."""
    words = text.split()
    if len(text) <= 22 or len(words) < 2:
        return [text]
    mid = len(words) // 2
    return [" ".join(words[:mid]), " ".join(words[mid:])]


def _measure_width(text, font_size, ttf_path):
    """Exact pixel width of `text` at `font_size` using a real .ttf file
    (only works when a font file is actually available -- see fit path
    below for what happens when one isn't)."""
    from PIL import ImageFont
    font = ImageFont.truetype(ttf_path, size=int(round(font_size)))
    box = font.getbbox(text)
    return box[2] - box[0]


CONSERVATIVE_CHAR_FACTOR = 0.72  # safe even for chunky fonts like Arial Black


def _conservative_size(line, max_width, base_size, min_size):
    est_width = len(line) * base_size * CONSERVATIVE_CHAR_FACTOR
    if est_width <= max_width:
        return base_size
    return max(min_size, max_width / (len(line) * CONSERVATIVE_CHAR_FACTOR))


def fit_headline_sizes(lines, max_width, base_size, min_size=40):
    """Return a font size for each line that fits inside max_width.

    When the Anton .ttf is present we measure the line with Pillow against
    that exact file -- and because both output paths really do use it (the
    browser via @font-face, the PNG via a directly-loaded font file), the
    measurement is the truth. When it's missing, no font is knowable ahead
    of time, so we fall back to a deliberately pessimistic character-width
    estimate that stays safe even under a wide generic substitute."""
    ttf_path = FONT_FILES.get("headline")

    sizes = []
    for line in lines:
        if headline_font_available():
            width = _measure_width(line, base_size, ttf_path)
            size = base_size if width <= max_width else max(
                min_size, max_width / width * base_size
            )
        else:
            size = _conservative_size(line, max_width, base_size, min_size)
        sizes.append(size)
    return sizes


def cap_height(size):
    """Height of a capital letter above the baseline, from the real font
    when we have it, else a safe generic ratio."""
    ttf_path = FONT_FILES.get("headline")
    if headline_font_available():
        from PIL import ImageFont
        font = ImageFont.truetype(ttf_path, int(round(size)))
        ascent, _ = font.getmetrics()
        return ascent - font.getbbox("AH")[1]
    return size * 0.75


def body_max_chars(font_size):
    return max(20, int((CANVAS - 2 * MARGIN_X) / (font_size * BODY_CHAR_WIDTH_RATIO)))


class Layout(NamedTuple):
    headline_sizes: list[float]
    headline_baselines: list[float]
    body_size: float
    body_line_height: float
    body_lines: list
    body_start_y: float


def layout_card(headline_lines, tokens) -> Layout:
    """Stack the headline and the body paragraph below the category tag so
    that everything fits between the tag underline and the bottom margin.

    The headline hangs off the underline by its cap height, so it can never
    be struck through by it. If the two blocks together would still run off
    the canvas -- a 2-line headline plus a long paragraph easily does -- the
    body is stepped down first (re-wrapping at each size, since smaller text
    fits more characters per line) and the headline only after that."""
    underline_bottom = CATEGORY_Y + 23
    limit = CANVAS - BODY_BOTTOM_MARGIN
    max_width = CANVAS - 2 * MARGIN_X
    # Two tiers: shrink to the preferred floor while also shrinking the
    # headline, and only if that still overflows keep going to the hard
    # floor. Running off the bottom of the card is worse than small type.
    body_steps = list(range(BODY_FONT_SIZE, BODY_MIN_FONT_SIZE - 1, -1))
    rescue_steps = list(range(BODY_MIN_FONT_SIZE - 1, BODY_HARD_MIN_FONT_SIZE - 1, -1))

    headline_scale = 1.0
    while True:
        base = HEADLINE_FONT_SIZE * headline_scale
        sizes = fit_headline_sizes(headline_lines, max_width, base, HEADLINE_MIN_FONT_SIZE)

        y = underline_bottom + CATEGORY_UNDERLINE_GAP + cap_height(sizes[0])
        baselines = []
        for size in sizes:
            baselines.append(y)
            y += size * 1.1
        body_start = baselines[-1] + HEADLINE_BODY_GAP

        exhausted = headline_scale <= 0.6
        steps = body_steps + (rescue_steps if exhausted else [])

        for body_size in steps:
            line_height = body_size * BODY_LINE_HEIGHT_RATIO
            lines = wrap_tokens(tokens, body_max_chars(body_size))
            fits = body_start + (len(lines) - 1) * line_height <= limit
            if fits or (exhausted and body_size == steps[-1]):
                return Layout(sizes, baselines, body_size, line_height, lines, body_start)

        headline_scale -= 0.05


def render_headline(lines, sizes, baselines, headline_font):
    out = []
    for line, size, y in zip(lines, sizes, baselines):
        out.append(
            f'<text x="{MARGIN_X}" y="{y:.1f}" font-family="{headline_font}" '
            f'font-size="{size:.1f}" font-weight="900" fill="#FFFFFF">'
            f'{escape(line)}</text>'
        )
    return "\n".join(out)


# --------------------------------------------------------------------------
# STEP 4 - build the final SVG
# --------------------------------------------------------------------------
# Target mean luminance for the photo band, 0-255. The lift is ADAPTIVE:
# a dark frame gets brightened toward this, a frame that is already bright
# is left alone. A fixed multiplier blows out the highlights on the bright
# images the prompt now asks for -- pure white cleanroom, no detail.
PHOTO_TARGET_LUMA = 132
PHOTO_MAX_LIFT = 1.55     # never lift more than this, however dark the source
PHOTO_CONTRAST = 1.04
PHOTO_SATURATION = 1.10


def brighten_photo(image_b64: str, mime: str) -> tuple[str, str]:
    """Lift a dark photo toward a consistent exposure; leave bright ones be.

    Image models still return underexposed frames for editorial prompts even
    when asked for daylight, and a dark card disappears in the feed. Scaling
    by measured luminance rather than a fixed factor means both a murky
    night shot and a bright cleanroom land at a similar, usable exposure.
    """
    import base64 as _b64
    import io as _io

    from PIL import Image, ImageEnhance, ImageStat

    try:
        im = Image.open(_io.BytesIO(_b64.b64decode(image_b64))).convert("RGB")

        mean = ImageStat.Stat(im.convert("L")).mean[0]
        lift = 1.0 if mean >= PHOTO_TARGET_LUMA else min(
            PHOTO_TARGET_LUMA / max(mean, 1.0), PHOTO_MAX_LIFT
        )

        if lift > 1.01:
            im = ImageEnhance.Brightness(im).enhance(lift)
        im = ImageEnhance.Contrast(im).enhance(PHOTO_CONTRAST)
        im = ImageEnhance.Color(im).enhance(PHOTO_SATURATION)

        print(f"  photo luma {mean:.0f} -> lift x{lift:.2f}")
        buf = _io.BytesIO()
        im.save(buf, format="JPEG", quality=90, optimize=True)
        return _b64.b64encode(buf.getvalue()).decode("utf-8"), "image/jpeg"
    except Exception as e:
        # A failed enhance must never cost us the whole card.
        print(f"Could not brighten photo ({e}); using it as generated.")
        return image_b64, mime


def build_svg(content: dict, image: tuple[str, str] | None) -> str:
    category_text = f'{content["category_left"].upper()} × {content["category_right"].upper()}'
    underline_width = max(120, len(category_text) * 13.5)
    headline_font, body_font = font_families()
    font_css = embed_font_css()

    # --- background: generated photo, or a flat dark fallback ------------
    if image:
        image_b64, mime = brighten_photo(*image)
        background = (
            f'<image href="data:{mime};base64,{image_b64}" '
            f'x="0" y="0" width="{CANVAS}" height="{PHOTO_HEIGHT}" '
            f'preserveAspectRatio="xMidYMid slice"/>'
        )
    else:
        background = f'<rect x="0" y="0" width="{CANVAS}" height="{PHOTO_HEIGHT}" fill="#111318"/>'

    # --- headline (auto-fit to width) + description (color-coded tokens) --
    headline_lines = split_headline(content["headline"].upper())
    desc_text, desc_spans = build_color_map(content["description"])
    tokens = get_word_tokens(desc_text, desc_spans)

    # --- vertical placement, once both blocks' extents are known ---------
    layout = layout_card(headline_lines, tokens)
    headline_svg = render_headline(
        headline_lines, layout.headline_sizes, layout.headline_baselines, headline_font
    )

    body_svg = ""
    for i, line in enumerate(layout.body_lines):
        y = layout.body_start_y + i * layout.body_line_height
        body_svg += (
            f'<text x="{MARGIN_X}" y="{y:.1f}" xml:space="preserve" '
            f'font-family="{body_font}" font-size="{layout.body_size:.1f}" '
            f'font-weight="500" fill="#FFFFFF">{line_to_tspans(line)}</text>\n'
        )

    svg = f'''<svg width="{CANVAS}" height="{CANVAS}" viewBox="0 0 {CANVAS} {CANVAS}"
     xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">

  <!-- base black canvas -->
  <rect x="0" y="0" width="{CANVAS}" height="{CANVAS}" fill="#000000"/>

  <!-- photo band -->
  {background}

  <!-- gradient blend from photo into the black lower section -->
  <defs>
    <linearGradient id="topscrim" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#000000" stop-opacity="0.62"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="0"/>
    </linearGradient>
    <linearGradient id="fade" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#000000" stop-opacity="0"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="1"/>
    </linearGradient>
    <style>{font_css}</style>
  </defs>
  <rect x="0" y="{PHOTO_HEIGHT - 90}" width="{CANVAS}" height="90" fill="url(#fade)"/>
  <rect x="0" y="{PHOTO_HEIGHT}" width="{CANVAS}" height="{CANVAS - PHOTO_HEIGHT}" fill="#000000"/>

  <!-- scrim so the wordmark stays legible over a bright photo -->
  <rect x="0" y="0" width="{CANVAS}" height="{SCRIM_HEIGHT}" fill="url(#topscrim)"/>

  <!-- 404 wordmark -->
  <text x="{MARGIN_X}" y="{LOGO_Y}" font-family="{headline_font}"
        font-size="40" font-weight="900" fill="#FFFFFF">404</text>

  <!-- category tag -->
  <text x="{MARGIN_X}" y="{CATEGORY_Y}" font-family="{body_font}"
        font-size="24" font-weight="700" letter-spacing="1.5"
        fill="#FFFFFF">{escape(category_text)}</text>
  <rect x="{MARGIN_X}" y="{CATEGORY_Y + 20}" width="{underline_width}" height="3" fill="#FFFFFF"/>

  <!-- headline -->
  {headline_svg}

  <!-- description -->
  {body_svg}
</svg>'''
    return svg


# --------------------------------------------------------------------------
# STEP 5 - rasterize the SVG to PNG
# --------------------------------------------------------------------------
def render_png(svg: str, out_path: Path) -> bool:
    """Rasterize with resvg (a Rust SVG renderer shipped as a pip wheel, so
    there are no system libraries to install).

    resvg does NOT implement @font-face, so the base64 font embedded in the
    SVG is ignored on this path -- it loads the same .ttf files directly
    instead, which is why HEADLINE_FONT_STACK names 'Anton' explicitly.
    Same font file, same metrics, so the PNG matches the browser's SVG."""
    try:
        import resvg_py
    except ImportError:
        print("resvg-py is not installed (pip install resvg-py) -- skipping PNG.")
        return False

    font_files = [p for p in FONT_FILES.values() if p and os.path.exists(p)]
    try:
        png = resvg_py.svg_to_bytes(
            svg_string=svg,
            font_files=font_files,
            width=CANVAS,
            height=CANVAS,
        )
    except Exception as e:
        print(f"PNG render failed ({e}) -- the SVG was still written.")
        return False

    out_path.write_bytes(bytes(png))
    return True


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------
def output_paths(index: int, total: int) -> tuple[Path, Path]:
    stem = Path(OUTPUT_PATH).stem
    if total > 1:
        stem = f"{stem}_{index + 1}"
    return SCRIPT_DIR / f"{stem}.svg", SCRIPT_DIR / f"{stem}.png"


def main():
    parser = argparse.ArgumentParser(description="Generate 404-style stat cards.")
    parser.add_argument("topics", nargs="*", help="one or more card topics")
    parser.add_argument(
        "--no-batch",
        action="store_true",
        help="generate images with realtime calls instead of the Batch API "
             "(instant, but ~2x the cost)",
    )
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        sys.exit("Set ANTHROPIC_API_KEY in .env before running.")

    ensure_fonts()

    topics = args.topics or [
        "The rise of AI-generated spam content flooding social media platforms"
    ]

    contents = []
    for topic in topics:
        print(f"Generating copy for topic: {topic!r} ...")
        content = generate_content(topic)
        print(json.dumps(content, indent=2))
        contents.append(content)

    prompts = [c["image_prompt"] for c in contents]
    mode = "realtime" if args.no_batch else "batch"
    print(f"\nGenerating {len(prompts)} background image(s) with "
          f"{IMAGE_MODEL} ({mode}) ...")
    images = generate_backgrounds(prompts, use_batch=not args.no_batch)

    print()
    for i, (content, image) in enumerate(zip(contents, images)):
        svg = build_svg(content, image)
        svg_path, png_path = output_paths(i, len(contents))
        svg_path.write_text(svg, encoding="utf-8")
        print(f"Saved {svg_path.name}")
        if render_png(svg, png_path):
            print(f"Saved {png_path.name}")


if __name__ == "__main__":
    main()
