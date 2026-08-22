"""A day's worth of standalone tweets, generated once and drained hourly.

24 tweets a day in three shapes: 6 reply-bait posts on subjects Elon Musk
engages with, 6 ranked statistics lists with country flags, and 12 plain
one-fact posts. Generating in bulk is what keeps this cheap -- two API calls
a day rather than 24.

The two groups are generated SEPARATELY on purpose. Asking for all three
kinds in one call anchored every search on Tesla and SpaceX, and the lists
and plain posts came back as more Musk coverage. Splitting the calls keeps
the general-interest tweets genuinely general.

Generation is grounded with web search. These publish to a public account
with no human review, so a model inventing a fund size -- or putting the
wrong country's flag against a real one -- is publishing a false financial
claim under your name.
"""

import json
from datetime import datetime, timezone

from anthropic import Anthropic

from . import assets, config, feeds, trends
from .text import normalise_list

POOL_FILE = "tweets.json"

# Counts are fixed so the daily X spend doesn't move: 24 posts either way.
KIND_COUNTS = {"reply_bait": 6, "list": 6, "normal": 12}
POOL_SIZE = sum(KIND_COUNTS.values())

COMMON_RULES = """
Rules for every tweet:
- Stand alone. No threads, no replies, no "1/", no references to other tweets.
- NO hashtags. Not one.
- NO links or URLs of any kind.

CRITICAL - accuracy: every number, company name, fund size, percentage, \
ranking or date must come from a search result you actually read in this \
conversation. If you are not certain of a figure, leave that row or that \
tweet out rather than approximating. These publish automatically with no \
human review."""

REPLY_BAIT_SYSTEM = """You write standalone tweets for a media account \
covering business, technology and finance.

Write exactly {count} tweets on subjects Elon Musk actively engages with: \
Tesla, SpaceX, Starlink, xAI, X itself, EV and battery economics, launch \
cadence, robotaxis, humanoid robots, AI compute buildouts, semiconductors.

Each tweet:
- Leads with a specific, verified number or a concrete development.
- Ends with ONE genuine, answerable question about that number or decision.
- Is under 260 characters. No emoji.
- Does NOT @mention or tag anyone. No "hey @elonmusk", no baiting, no \
insults, no provocation, no flattery. A question worth answering is what \
earns a reply; anything else reads as spam and gets the account muted.

Spread them across at least four different subjects - not {count} posts \
about one company.
""" + COMMON_RULES

GENERAL_SYSTEM = """You write standalone tweets for a media account covering \
venture capital, asset management, market statistics and politics.

Write exactly {n_list} tweets of kind "list" and {n_normal} of kind "normal".

=== kind "list" ===
A ranked table, in this exact shape:

Title of the ranking:

<flag> Name: value
<flag> Name: value

- Line 1 is the title ending in a colon, then a blank line, then the rows.
- EVERY row ranks the SAME metric in the SAME unit. A row measuring \
something else does not belong - "AI share of global VC: 50%" cannot sit in \
a table of funding totals.
- EVERY row ends in a real number with its unit ("$412B", "55.0M", "39.8%"). \
No prose values like "aggressive EU push".
- Rows sorted strictly largest to smallest. $30B comes before $20B, which \
comes before $305M.
- Every row starts with the flag of the country the entity belongs to, or is \
headquartered in. NEVER derive a flag from initials, an abbreviation or a \
ticker: "GM" is General Motors, an American company, so the flag is the \
United States - not Gambia. Decide the country first, then its flag.
- Rankings compare DIFFERENT entities. A table where every row is the same \
country is not a ranking; pick a subject with real geographic spread.
- 6 to 8 rows. The whole tweet under 275 characters including flags.

=== kind "normal" ===
One striking fact per tweet, fact first, under 260 characters, no emoji.

=== subjects ===
Cover clearly different ground across these {total} tweets: sovereign wealth \
funds, IPOs and M&A, private equity, banking, commodities, housing, \
demographics, government debt, trade, healthcare, energy, defence, shipping.

Do NOT write about Tesla, SpaceX, xAI or Elon Musk - those are covered \
elsewhere. No more than two tweets about any one company.
""" + COMMON_RULES


def _schema(kinds: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "tweets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "topic": {"type": "string"},
                        "kind": {"type": "string", "enum": kinds},
                    },
                    "required": ["text", "topic", "kind"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["tweets"],
        "additionalProperties": False,
    }


def _call(system: str, user: str, kinds: list[str]) -> list[dict]:
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY())

    cfg = {"format": {"type": "json_schema", "schema": _schema(kinds)}}
    if not trends._is_small_model():
        cfg["effort"] = "low"

    resp = client.messages.create(
        model=config.COPY_MODEL,
        max_tokens=16000,
        system=system,
        output_config=cfg,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1])
    return [t for t in data.get("tweets", []) if t.get("text", "").strip()]


def clean_list_rows(tweet: dict) -> dict:
    """Drop ranking rows that carry no number.

    The model occasionally fills a row with prose ("aggressive EU push")
    instead of a figure, which reads as a broken ranking. Rows without a
    digit go; the title and the numbered rows survive.
    """
    if tweet.get("kind") != "list":
        return tweet
    lines = tweet.get("text", "").split("\n")
    kept = [ln for i, ln in enumerate(lines)
            if i == 0 or not ln.strip() or any(c.isdigit() for c in ln)]
    tweet["text"] = normalise_list("\n".join(kept).rstrip())
    return tweet


def generate_pool(count: int = POOL_SIZE) -> list[dict]:
    """Two grounded calls: Musk-adjacent, then everything else.

    Each is grounded on free RSS rather than the paid web-search tool,
    and on a DIFFERENT slice of it -- which is also what stops the
    general tweets drifting onto Tesla.
    """
    n_reply = KIND_COUNTS["reply_bait"]
    n_list = KIND_COUNTS["list"]
    n_normal = count - n_reply - n_list

    musk = feeds.as_context(feeds.fetch(only=["musk"]), limit=45)
    general = feeds.as_context(feeds.fetch(exclude=["musk"]), limit=90)
    print(f"  RSS: {len(musk.splitlines())} musk / "
          f"{len(general.splitlines())} general headlines (no search fee)")

    sep = chr(10) * 2          # blank line between context and task

    out = _call(
        REPLY_BAIT_SYSTEM.format(count=n_reply),
        f"Current headlines:{sep}{musk}{sep}Write today's {n_reply} tweets.",
        ["reply_bait"],
    )
    out += _call(
        GENERAL_SYSTEM.format(n_list=n_list, n_normal=n_normal,
                              total=n_list + n_normal),
        f"Current headlines:{sep}{general}{sep}"
        f"Write today's {n_list + n_normal} tweets.",
        ["list", "normal"],
    )
    return [clean_list_rows(t) for t in out]


def interleave(generated: list[dict]) -> list[dict]:
    """Spread the kinds across the day instead of posting them in blocks.

    Drained one per hour, an unshuffled pool would post six ranked lists back
    to back and then six Musk-adjacent ones. Two normal, a list, a reply-bait
    keeps the timeline varied whatever order the model returned.
    """
    buckets: dict[str, list[dict]] = {}
    for t in generated:
        buckets.setdefault(t.get("kind", "normal"), []).append(t)

    order, pattern = [], ["normal", "normal", "list", "reply_bait"]
    while any(buckets.values()):
        placed = False
        for kind in pattern:
            if buckets.get(kind):
                order.append(buckets[kind].pop(0))
                placed = True
        if not placed:
            for rest in buckets.values():
                order.extend(rest)
                rest.clear()
    return order


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

    generated = interleave(generate_pool())
    pool = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tweets": [
            {"id": i, "text": t["text"], "topic": t.get("topic", ""),
             "kind": t.get("kind", "normal"),
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
