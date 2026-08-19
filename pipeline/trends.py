"""Find a trending story and write the card copy for it, in one Claude call.

Uses Claude's server-side web_search tool, so there's no separate news API
key to manage — the search runs on Anthropic's side and bills to the same
ANTHROPIC_API_KEY.

The call returns the same JSON shape automate.py already renders, plus a
`story_id` used to keep the pipeline from posting the same story twice
across the day's twelve runs.
"""

import json
import re

from anthropic import Anthropic

from . import config

SYSTEM = """You find the single most-discussed news story of the moment and \
write a "404 Media" style stat card about it.

Use the web_search tool first. Search for what is genuinely trending RIGHT NOW \
in these areas: {topics}. Prefer stories with a hard number in them — a dollar \
figure, a headcount, a percentage — because the card format is built around a \
statistic. Prefer stories from the last 24 hours.

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
  "caption": "social caption, 1-2 sentences plus 3-5 relevant hashtags"
}}

Rules for the description array:
- Concatenating every part's text must read as ONE natural paragraph (3-4 \
sentences worth), so include spaces at the edges of each part where a space belongs.
- color 'blue' = the main subject/behavior being highlighted.
- color 'red' = whatever it's being unfavorably compared against.
- color 'green' = the key hard number/statistic.
- color 'white' = connective/neutral text.
- Only highlight short, specific phrases -- not whole sentences.

Rules for image_prompt:
- Describe a real-looking editorial PHOTOGRAPH, concrete and visual, shot on a \
wide 16:9 frame with cinematic lighting and a moody, desaturated palette.
- Absolutely no text, letters, numbers, logos, watermarks or captions anywhere.

Rules for story_id:
- Stable and specific to the story itself, not the date. Two runs that find the \
same underlying story must produce the same story_id.

Only state figures you actually saw in a search result. Never invent a statistic."""


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

    messages = [{
        "role": "user",
        "content": "Find the top trending story right now and write the card." + avoid,
    }]

    # Server-side tools can stop with pause_turn when the search loop hits its
    # iteration cap; re-sending the turn resumes it where it left off.
    for _ in range(4):
        resp = client.messages.create(
            model=config.COPY_MODEL,
            max_tokens=4000,
            system=SYSTEM.format(topics=config.TOPICS),
            tools=[{"type": "web_search_20260209", "name": "web_search"}],
            output_config={"format": {"type": "json_schema", "schema": CARD_SCHEMA}},
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
