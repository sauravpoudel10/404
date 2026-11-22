"""Pick a venture or stock-market story and write a 3-5 slide carousel.

The venture slots (see feeds.ROTATION) post a carousel instead of a single
card: a photo cover ("Valkryn raises $15M"), a big-number slide, then one to
three statement slides. This module writes that copy in one Claude call, the
same price as a single card; carousel.py draws it.

Every figure on every slide is checked against the headlines by VALUE, not
spelling: the hero slide sets "$15 million" as "$15,000,000", and a text
match would reject that while letting an invented "$16M" through. A carousel
with a figure the headlines don't contain is rejected before any image is
generated.
"""

import json
import re

from anthropic import Anthropic

from . import config, feeds, trends

SYSTEM = """You find the biggest venture-capital, startup or stock-market \
story in a list of headlines and write a short Instagram carousel about it \
for "404 Media".

PICKING THE STORY. Prefer, in order: a startup funding round with a named \
company and a dollar figure; an IPO or a big acquisition; a large \
single-stock or index move with a percentage. The company should be one a \
curious reader would want to hear about -- skip fund launches, ratings \
changes and anything only a professional would recognise. The story MUST \
carry a hard number, because the second slide is built around it.

Use ONLY facts and figures that appear in the headlines you were given. \
Never add a figure, an investor, a valuation or a date that is not in front \
of you. Never give investment advice or say anyone should buy or sell.

Fields:
- company: the name the story is about, as a reader knows it. Max 22 characters.
- action: what happened, with the number: "raises $15M", "files for IPO", \
"falls 48%", "buys Classiq for $2.25B". Max 26 characters. Lower case \
except names.
- category: one of FUNDING, IPO, ACQUISITION, STOCKS, MARKETS.
- tagline: one plain sentence saying what the company does or what the move \
means. 60-110 characters.
- hero_number: THE figure, written as the headline gives it: "$15 million", \
"$4.49M", "48%", "$2.25 billion".
- hero_label: one word naming the figure: RAISE, VALUATION, DEAL, DROP, \
GAIN, IPO, FUNDING. Max 10 characters.
- hero_line: one line of context set above the figure. It must read on \
its own: "Intel's deal for quantum startup Classiq", "Series A to expand \
its stablecoin FX business" -- never "Up to that amount". Max 70 characters.
- slides: one to three statement slides. Write as many as the headlines \
genuinely support -- one if the story is thin, three only if there is real \
substance. Each states a FACT about the story: what the company does, who \
is involved, what changes. Never write about the writing itself -- no "a \
hard number", "the word 'up to' means", "this headline". A thin story gets \
one good slide, not three padded ones. Each has:
    - eyebrow: 2-6 words, the slide's subject: "What the company does", \
"Why it matters", "The market reaction".
    - parts: the statement, 80-190 characters in total, as runs of text. \
color "green" marks the 2-3 key phrases, "white" is everything else. \
Include spaces at the edges of runs where a space belongs.
- image_prompt: a bright, real-looking editorial PHOTOGRAPH, vertical 4:5, \
of the company's world -- a factory floor, a server hall, a trading desk, a \
lab, a port, a test range. ABSOLUTELY NO PEOPLE, no faces, no hands. No \
text, letters, numbers, logos or screens with writing. The subject sits in \
the UPPER HALF; keep the lower half simple and uncluttered, because it is \
darkened and carries the headline. Daylight or bright interiors, rich \
colour, never dark or moody.
- caption: 1-2 plain sentences for the post. No hashtags, no links, no emoji.
- story_id: short kebab-case slug for the story itself, stable across runs.
- headline_source: one line naming the outlet and what happened."""

SCHEMA = {
    "type": "object",
    "properties": {
        "story_id": {"type": "string"},
        "headline_source": {"type": "string"},
        "company": {"type": "string"},
        "action": {"type": "string"},
        "category": {"type": "string",
                     "enum": ["FUNDING", "IPO", "ACQUISITION", "STOCKS",
                              "MARKETS"]},
        "tagline": {"type": "string"},
        "hero_number": {"type": "string"},
        "hero_label": {"type": "string"},
        "hero_line": {"type": "string"},
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "eyebrow": {"type": "string"},
                    "parts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "color": {"type": "string",
                                          "enum": ["white", "green"]},
                            },
                            "required": ["text", "color"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["eyebrow", "parts"],
                "additionalProperties": False,
            },
        },
        "image_prompt": {"type": "string"},
        "caption": {"type": "string"},
    },
    "required": ["story_id", "headline_source", "company", "action",
                 "category", "tagline", "hero_number", "hero_label",
                 "hero_line", "slides", "image_prompt", "caption"],
    "additionalProperties": False,
}

# --- figures, compared by value ----------------------------------------------
NUM_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(trillion|billion|million|thousand|tn|bn|mn|"
    r"[tbmk])?(?![a-z])", re.I)
SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6,
         "b": 1e9, "bn": 1e9, "billion": 1e9, "t": 1e12, "tn": 1e12,
         "trillion": 1e12}


def values(text: str) -> list[float]:
    """Every figure in `text`, scaled: "$15 million" and "$15,000,000" agree."""
    out = []
    for raw, unit in NUM_RE.findall(text):
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        out.append(v * SCALE[unit.lower()] if unit else v)
    return out


def unsourced(claims: str, grounding: str) -> list[str]:
    """Figures in `claims` that match no figure in `grounding` (within 1%)."""
    have = values(grounding)
    bad = []
    for v in values(claims):
        if not any(abs(v - h) <= max(0.01 * abs(h), 1e-9) for h in have):
            bad.append(f"{v:g}")
    return bad


def _all_text(c: dict) -> str:
    parts = [c["action"], c["tagline"], c["hero_number"], c["hero_line"]]
    for s in c["slides"]:
        parts.append(s["eyebrow"])
        parts.extend(p["text"] for p in s["parts"])
    return "\n".join(parts)


# --- the call -----------------------------------------------------------------
def find_story(exclude_ids: list[str], slot: int, attempts: int = 2) -> dict:
    """Carousel content for slot `slot`, or raise if nothing sourced comes back."""
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY())

    topics, headlines = feeds.fetch_slot(slot)
    fresh = feeds.drop_covered(headlines, exclude_ids)
    print(f"  slot {slot} reads {topics} [carousel: {feeds.beat_for(slot)}] -> "
          f"{len(headlines)} headlines, {len(headlines) - len(fresh)} already covered")
    headlines = fresh if len(fresh) >= 15 else headlines
    context = feeds.as_context(headlines)
    note = feeds.note_for(slot)

    rejected: list[str] = []
    for attempt in range(attempts):
        avoid = list(exclude_ids[-40:]) + rejected
        user = ("Current headlines:\n\n" + context
                + (f"\n\n{note}" if note else "")
                + ("\n\nAlready covered -- pick a DIFFERENT story:\n"
                   + "\n".join(f"- {s}" for s in avoid) if avoid else ""))
        resp = client.messages.create(
            model=config.COPY_MODEL,
            max_tokens=8000,
            system=SYSTEM,
            output_config=({"format": {"type": "json_schema", "schema": SCHEMA}}
                           | ({} if trends._rejects_effort() else {"effort": "low"})),
            messages=[{"role": "user", "content": user}],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("Claude declined to write this carousel.")
        text = "".join(b.text for b in resp.content if b.type == "text")
        content = json.loads(text[text.find("{"):text.rfind("}") + 1])
        content["slides"] = content["slides"][:3]

        bad = unsourced(_all_text(content), context)
        if not bad and content["slides"]:
            content["headline"] = f'{content["company"]} {content["action"]}'
            return content
        why = f"unsourced figures {bad}" if bad else "no statement slides"
        print(f"  attempt {attempt + 1}: rejected {content['story_id']} ({why})")
        rejected.append(content["story_id"])

    raise RuntimeError("No carousel with fully sourced figures; skipping this slot.")
