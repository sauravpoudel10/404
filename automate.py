"""
404-style stat card generator
==============================
Produces a self-contained 1080x1350 SVG (plus a rendered PNG) styled like a
funding-announcement card: one photograph running full bleed, a dark scrim
over it, and the copy set into the top of the frame.

    [404]                              <- white rounded tile, top-left
    Americans now bet $166B a year     <- bold subject + regular remainder
    They now spend more on sports      <- body paragraph, white text
    betting each year than on movies   <- with blue / red / green
    and music combined.                <- highlighted phrases
    [the photo carries the rest]

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
IMAGE_ASPECT_RATIO = "3:4"    # portrait source, so the full-bleed crop loses little

# 4:5 portrait. It is the tallest ratio Instagram and Facebook show uncropped
# in the feed, so the card takes about 25% more screen than the old square.
CANVAS_W = 1080
CANVAS_H = 1350
CANVAS = CANVAS_W             # kept for callers that only care about the width
OUTPUT_PATH = "404_card.svg"

# Batch polling. A batch job is asynchronous by design; these bounds only
# decide how long WE are willing to sit and wait before giving up.
BATCH_POLL_SECONDS = 10
BATCH_TIMEOUT_SECONDS = 30 * 60

MARGIN_X = 72                 # left margin used by every element

# --- logo tile: white rounded square, top-left ---
LOGO_SIZE = 104
LOGO_Y = 62
LOGO_RADIUS = 26
LOGO_FONT_SIZE = 39

# --- headline: bold subject + regular remainder, sentence case ---
HEADLINE_TOP = 304            # baseline of the first headline line
HEADLINE_FONT_SIZE = 96
HEADLINE_MIN_FONT_SIZE = 56
HEADLINE_LINE_RATIO = 1.07    # tight leading
HEADLINE_MAX_LINES = 3
HEADLINE_TRACKING = -1.6      # slight negative tracking, as in the reference

# --- body: the colour-coded paragraph, unchanged in spirit ---
HEADLINE_BODY_GAP = 56
BODY_FONT_SIZE = 44
BODY_MIN_FONT_SIZE = 32
BODY_LINE_HEIGHT_RATIO = 1.34
BODY_MAX_LINES = 5
# Text owns the top of the card; the photo carries everything below this.
TEXT_ZONE_BOTTOM = 790

# --- the classic square card ----------------------------------------------
# The original 404 layout: a photo band across the top, the category tag and
# its underline, then an all-caps condensed headline over the paragraph.
# Kept intact and rendered at its own size; the two styles alternate.
CLASSIC_CANVAS = 1080
CLASSIC_PHOTO_HEIGHT = 455
CLASSIC_MARGIN_X = 90
CLASSIC_LOGO_Y = 90
CLASSIC_CATEGORY_Y = 500
CLASSIC_SCRIM_HEIGHT = 190
CLASSIC_CATEGORY_UNDERLINE_GAP = 26
CLASSIC_HEADLINE_FONT_SIZE = 84
CLASSIC_HEADLINE_MIN_FONT_SIZE = 46
CLASSIC_HEADLINE_BODY_GAP = 55
CLASSIC_BODY_FONT_SIZE = 48
CLASSIC_BODY_MIN_FONT_SIZE = 38          # preferred floor
CLASSIC_BODY_HARD_MIN_FONT_SIZE = 26     # absolute floor, only to avoid overflow
CLASSIC_BODY_LINE_HEIGHT_RATIO = 1.44
CLASSIC_BODY_CHAR_WIDTH_RATIO = 0.478    # tuned so lines fill the text width
CLASSIC_BODY_BOTTOM_MARGIN = 55
# 'Anton' is named explicitly because the PNG renderer loads the .ttf by its
# real family name rather than through @font-face -- see render_png().
CLASSIC_HEADLINE_FONT_STACK = (
    "'Anton', Impact, 'Arial Black', 'Helvetica Neue', sans-serif"
)

STYLES = ("classic", "feature")
DEFAULT_STYLE = "feature"


def canvas_for(style: str) -> tuple[int, int]:
    """(width, height) of a card in this style."""
    if style == "classic":
        return CLASSIC_CANVAS, CLASSIC_CANVAS
    return CANVAS_W, CANVAS_H


def aspect_for(style: str) -> str:
    """The aspect ratio to ask the image model for.

    The classic card slices a wide band out of the top of the frame; the
    feature card uses the whole thing, so it wants a portrait source.
    """
    return "16:9" if style == "classic" else IMAGE_ASPECT_RATIO


# Two static weights cut out of Inter's variable font (see ensure_fonts).
# They are separate FAMILIES rather than two weights of one family because
# resvg matches on family name and will not synthesise a bold.
BOLD_FAMILY = "Card404Bold"
TEXT_FAMILY = "Card404Text"
HEADLINE_FONT_STACK = f"'{BOLD_FAMILY}', 'Helvetica Neue', Arial, sans-serif"
BODY_FONT_STACK = f"'{TEXT_FAMILY}', 'Helvetica Neue', Arial, sans-serif"

EMBED_FONTS = True
FONT_FILES = {
    "bold": str(SCRIPT_DIR / f"{BOLD_FAMILY}.ttf"),
    "text": str(SCRIPT_DIR / f"{TEXT_FAMILY}.ttf"),
    "anton": str(SCRIPT_DIR / "Anton-Regular.ttf"),   # classic headline
}
FONT_DOWNLOAD_URLS = {
    "anton": "https://raw.githubusercontent.com/google/fonts/main/ofl/anton/"
             "Anton-Regular.ttf",
}
FONT_WEIGHTS = {"bold": 800, "text": 400}
INTER_VARIABLE_URL = (
    "https://raw.githubusercontent.com/google/fonts/main/ofl/inter/"
    "Inter%5Bopsz,wght%5D.ttf"
)


def _cut_static_weight(raw: bytes, weight: int, family: str, path: str):
    """Freeze the variable font at one weight and rename it to `family`.

    Inter ships only as a variable font, and resvg renders its default
    instance whatever `font-weight` the SVG asks for -- so a bold subject
    and a regular remainder on the same line have to arrive as two separate
    files under two distinct family names.
    """
    import io as _io

    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    font = instancer.instantiateVariableFont(
        TTFont(_io.BytesIO(raw)), {"wght": weight, "opsz": 28}, inplace=False
    )
    name = font["name"]
    name.setName(family, 1, 3, 1, 0x409)       # family
    name.setName("Regular", 2, 3, 1, 0x409)    # subfamily
    name.setName(family, 4, 3, 1, 0x409)       # full name
    name.setName(family, 6, 3, 1, 0x409)       # postscript
    for nid in (16, 17, 21, 22):               # typographic names would win
        name.removeNames(nameID=nid)
    font.save(path)


def ensure_fonts():
    """Build the two card fonts once, into the working folder.

    Never raises: on any failure the SVG falls back to the system stacks
    above, and the layout measurements fall back to character estimates.
    """
    if not EMBED_FONTS:
        return
    missing = {k: p for k, p in FONT_FILES.items() if not os.path.exists(p)}
    if not missing:
        return

    # Anton ships as a static file, so it is a straight download.
    for key, url in FONT_DOWNLOAD_URLS.items():
        if key not in missing:
            continue
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            Path(missing[key]).write_bytes(resp.content)
            print(f"Downloaded {os.path.basename(missing[key])}")
        except Exception as e:
            print(f"Could not download {os.path.basename(missing[key])} ({e}); "
                  "using system font fallback.")

    cut = {k: v for k, v in missing.items() if k in FONT_WEIGHTS}
    if not cut:
        return
    try:
        resp = requests.get(INTER_VARIABLE_URL, timeout=60)
        resp.raise_for_status()
        for key, path in cut.items():
            _cut_static_weight(resp.content, FONT_WEIGHTS[key],
                               Path(path).stem, path)
            print(f"Built {os.path.basename(path)}")
    except Exception as e:
        print(f"Could not build card fonts ({e}); using system font fallback.")


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
        '  "headline_lead": "the subject: 1-3 words, the name a reader '
        'recognises",\n'
        '  "headline_rest": "what happened: 2-6 words, and where the number '
        'goes",\n'
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
        "Rules for the headline:\n"
        "- The two pieces are set on one flowing line, the lead in heavy "
        "bold and the rest in regular weight.\n"
        "- Sentence case, not all caps, under 42 characters together.\n\n"
        "Rules for image_prompt:\n"
        "- Describe a real-looking editorial PHOTOGRAPH in a vertical 4:5 "
        "frame, brightly and vividly lit, with rich saturated colour.\n"
        "- The subject sits in the LOWER TWO THIRDS; the top third stays "
        "open and uncluttered, because the headline is set over it.\n"
        "- Be creative and specific about vantage point, lens and moment "
        "rather than describing a generic stock photo.\n"
        "- Absolutely no text, letters, numbers, logos, watermarks or "
        "captions anywhere in the image."
    )

    example_user = "Topic: Americans spending more on sports betting than on movies, arts, museums and music combined ($166B/year)"
    example_assistant = json.dumps({
        "category_left": "CONSUMER",
        "category_right": "SPENDING",
        "headline_lead": "Americans",
        "headline_rest": "now bet $166B a year",
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
            "A bright vertical 4:5 editorial photograph looking down a "
            "sportsbook floor from balcony height: rows of vivid screens "
            "below, spectators mid-cheer in the lower two thirds, the upper "
            "third an open expanse of pale ceiling and daylight haze, "
            "saturated colour, no text or logos anywhere."
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
def image_gen_config(aspect: str = IMAGE_ASPECT_RATIO) -> dict:
    return {
        "response_modalities": ["IMAGE"],
        "image_config": {"aspect_ratio": aspect},
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


def generate_backgrounds_batch(prompts: list[str], aspect: str = IMAGE_ASPECT_RATIO) -> list[tuple[str, str] | None]:
    """Submit every image prompt as ONE Gemini batch job, then wait for it."""
    client = _gemini_client()

    job = client.batches.create(
        model=IMAGE_MODEL,
        src=[
            {
                "contents": [{"role": "user", "parts": [{"text": p}]}],
                "config": image_gen_config(aspect),
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


def generate_backgrounds_sync(prompts: list[str], aspect: str = IMAGE_ASPECT_RATIO) -> list[tuple[str, str] | None]:
    """Realtime generateContent, one call per prompt. Fast, full price."""
    client = _gemini_client()
    results = []
    for i, prompt in enumerate(prompts, 1):
        print(f"  generating image {i}/{len(prompts)} ...")
        try:
            resp = client.models.generate_content(
                model=IMAGE_MODEL, contents=prompt, config=image_gen_config(aspect)
            )
            results.append(_extract_image(resp))
        except Exception as e:
            print(f"  image {i} failed: {e}")
            results.append(None)
    return results


def generate_backgrounds(prompts: list[str], use_batch: bool,
                         aspect: str = IMAGE_ASPECT_RATIO):
    if not GEMINI_API_KEY:
        print("No GEMINI_API_KEY set -- using plain dark backgrounds instead.")
        return [None] * len(prompts)
    try:
        if use_batch:
            return generate_backgrounds_batch(prompts, aspect)
        return generate_backgrounds_sync(prompts, aspect)
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
# Characters that legitimately butt up against the previous part with no
# space: closing punctuation on the left of the join, and openers/prefixes on
# the right of it. Anything else joining two words means the model simply
# forgot the space -- which renders as "is<red>burning through cash</red>while".
_ATTACH_AFTER = set(".,;:!?%)]}\'\u2019\u201d/-\u2013\u2014")
_ATTACH_BEFORE = set("([{$\u201c\u2018/-\u2013\u2014")


def _needs_space(left: str, right: str) -> bool:
    """True when two adjacent parts have run together mid-sentence."""
    if not left or not right:
        return False
    a, b = left[-1], right[0]
    if a.isspace() or b.isspace():
        return False
    if b in _ATTACH_AFTER or a in _ATTACH_BEFORE:
        return False
    return True


def build_color_map(parts):
    """Flatten description parts into one string + (start, end, color) spans.

    The prompt asks for a space at the edge of each part where one belongs,
    and the model does not always oblige. Repairing the join here is safe:
    the highlights are phrases, so two word characters meeting across a
    boundary is a missing space rather than a deliberate mid-word split.
    """
    text, spans, pos = "", [], 0
    for part in parts:
        t = part["text"]
        if _needs_space(text, t):
            text += " "
            pos += 1
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
# STEP 3b - font loading, pixel measurement, and auto-fit layout
# --------------------------------------------------------------------------
def font_available() -> bool:
    """True when both Inter weights are on disk, so text can be measured exactly."""
    return bool(EMBED_FONTS and all(os.path.exists(FONT_FILES[k])
                                    for k in ("bold", "text")))


def anton_available() -> bool:
    return bool(EMBED_FONTS and os.path.exists(FONT_FILES["anton"]))


def embed_font_css() -> str:
    """@font-face CSS for the card fonts, so the standalone .svg renders the
    same in a browser. The PNG path ignores this and loads the .ttf files
    directly -- see render_png()."""
    if not EMBED_FONTS:
        return ""
    css = ""
    for path in FONT_FILES.values():
        if os.path.exists(path):
            b64 = base64.b64encode(Path(path).read_bytes()).decode("utf-8")
            css += (
                f"@font-face{{font-family:'{Path(path).stem}';"
                f"src:url(data:font/ttf;base64,{b64}) format('truetype');}}"
            )
    return css


def font_families():
    """(bold, regular) family stacks, embedded names first when available."""
    if font_available():
        return HEADLINE_FONT_STACK, BODY_FONT_STACK
    return ("'Helvetica Neue', Arial, sans-serif",) * 2


# --- measurement -----------------------------------------------------------
# Widths come from the real .ttf via Pillow, using advance width (getlength)
# rather than the ink bounding box, because runs are concatenated on a line
# and only advances add up correctly. Without the files we fall back to a
# deliberately pessimistic character estimate.
_FALLBACK_CHAR_RATIO = {"bold": 0.62, "text": 0.55}


def _run_width(text: str, key: str, size: float) -> float:
    if font_available():
        from PIL import ImageFont
        font = ImageFont.truetype(FONT_FILES[key], max(1, int(round(size))))
        return font.getlength(text)
    return len(text) * size * _FALLBACK_CHAR_RATIO[key]


def _space_width(size: float) -> float:
    return _run_width(" ", "text", size)


# --- headline --------------------------------------------------------------
def headline_words(content: dict) -> list[tuple[str, str]]:
    """The headline as (word, weight-key) pairs.

    Two fields drive the reference look: `headline_lead` is set bold and
    names the subject, `headline_rest` completes the sentence in regular.
    Older content that only carries a single `headline` still renders --
    its first word takes the bold weight.
    """
    lead = (content.get("headline_lead") or "").strip()
    rest = (content.get("headline_rest") or "").strip()
    if not lead and not rest:
        parts = (content.get("headline") or "").split()
        lead, rest = (parts[0] if parts else ""), " ".join(parts[1:])
    return ([(w, "bold") for w in lead.split()]
            + [(w, "text") for w in rest.split()])


def _headline_line_width(words, size: float) -> float:
    if not words:
        return 0.0
    return (sum(_run_width(t, k, size) for t, k in words)
            + _space_width(size) * (len(words) - 1))


def wrap_headline(words, max_width: float, size: float) -> list[list]:
    lines, current = [], []
    for word in words:
        if current and _headline_line_width(current + [word], size) > max_width:
            lines.append(current)
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(current)
    return lines or [[]]


# --- body ------------------------------------------------------------------
def wrap_tokens_px(tokens, max_width: float, size: float) -> list[list]:
    """Wrap the colour-coded tokens by measured width rather than by an
    estimated character count, so a line genuinely fills the column."""
    space = _space_width(size)
    lines, current, used = [], [], 0.0
    for token in tokens:
        width = sum(_run_width(t, "text", size) for t, _ in token)
        advance = width + (space if current else 0.0)
        if current and used + advance > max_width:
            lines.append(current)
            current, used = [token], width
        else:
            current.append(token)
            used += advance
    if current:
        lines.append(current)
    return lines or [[]]


class Layout(NamedTuple):
    headline_size: float
    headline_lines: list
    headline_baselines: list[float]
    body_size: float
    body_line_height: float
    body_lines: list
    body_start_y: float


def layout_card(words, tokens) -> Layout:
    """Fit the headline and the paragraph into the text zone at the top.

    The headline is tried at its full size first and stepped down only when
    it needs more than HEADLINE_MAX_LINES; the body is then stepped down
    (re-wrapping each time, since smaller type fits more per line) until the
    block clears TEXT_ZONE_BOTTOM. If nothing fits, the smallest combination
    is used -- small type beats type running off the card.
    """
    max_width = CANVAS_W - 2 * MARGIN_X
    candidate = None

    for h_size in range(HEADLINE_FONT_SIZE, HEADLINE_MIN_FONT_SIZE - 1, -2):
        h_lines = wrap_headline(words, max_width, h_size)
        too_tall = len(h_lines) > HEADLINE_MAX_LINES
        line_height = h_size * HEADLINE_LINE_RATIO
        baselines = [HEADLINE_TOP + i * line_height for i in range(len(h_lines))]

        for b_size in range(BODY_FONT_SIZE, BODY_MIN_FONT_SIZE - 1, -1):
            b_lines = wrap_tokens_px(tokens, max_width, b_size)
            b_height = b_size * BODY_LINE_HEIGHT_RATIO
            start = baselines[-1] + HEADLINE_BODY_GAP + b_size * 0.78
            bottom = start + (len(b_lines) - 1) * b_height

            candidate = Layout(h_size, h_lines, baselines,
                               b_size, b_height, b_lines, start)
            if not too_tall and len(b_lines) <= BODY_MAX_LINES \
                    and bottom <= TEXT_ZONE_BOTTOM:
                return candidate

    return candidate


def render_headline(lines, size, baselines, bold_font, text_font) -> str:
    out = []
    for words, y in zip(lines, baselines):
        spans = []
        for i, (word, key) in enumerate(words):
            family = bold_font if key == "bold" else text_font
            spans.append(f'<tspan font-family="{family}">'
                         f'{escape((" " if i else "") + word)}</tspan>')
        out.append(
            f'<text x="{MARGIN_X}" y="{y:.1f}" xml:space="preserve" '
            f'font-size="{size:.1f}" letter-spacing="{HEADLINE_TRACKING}" '
            f'fill="#FFFFFF">{"".join(spans)}</text>'
        )
    return "\n  ".join(out)


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


# --------------------------------------------------------------------------
# STEP 3c - the classic square card: layout and renderer
# --------------------------------------------------------------------------
# This is the original design, unchanged in behaviour. It measures the
# headline against Anton and wraps the paragraph on an estimated character
# count, which is what it was tuned against; the feature card measures both
# in pixels instead. Keeping them separate is deliberate -- retuning the old
# layout to new machinery would change a look that already works.
CONSERVATIVE_CHAR_FACTOR = 0.72  # safe even for chunky fonts like Arial Black


def classic_split_headline(text):
    """Split a long headline into at most 2 lines (by word boundaries)."""
    words = text.split()
    if len(text) <= 22 or len(words) < 2:
        return [text]
    mid = len(words) // 2
    return [" ".join(words[:mid]), " ".join(words[mid:])]


def _classic_measure(text, font_size):
    from PIL import ImageFont
    font = ImageFont.truetype(FONT_FILES["anton"], size=int(round(font_size)))
    box = font.getbbox(text)
    return box[2] - box[0]


def _classic_conservative(line, max_width, base_size, min_size):
    est_width = len(line) * base_size * CONSERVATIVE_CHAR_FACTOR
    if est_width <= max_width:
        return base_size
    return max(min_size, max_width / (len(line) * CONSERVATIVE_CHAR_FACTOR))


def classic_headline_sizes(lines, max_width, base_size, min_size=40):
    """A font size per line that fits inside max_width.

    With Anton present the measurement is the truth, because both output
    paths really use that file -- the browser via @font-face, the PNG via a
    directly loaded font file. Without it, no font is knowable ahead of
    time, so the estimate is deliberately pessimistic.
    """
    sizes = []
    for line in lines:
        if anton_available():
            width = _classic_measure(line, base_size)
            size = base_size if width <= max_width else max(
                min_size, max_width / width * base_size
            )
        else:
            size = _classic_conservative(line, max_width, base_size, min_size)
        sizes.append(size)
    return sizes


def classic_cap_height(size):
    """Height of a capital above the baseline, from Anton where we have it."""
    if anton_available():
        from PIL import ImageFont
        font = ImageFont.truetype(FONT_FILES["anton"], int(round(size)))
        ascent, _ = font.getmetrics()
        return ascent - font.getbbox("AH")[1]
    return size * 0.75


def classic_body_max_chars(font_size):
    width = CLASSIC_CANVAS - 2 * CLASSIC_MARGIN_X
    return max(20, int(width / (font_size * CLASSIC_BODY_CHAR_WIDTH_RATIO)))


class ClassicLayout(NamedTuple):
    headline_sizes: list[float]
    headline_baselines: list[float]
    body_size: float
    body_line_height: float
    body_lines: list
    body_start_y: float


def layout_classic(headline_lines, tokens) -> ClassicLayout:
    """Stack the headline and paragraph below the category tag.

    The headline hangs off the underline by its cap height, so it can never
    be struck through by it. If the two blocks together would still run off
    the canvas, the body is stepped down first (re-wrapping at each size,
    since smaller text fits more characters per line) and the headline only
    after that.
    """
    underline_bottom = CLASSIC_CATEGORY_Y + 23
    limit = CLASSIC_CANVAS - CLASSIC_BODY_BOTTOM_MARGIN
    max_width = CLASSIC_CANVAS - 2 * CLASSIC_MARGIN_X
    body_steps = list(range(CLASSIC_BODY_FONT_SIZE,
                            CLASSIC_BODY_MIN_FONT_SIZE - 1, -1))
    rescue_steps = list(range(CLASSIC_BODY_MIN_FONT_SIZE - 1,
                              CLASSIC_BODY_HARD_MIN_FONT_SIZE - 1, -1))

    headline_scale = 1.0
    while True:
        base = CLASSIC_HEADLINE_FONT_SIZE * headline_scale
        sizes = classic_headline_sizes(headline_lines, max_width, base,
                                       CLASSIC_HEADLINE_MIN_FONT_SIZE)

        y = (underline_bottom + CLASSIC_CATEGORY_UNDERLINE_GAP
             + classic_cap_height(sizes[0]))
        baselines = []
        for size in sizes:
            baselines.append(y)
            y += size * 1.1
        body_start = baselines[-1] + CLASSIC_HEADLINE_BODY_GAP

        exhausted = headline_scale <= 0.6
        steps = body_steps + (rescue_steps if exhausted else [])

        for body_size in steps:
            line_height = body_size * CLASSIC_BODY_LINE_HEIGHT_RATIO
            lines = wrap_tokens(tokens, classic_body_max_chars(body_size))
            fits = body_start + (len(lines) - 1) * line_height <= limit
            if fits or (exhausted and body_size == steps[-1]):
                return ClassicLayout(sizes, baselines, body_size,
                                     line_height, lines, body_start)

        headline_scale -= 0.05


def render_headline_classic(lines, sizes, baselines, headline_font):
    out = []
    for line, size, y in zip(lines, sizes, baselines):
        out.append(
            f'<text x="{CLASSIC_MARGIN_X}" y="{y:.1f}" '
            f'font-family="{headline_font}" '
            f'font-size="{size:.1f}" font-weight="900" fill="#FFFFFF">'
            f'{escape(line)}</text>'
        )
    return "\n  ".join(out)


def build_svg_classic(content: dict, image: tuple[str, str] | None) -> str:
    category_text = (f'{content["category_left"].upper()} '
                     f'\u00d7 {content["category_right"].upper()}')
    underline_width = max(120, len(category_text) * 13.5)
    headline_font = (f"'Anton', {CLASSIC_HEADLINE_FONT_STACK}"
                     if anton_available() else CLASSIC_HEADLINE_FONT_STACK)
    _, body_font = font_families()
    font_css = embed_font_css()

    if image:
        image_b64, mime = brighten_photo(*image)
        background = (
            f'<image href="data:{mime};base64,{image_b64}" '
            f'x="0" y="0" width="{CLASSIC_CANVAS}" '
            f'height="{CLASSIC_PHOTO_HEIGHT}" '
            f'preserveAspectRatio="xMidYMid slice"/>'
        )
    else:
        background = (f'<rect x="0" y="0" width="{CLASSIC_CANVAS}" '
                      f'height="{CLASSIC_PHOTO_HEIGHT}" fill="#111318"/>')

    headline_lines = classic_split_headline(content["headline"].upper())
    desc_text, desc_spans = build_color_map(content["description"])
    tokens = get_word_tokens(desc_text, desc_spans)

    layout = layout_classic(headline_lines, tokens)
    headline_svg = render_headline_classic(
        headline_lines, layout.headline_sizes, layout.headline_baselines,
        headline_font,
    )

    body_svg = ""
    for i, line in enumerate(layout.body_lines):
        y = layout.body_start_y + i * layout.body_line_height
        body_svg += (
            f'  <text x="{CLASSIC_MARGIN_X}" y="{y:.1f}" xml:space="preserve" '
            f'font-family="{body_font}" font-size="{layout.body_size:.1f}" '
            f'font-weight="500" fill="#FFFFFF">{line_to_tspans(line)}</text>\n'
        )

    return f'''<svg width="{CLASSIC_CANVAS}" height="{CLASSIC_CANVAS}"
     viewBox="0 0 {CLASSIC_CANVAS} {CLASSIC_CANVAS}"
     xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">

  <rect x="0" y="0" width="{CLASSIC_CANVAS}" height="{CLASSIC_CANVAS}" fill="#000000"/>

  <!-- photo band -->
  {background}

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
  <rect x="0" y="{CLASSIC_PHOTO_HEIGHT - 90}" width="{CLASSIC_CANVAS}" height="90" fill="url(#fade)"/>
  <rect x="0" y="{CLASSIC_PHOTO_HEIGHT}" width="{CLASSIC_CANVAS}" height="{CLASSIC_CANVAS - CLASSIC_PHOTO_HEIGHT}" fill="#000000"/>

  <!-- scrim so the wordmark stays legible over a bright photo -->
  <rect x="0" y="0" width="{CLASSIC_CANVAS}" height="{CLASSIC_SCRIM_HEIGHT}" fill="url(#topscrim)"/>

  <!-- 404 wordmark -->
  <text x="{CLASSIC_MARGIN_X}" y="{CLASSIC_LOGO_Y}" font-family="{headline_font}"
        font-size="40" font-weight="900" fill="#FFFFFF">404</text>

  <!-- category tag -->
  <text x="{CLASSIC_MARGIN_X}" y="{CLASSIC_CATEGORY_Y}" font-family="{body_font}"
        font-size="24" font-weight="700" letter-spacing="1.5"
        fill="#FFFFFF">{escape(category_text)}</text>
  <rect x="{CLASSIC_MARGIN_X}" y="{CLASSIC_CATEGORY_Y + 20}" width="{underline_width}" height="3" fill="#FFFFFF"/>

  <!-- headline -->
  {headline_svg}

  <!-- description -->
{body_svg}</svg>'''


# How dark the scrim is over the copy, and how much of the photo is left
# alone below it. A fixed gradient cannot serve both: a two-line card wants
# the photo back early, while a five-line one needs cover further down --
# and coloured highlights on a bright frame (green text over produce) are
# the first thing to become unreadable.
SCRIM_COLOR = "#05070B"
SCRIM_OVER_TEXT = 0.82        # opacity everywhere the copy sits
SCRIM_TOP = 0.90              # a touch darker still behind the logo tile
SCRIM_CLEAR = 0.14            # once past the copy, the photo carries the card
SCRIM_FOOT = 0.52             # closing the frame on a solid bottom edge
SCRIM_FADE = 110              # px over which it opens up below the last line


def build_scrim(text_bottom: float) -> str:
    """A vertical gradient that holds full cover to the end of the copy,
    then opens up quickly so the photograph is genuinely visible."""
    hold = max(0.0, min(1.0, (text_bottom - 40) / CANVAS_H))
    clear = max(hold + 0.02, min(0.96, (text_bottom + SCRIM_FADE) / CANVAS_H))
    stops = [
        (0.0, SCRIM_TOP),
        (hold, SCRIM_OVER_TEXT),
        (clear, SCRIM_CLEAR),
        (1.0, SCRIM_FOOT),
    ]
    body = "".join(
        f'\n      <stop offset="{offset * 100:.1f}%" stop-color="{SCRIM_COLOR}" '
        f'stop-opacity="{opacity}"/>'
        for offset, opacity in stops
    )
    return (f'<linearGradient id="scrim" x1="0" y1="0" x2="0" y2="1">{body}'
            f'\n    </linearGradient>')


def build_svg(content: dict, image: tuple[str, str] | None,
              style: str = DEFAULT_STYLE) -> str:
    """Render the card in either style. See STYLES."""
    if style == "classic":
        return build_svg_classic(content, image)
    return build_svg_feature(content, image)


def build_svg_feature(content: dict, image: tuple[str, str] | None) -> str:
    bold_font, text_font = font_families()
    font_css = embed_font_css()

    # --- background: the photo runs full bleed behind everything ---------
    if image:
        image_b64, mime = brighten_photo(*image)
        background = (
            f'<image href="data:{mime};base64,{image_b64}" x="0" y="0" '
            f'width="{CANVAS_W}" height="{CANVAS_H}" '
            f'preserveAspectRatio="xMidYMid slice"/>'
        )
    else:
        background = (f'<rect width="{CANVAS_W}" height="{CANVAS_H}" '
                      f'fill="#111318"/>')

    # --- copy ------------------------------------------------------------
    words = headline_words(content)
    desc_text, desc_spans = build_color_map(content["description"])
    tokens = get_word_tokens(desc_text, desc_spans)
    layout = layout_card(words, tokens)

    headline_svg = render_headline(layout.headline_lines, layout.headline_size,
                                   layout.headline_baselines, bold_font, text_font)

    body_svg = ""
    for i, line in enumerate(layout.body_lines):
        y = layout.body_start_y + i * layout.body_line_height
        body_svg += (
            f'  <text x="{MARGIN_X}" y="{y:.1f}" xml:space="preserve" '
            f'font-family="{text_font}" font-size="{layout.body_size:.1f}" '
            f'fill="#FFFFFF">{line_to_tspans(line)}</text>\n'
        )

    # Centre "404" optically in the tile: Inter's cap height is ~0.727em, so
    # sitting the baseline half a cap below the middle centres the capitals
    # rather than the (invisible) full em box.
    logo_baseline = LOGO_Y + LOGO_SIZE / 2 + LOGO_FONT_SIZE * 0.727 / 2

    text_bottom = (layout.body_start_y
                   + (len(layout.body_lines) - 1) * layout.body_line_height
                   + layout.body_size * 0.25)          # descender
    scrim = build_scrim(text_bottom)

    svg = f'''<svg width="{CANVAS_W}" height="{CANVAS_H}" viewBox="0 0 {CANVAS_W} {CANVAS_H}"
     xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">

  <rect x="0" y="0" width="{CANVAS_W}" height="{CANVAS_H}" fill="#05070B"/>

  <!-- full-bleed photo -->
  {background}

  <defs>
    {scrim}
    <style>{font_css}</style>
  </defs>
  <rect x="0" y="0" width="{CANVAS_W}" height="{CANVAS_H}" fill="url(#scrim)"/>

  <!-- logo tile -->
  <rect x="{MARGIN_X}" y="{LOGO_Y}" width="{LOGO_SIZE}" height="{LOGO_SIZE}"
        rx="{LOGO_RADIUS}" ry="{LOGO_RADIUS}" fill="#FFFFFF"/>
  <text x="{MARGIN_X + LOGO_SIZE / 2}" y="{logo_baseline:.1f}"
        font-family="{bold_font}" font-size="{LOGO_FONT_SIZE}"
        letter-spacing="-1.2" text-anchor="middle" fill="#0A0B0D">404</text>

  <!-- headline -->
  {headline_svg}

  <!-- description -->
{body_svg}</svg>'''
    return svg


# --------------------------------------------------------------------------
# STEP 5 - rasterize the SVG to PNG
# --------------------------------------------------------------------------
def render_png(svg: str, out_path: Path, style: str = DEFAULT_STYLE) -> bool:
    """Rasterize with resvg (a Rust SVG renderer shipped as a pip wheel, so
    there are no system libraries to install).

    resvg does NOT implement @font-face, so the base64 font embedded in the
    SVG is ignored on this path -- it loads the same .ttf files directly
    instead, which is why the font stacks name the two families explicitly.
    Same font file, same metrics, so the PNG matches the browser's SVG."""
    try:
        import resvg_py
    except ImportError:
        print("resvg-py is not installed (pip install resvg-py) -- skipping PNG.")
        return False

    font_files = [p for p in FONT_FILES.values() if p and os.path.exists(p)]
    try:
        width, height = canvas_for(style)
        png = resvg_py.svg_to_bytes(
            svg_string=svg,
            font_files=font_files,
            width=width,
            height=height,
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
        svg = build_svg(content, image, DEFAULT_STYLE)
        svg_path, png_path = output_paths(i, len(contents))
        svg_path.write_text(svg, encoding="utf-8")
        print(f"Saved {svg_path.name}")
        if render_png(svg, png_path):
            print(f"Saved {png_path.name}")


if __name__ == "__main__":
    main()
