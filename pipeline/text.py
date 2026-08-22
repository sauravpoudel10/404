"""Caption sanitising shared by every platform.

Prompts ask the models not to emit hashtags or links, but a prompt is a
request, not a guarantee -- across ~36 posts a day something eventually slips
through. So the rule is enforced here, at post time, for every destination.

Links matter for more than tidiness: X bills $0.015 per post but $0.20 if it
contains a link, so one stray URL costs 13x.
"""

import re

URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.I)

# A hash followed by a LETTER. Deliberately not `#\w+`, which would also eat
# "#1" and mangle ordinary copy like "ranked #1 by revenue".
#
# `&` is allowed inside a tag because "#M&A" is common in finance copy, and
# without it the tag splits into "#M" + "&A" -- which silently breaks the
# trailing-run match below and lets a whole block of tags through.
TAG = r"(?<!\w)#[A-Za-z][\w&]*"
HASHTAG_RE = re.compile(TAG)

# A trailing run of tags -- the usual "...text. #VC #AI" shape. The tail
# allows punctuation and emoji so a stray character after the last tag
# doesn't stop the whole run from matching.
TRAILING_TAGS_RE = re.compile(rf"(?:\s*{TAG})+[\s\W]*$")


def sanitize(text: str, limit: int | None = None) -> str:
    """Strip links and hashtags, tidy whitespace, optionally truncate.

    Hashtags are handled in two passes, because the two positions mean
    different things. A trailing run is pure tagging and is deleted whole.
    An inline one is usually a word doing real grammatical work -- "ahead of
    #Intel and AMD" -- so only the '#' is dropped and the word survives;
    deleting it would leave "ahead of and AMD".
    """
    text = URL_RE.sub("", text)
    text = TRAILING_TAGS_RE.sub("", text)
    text = HASHTAG_RE.sub(lambda m: m.group(0)[1:], text)

    # Removing trailing tags leaves dangling separators and double spaces.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip().rstrip("-–—,;:· ").strip()

    if limit and len(text) > limit:
        if "\n" in text:
            # A ranked list must lose whole rows -- cutting mid-row leaves a
            # dangling flag and half a number.
            lines = text.split("\n")
            while lines and len("\n".join(lines)) > limit:
                lines.pop()
            text = "\n".join(lines).rstrip()
        else:
            text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return text


# --- ranked-list normalising -------------------------------------------------
# The model reliably produces flags and real figures, but not a single
# consistent metric per table: it will mix "$1.70T of assets" with "$32.7B
# invested in 2025", or "37 pricings" with "$2.9B raised". Enforcing it in
# code is free; enforcing it by prompt needs a bigger, costlier model.

_SCALE = {"t": 1e12, "b": 1e9, "m": 1e6, "k": 1e3}
_VALUE_RE = re.compile(
    r"(?P<cur>[$€£₹])?\s*(?P<num>\d[\d,]*\.?\d*)\s*(?P<suffix>[TBMK])?(?P<pct>%)?",
    re.I,
)


def _parse_value(row: str):
    """Return (unit_class, magnitude) for a ranking row, or None."""
    m = _VALUE_RE.search(row.split(":")[-1] if ":" in row else row)
    if not m:
        return None
    num = float(m.group("num").replace(",", ""))
    if m.group("suffix"):
        num *= _SCALE[m.group("suffix").lower()]
    if m.group("pct"):
        return "%", num
    if m.group("cur"):
        return "currency", num
    return "count", num


def normalise_list(body: str, min_rows: int = 4) -> str:
    """Keep only rows sharing the dominant unit, sorted largest first.

    Falls back to the original text when too few rows survive -- a slightly
    mixed six-row table reads better than a spotless two-row one.
    """
    lines = body.split("\n")
    if len(lines) < 2:
        return body
    title, rest = lines[0], [ln for ln in lines[1:] if ln.strip()]

    parsed = [(ln, _parse_value(ln)) for ln in rest]
    usable = [(ln, v) for ln, v in parsed if v]
    if len(usable) < min_rows:
        return body

    counts: dict[str, int] = {}
    for _, (unit, _mag) in usable:
        counts[unit] = counts.get(unit, 0) + 1
    dominant = max(counts, key=counts.get)

    kept = [(ln, mag) for ln, (unit, mag) in usable if unit == dominant]
    if len(kept) < min_rows:
        return body

    kept.sort(key=lambda pair: pair[1], reverse=True)
    return title + "\n\n" + "\n".join(ln for ln, _ in kept)
