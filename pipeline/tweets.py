"""A day's worth of standalone tweets, generated once and drained hourly.

One Haiku call produces 24 tweets about VC, asset managers, market
statistics and politics; the hourly job pops the next unused one. Generating
in bulk is what makes this cheap — 1 API call a day instead of 24.

The generation call is grounded with web search on purpose. These post
automatically to a public account, and a model inventing a plausible-looking
fund size or market share would be publishing fabricated financial claims
under your name. Figures must come from something it actually read.
"""

import json
from datetime import datetime, timezone

from anthropic import Anthropic

from . import assets, config, trends

POOL_FILE = "tweets.json"
POOL_SIZE = 24
MAX_SEARCHES = 3

SYSTEM = """You write standalone tweets for a media account covering venture \
capital, asset management, market statistics, and politics.

Search first, then write {count} tweets grounded in what you found.

Every tweet must:
- Stand alone. No threads, no replies, no "1/", no references to other tweets.
- Be under 260 characters.
- Lead with the most striking fact, not a wind-up.
- Contain NO hashtags. Not one.
- Contain NO links or URLs of any kind.
- Use no emoji.

CRITICAL — accuracy: every number, company name, fund size, percentage or \
date must come from a search result you actually read in this conversation. \
If you are not certain of a figure, write the tweet without it rather than \
approximating. Do not invent statistics, and do not describe a trend as \
larger or smaller than your sources support. These publish automatically \
with no human review.

Vary the subject matter across the {count} tweets — do not write {count} \
variations of one story. Mix firm-level news, market-wide statistics, \
policy/regulatory developments, and notable funding rounds."""

POOL_SCHEMA = {
    "type": "object",
    "properties": {
        "tweets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "topic": {"type": "string"},
                },
                "required": ["text", "topic"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tweets"],
    "additionalProperties": False,
}


def generate_pool(count: int = POOL_SIZE) -> list[dict]:
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY())

    cfg = {"format": {"type": "json_schema", "schema": POOL_SCHEMA}}
    if not trends._is_small_model():
        cfg["effort"] = "low"

    resp = client.messages.create(
        model=config.COPY_MODEL,
        max_tokens=8000,
        system=SYSTEM.format(count=count),
        tools=[{
            "type": ("web_search_20250305" if trends._is_small_model()
                     else "web_search_20260209"),
            "name": "web_search",
            "max_uses": MAX_SEARCHES,
        }],
        output_config=cfg,
        messages=[{"role": "user",
                   "content": f"Write today's {count} tweets."}],
    )

    text = "".join(b.text for b in resp.content if b.type == "text")
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1])
    return [t for t in data["tweets"] if t.get("text", "").strip()]


def _empty_pool() -> dict:
    return {"generated_at": None, "tweets": []}


def refill(force: bool = False) -> dict:
    """Generate a new pool if the current one is exhausted or from a past day."""
    pool = assets.read_json(POOL_FILE, _empty_pool())
    remaining = [t for t in pool.get("tweets", []) if not t.get("used")]

    today = datetime.now(timezone.utc).date().isoformat()
    stale = (pool.get("generated_at") or "")[:10] != today

    if not force and remaining and not stale:
        return pool

    generated = generate_pool()
    pool = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tweets": [
            {"id": i, "text": t["text"], "topic": t.get("topic", ""),
             "used": False, "posted_at": None, "post_id": None}
            for i, t in enumerate(generated)
        ],
    }
    assets.write_json(POOL_FILE, pool, f"tweet pool {today} ({len(generated)})")
    return pool


def take_next() -> dict | None:
    """Return the next unused tweet, refilling the pool if it's empty."""
    pool = refill()
    for tweet in pool["tweets"]:
        if not tweet.get("used"):
            return tweet
    return None


def mark_used(tweet_id: int, post_id: str):
    pool = assets.read_json(POOL_FILE, _empty_pool())
    for tweet in pool.get("tweets", []):
        if tweet["id"] == tweet_id:
            tweet["used"] = True
            tweet["post_id"] = post_id
            tweet["posted_at"] = datetime.now(timezone.utc).isoformat()
    assets.write_json(POOL_FILE, pool, f"tweet {tweet_id} posted")
