"""Find a trending story and write the card copy for it, in one Claude call.

Story discovery comes from free RSS (see feeds.py), not the paid web-search
tool. Search billed $0.01 per query and pulled 5-12k tokens of results into
context on every one of the twelve daily runs; headlines cost nothing and
are about a fifth the size.

The call returns the same JSON shape automate.py already renders, plus a
`story_id` used to keep the pipeline from posting the same story twice
across the day's twelve runs.
"""

import json
import re
from datetime import datetime, timezone

from anthropic import Anthropic

from . import config, feeds

SYSTEM = """You find the single most-discussed news story of the moment and \
write a "404 Media" style stat card about it.

You will be given a list of current headlines. Pick the single biggest story in them.

The audience is mainly American, so prefer stories that matter to a US reader - but the beat is wide: finance and markets, startups and funding, billionaires and big personalities, SpaceX and space, Musk, Trump and policy, Europe, Asia, and the everyday economics ordinary people feel - wages, housing, jobs, prices. A story about a factory town or a jobs report is as good as one about a mega-round.

PICKING THE STORY - apply this test before anything else. Ask: would an ordinary American who does not work in finance stop scrolling for this?

Take stories where the answer is yes:
- something that changes what people pay: prices, rent, wages, insurance, petrol, groceries, taxes, interest rates
- jobs: a big employer hiring or cutting, a plant opening or closing, a town that gains or loses work
- a company people actually use: a supermarket, a carmaker, an airline, a phone, a streaming service, a bank they bank with
- a name people recognise: a president, a billionaire, a household brand
- something at genuine scale: national debt, a record, a first, a collapse

Reject stories that only a professional would recognise, however large the number: asset managers buying asset managers, mid-cap share moves, fund launches, ratings changes, B2B supply contracts, a company whose name means nothing outside its industry. "$7B asset manager merger" is a bigger number than "grocery prices up 4%" and a far worse card.

Within what survives that test, strongly prefer a story whose headline or summary carries a hard number - a dollar figure, a headcount, a percentage - because the card is built around a statistic.

The headline must make sense to someone with no finance knowledge. If it needs industry vocabulary to parse, pick a different story.

Then reply with ONLY valid JSON, no markdown fences and no commentary, in \
exactly this shape:

{{
  "story_id": "short-kebab-case-slug-identifying-the-story",
  "headline_source": "one line naming the outlet and what happened",
  "category_left": "ONE OR TWO WORDS",
  "category_right": "ONE OR TWO WORDS",
  "headline": "SHORT PUNCHY ALL-CAPS HEADLINE, under 30 characters, usually a number",
  "description": [
    {{"text": "...", "color": "white"}},
    {{"text": "...", "color": "blue|red|green"}}
  ],
  "image_prompt": "one or two sentences describing a photograph",
  "caption": "social caption, 1-2 plain sentences"
}}

Rules for the description array:
- Concatenating every part's text must read as ONE natural paragraph of 2-3 short \
sentences, 120-165 characters TOTAL. It is set in large type on the card, so anything longer gets shrunk to fit and loses its impact. Include spaces at the edges of each part where a space belongs.
- color 'blue' = the main subject/behavior being highlighted.
- color 'red' = whatever it's being unfavorably compared against.
- color 'green' = the key hard number/statistic.
- color 'white' = connective/neutral text.
- Only highlight short, specific phrases -- not whole sentences.

Rules for image_prompt:
- Describe a real-looking editorial PHOTOGRAPH, concrete and visual, shot on a \
wide 16:9 frame.
- Ask for a BRIGHT, well-lit, vivid image: daylight, bright interiors, strong clean light, rich saturated colour. Explicitly avoid dark, dim, moody, murky, night-time or heavily shadowed scenes -- the card is seen as a thumbnail, and dark frames disappear in the feed.
- Absolutely no text, letters, numbers, logos, watermarks or captions anywhere.

Rules for caption:
- 1-2 sentences of plain prose. No hashtags, no links, no emoji.

Rules for story_id:
- Stable and specific to the story itself, not the date. Two runs that find the \
same underlying story must produce the same story_id.

Use ONLY facts and figures that appear in the headlines you were given. You have no other source. If the headline gives no number, either pick a different story or write the card without inventing one. Never state a figure that is not in front of you."""


# Constraining generation to this schema is what makes the parse reliable.
# Free-form JSON from the model is *usually* valid, but an unescaped quote
# inside a quote-heavy news sentence shows up often enough to break a job
# that runs twelve times a day.
CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "story_id": {"type": "string"},
        "headline_source": {"type": "string"},
        "category_left": {"type": "string"},
        "category_right": {"type": "string"},
        "headline": {"type": "string"},
        "description": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "color": {"type": "string", "enum": ["white", "blue", "red", "green"]},
                },
                "required": ["text", "color"],
                "additionalProperties": False,
            },
        },
        "image_prompt": {"type": "string"},
        "caption": {"type": "string"},
    },
    "required": ["story_id", "headline_source", "category_left", "category_right",
                 "headline", "description", "image_prompt", "caption"],
    "additionalProperties": False,
}


def _is_small_model() -> bool:
    """Haiku 4.5 and Sonnet 4.5 reject both `effort` and the newer search tool."""
    return config.COPY_MODEL.startswith(("claude-haiku", "claude-sonnet-4-5"))


def _output_config() -> dict:
    """Schema constraint, plus low effort where the model supports it.

    Haiku 4.5 rejects `effort` outright with a 400, so it can't be sent
    unconditionally — but it's worth sending where accepted, since it cut
    output tokens from ~3,700 to ~870 on Sonnet.
    """
    cfg = {"format": {"type": "json_schema", "schema": CARD_SCHEMA}}
    if not _is_small_model():
        cfg["effort"] = "low"
    return cfg


def _extract_json(text: str) -> dict:
    """Parse the reply, tolerating narration around the JSON.

    Schema-constrained output should make this trivial, but the scan is kept
    as a backstop: it tries to decode from each '{' rather than assuming the
    first one opens the object, so a stray brace in prose can't derail it.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)

    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "story_id" in obj:
            return obj
    raise ValueError(f"No parseable card JSON in model reply:\n{text[:800]}")


def find_story(exclude_ids: list[str]) -> dict:
    """Return card content for a trending story not in `exclude_ids`."""
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY())

    avoid = ""
    if exclude_ids:
        avoid = (
            "\n\nYou have already covered these stories — pick a DIFFERENT one:\n"
            + "\n".join(f"- {sid}" for sid in exclude_ids[-40:])
        )

    slot = datetime.now(timezone.utc).hour
    topics = feeds.rotation_for(slot)
    headlines = feeds.fetch(only=topics)
    fresh = feeds.drop_covered(headlines, exclude_ids)
    print(f"  slot {slot:02d}:00 reads {topics} -> {len(headlines)} headlines, "
          f"{len(headlines) - len(fresh)} already covered")
    # Keep a floor: if suppression empties the slot, better a near-repeat
    # than no card at all.
    headlines = fresh if len(fresh) >= 15 else headlines

    messages = [{
        "role": "user",
        "content": ("Current headlines:\n\n" + feeds.as_context(headlines)
                    + "\n\nPick the biggest story and write the card." + avoid),
    }]

    # No server tools any more, so no pause_turn to resume -- one call.
    for _ in range(1):
        resp = client.messages.create(
            model=config.COPY_MODEL,
            max_tokens=4000,
            system=SYSTEM,
            output_config=_output_config(),
            messages=messages,
        )
        if resp.stop_reason != "pause_turn":
            break
        messages = messages[:1] + [{"role": "assistant", "content": resp.content}]

    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write copy for this story.")

    text = "".join(b.text for b in resp.content if b.type == "text")
    content = _extract_json(text)

    missing = {"story_id", "headline", "description", "image_prompt", "caption"} - content.keys()
    if missing:
        raise ValueError(f"Model reply missing fields: {sorted(missing)}")
    return content
