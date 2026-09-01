"""A day's worth of standalone tweets, generated once and drained hourly.

24 tweets a day in three shapes: 12 ranked statistics lists with country
flags, 6 reply-bait posts on subjects Elon Musk engages with, and 6 plain
one-fact posts. Generating in bulk is what keeps this cheap -- two API calls
a day rather than 24.

The list subjects are chosen HERE, not by the model. Left to itself it
reaches for GDP every time: two live posts 22 minutes apart were "largest
economies by nominal GDP" and "largest economies by purchasing power". The
catalogue below is grouped into families, one list per family per day, least
recently used first, with the history carried across pools.

The FIGURES do not come from the model either. Asked for the largest banks by
assets it produced UBS at $5.0T and no Chinese bank at all, against a real
top four that is entirely Chinese. Each list is now handed the published
table (see reference.py) and asked only to format it.

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
import re
from datetime import date, datetime, timezone

from anthropic import Anthropic

from . import assets, config, feeds, reference, trends
from .text import normalise_list

POOL_FILE = "tweets.json"

# Counts are fixed so the daily X spend doesn't move: 24 posts either way.
# Lists are the format that performs, so they take half the day.
KIND_COUNTS = {"reply_bait": 6, "list": 12, "normal": 6}
POOL_SIZE = sum(KIND_COUNTS.values())

# Ranked-list subjects, grouped into families. Two things depend on the
# grouping: no two lists in the same day may come from one family (which is
# what made "largest economies" and "largest economies by PPP" land 22
# minutes apart), and within a family the least recently used subject is
# taken, so the whole catalogue cycles instead of orbiting GDP.
LIST_SUBJECTS = {
    "economy": [
        "largest economies by nominal GDP",
        "largest economies by purchasing power",
        "highest GDP per capita",
        "fastest growing economies",
    ],
    "money": [
        "largest foreign currency reserves",
        "highest inflation rates",
        "highest central bank interest rates",
        "highest government debt as a share of GDP",
    ],
    "trade": [
        "largest exporters of goods",
        "largest importers of goods",
        "busiest container ports",
    ],
    "companies": [
        "most valuable companies in the world",
        "companies with the highest revenue",
        "companies with the most employees",
    ],
    "finance": [
        "largest banks by assets",
        "largest stock exchanges by market value",
    ],
    "people": [
        "richest people in the world",
        "countries with the most billionaires",
    ],
    "energy": [
        "largest oil producers",
        "largest natural gas producers",
        "largest electricity producers",
    ],
    "commodities": [
        "largest gold reserves held by countries",
        "largest steel producers",
        "largest cement producers",
    ],
    "agriculture": [
        "largest wheat producers",
        "largest coffee producers",
        "largest wine producers",
    ],
    "defence": [
        "highest military spending",
        "largest active armed forces",
    ],
    "population": [
        "most populous countries",
        "largest cities by population",
        "countries with the oldest populations",
    ],
    "health": [
        "longest life expectancy",
        "highest healthcare spending per person",
        "most hospital beds per 1,000 people",
    ],
    "work": [
        "highest average salaries",
        "highest minimum wages",
        "highest corporate tax rates",
    ],
    "transport": [
        "busiest airports by passengers",
        "largest car producing countries",
    ],
    "knowledge": [
        "fastest average internet speeds",
        "most internet users",
        "highest research spending as a share of GDP",
    ],
    "climate": [
        "largest carbon emitters",
        "largest forest area",
    ],
    "leisure": [
        "most visited countries by tourists",
        "most valuable football clubs",
        "most Olympic medals won",
        "most powerful passports by visa free access",
    ],
}

# A subject with no table behind it would send the model back to inventing
# figures, which is the whole thing this replaced.
assert not set(sum(LIST_SUBJECTS.values(), [])) - set(reference.SOURCES), \
    "LIST_SUBJECTS contains a subject reference.py cannot source"


def pick_list_subjects(count: int, history: dict[str, str],
                       today: date) -> list[str]:
    """Choose `count` subjects, one per family, least recently used first.

    The family window rotates with the date so consecutive days do not open
    on the same subjects, and `history` (subject -> ISO date last used)
    carries across pools so the catalogue cycles rather than repeating.
    """
    families = sorted(LIST_SUBJECTS)
    offset = (today.toordinal() * count) % len(families)
    ordered = families[offset:] + families[:offset]

    chosen = []
    for family in ordered:
        if len(chosen) == count:
            break
        # "" sorts before any date, so a never-used subject wins outright.
        subjects = sorted(LIST_SUBJECTS[family],
                          key=lambda name: history.get(name, ""))
        chosen.append(subjects[0])
    return chosen


COMMON_RULES = """
Rules for every tweet:
- Stand alone. No threads, no replies, no "1/", no references to other tweets.
- NO hashtags. Not one.
- NO links or URLs of any kind.

CRITICAL - accuracy: every number, company name, fund size, percentage or \
date in a NEWS claim must come from a headline you were actually given. If \
you are not certain of a figure, leave that tweet out rather than \
approximating. These publish automatically with no human review."""

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

Write exactly {n_list} tweets of kind "list" and {n_normal} of kind "normal". \
The lists are the important half: they are what this account is followed for.

=== kind "list" ===
Write ONE list for each DATASET supplied in the user message, in the order
given, and nothing else.

The datasets are real published rankings. Your job is to format them, not to
recall them. Every name and every number must come from the rows in front of
you. Do NOT add an entry that is not in the dataset, do NOT reorder it, and
do NOT fill a gap from memory -- if the dataset is short, write a short list.

Each is a ranked table in this exact shape:

Subject of the ranking (2026)

<flag> Name ~ value
<flag> Name ~ value

- Line 1 names the subject and ends with the year in brackets. No colon. \
Then a blank line, then the rows.
- EVERY row ranks the SAME metric in the SAME unit. A row measuring \
something else does not belong - "AI share of global VC: 50%" cannot sit in \
a table of funding totals.
- EVERY row ends in a real number with its unit ("$412B", "55.0M", "39.8%"). \
No prose values like "aggressive EU push".
- Rows sorted strictly largest to smallest. $30B comes before $20B, which \
comes before $305M.
- Every row starts with a flag. Take the country from the DATASET: most of \
these tables name it in a column, and where the row is a country the flag is \
that country. Only where the dataset gives no country do you use the entity's \
headquarters, and even then NEVER derive a flag from initials, an \
abbreviation or a ticker: "GM" is General Motors, an American company, so the \
flag is the United States - not Gambia. Decide the country first, then its \
flag.
- Rankings compare DIFFERENT entities. A table where every row is the same \
country is not a ranking; pick a subject with real geographic spread.
- 8 to 10 rows where the names are short enough, fewer where they are long. \
The whole tweet must stay under 275 characters including flags, so count as \
you go and cut the tail rather than overflow.
- Round the dataset's value and mark it approximate with "~": 7,645.80 \
billion becomes "~$7.6T", 1,417,492,000 becomes "~1.42B". Keep the unit the \
dataset uses; never convert between units you are guessing at.
- Shorten long official names to what a reader recognises: "Industrial and \
Commercial Bank of China" becomes "ICBC", "United States of America" becomes \
"United States".
- Title the list after the subject and put the dataset's year in brackets. \
If the dataset names no year, leave the brackets off rather than guessing.

=== kind "normal" ===
One striking fact per tweet, fact first, under 260 characters, no emoji.

=== subjects for the "normal" tweets ===
One striking fact each, on clearly different ground: sovereign wealth funds, \
IPOs and M&A, private equity, banking, commodities, housing, demographics, \
government debt, trade, healthcare, energy, defence, shipping. Ground these \
in the headlines you were given.

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


# A row always opens with a flag, which is a pair of regional indicator
# symbols. Nothing else in these tweets uses that range, so it is an exact
# test for "this line is data, not a heading".
FLAG_RE = re.compile("[\U0001F1E6-\U0001F1FF]")

# Function words that stay lower case inside a title. Deliberately only
# these: "held", "most" and "free" are content words and read as typos in
# lower case -- "Visa free Access", "the most Billionaires".
TITLE_MINOR = {"by", "in", "the", "of", "per", "as", "a", "and",
               "with", "to", "on", "for"}


def list_title(subject: str, year: str = "") -> str:
    """Title-case a catalogue subject, e.g. "Largest Gold Reserves (2026)"."""
    words = subject.split()
    titled = [w if i and w in TITLE_MINOR else w[:1].upper() + w[1:]
              for i, w in enumerate(words)]
    title = " ".join(titled)
    return f"{title} ({year})" if year else title


def clean_list_rows(tweet: dict, subject: str = "", year: str = "") -> dict:
    """Drop rows carrying no number, and guarantee the heading.

    Two separate failures are handled here. The model occasionally fills a
    row with prose ("aggressive EU push") instead of a figure, which reads as
    a broken ranking; rows without a digit go.

    It also, in practice, skips the title outright and opens on the first
    country -- which `normalise_list` then treats as the heading, producing a
    list whose top entry sits above a blank line. Since the subject and the
    table's year are both known here, the title is built rather than asked
    for.
    """
    if tweet.get("kind") != "list":
        return tweet
    lines = tweet.get("text", "").split("\n")
    kept = [ln for i, ln in enumerate(lines)
            if i == 0 or not ln.strip() or any(c.isdigit() for c in ln)]

    while kept and not kept[0].strip():
        kept.pop(0)
    if subject and (not kept or FLAG_RE.search(kept[0])):
        kept.insert(0, list_title(subject, year))

    body = normalise_list("\n".join(kept).rstrip()).split("\n")
    # normalise_list only re-inserts the blank line when it actually
    # filters rows; the heading has to be separated either way.
    if len(body) > 1 and body[1].strip():
        body.insert(1, "")
    tweet["text"] = "\n".join(body)
    return tweet


def generate_pool(count: int = POOL_SIZE,
                  history: dict[str, str] | None = None) -> list[dict]:
    """Two grounded calls: Musk-adjacent, then everything else.

    Each is grounded on free sources rather than the paid web-search tool,
    and on a DIFFERENT slice -- which is also what stops the general tweets
    drifting onto Tesla. The lists take published tables, the news tweets
    take RSS headlines.
    """
    n_reply = KIND_COUNTS["reply_bait"]
    n_list = KIND_COUNTS["list"]

    subjects = pick_list_subjects(n_list, history or {},
                                  datetime.now(timezone.utc).date())

    # Fetch first, then size the call to what actually came back. A source
    # that has gone missing costs one list, not a fabricated one, and the
    # day still posts 24 times because the shortfall goes to plain tweets.
    datasets = [(name, rows, reference.detect_year(rows)) for name, rows in
                ((name, reference.fetch(name)) for name in subjects) if rows]
    n_list = len(datasets)
    n_normal = count - n_reply - n_list
    print(f"  {n_list} grounded lists: "
          f"{', '.join(name for name, _, _ in datasets)}")

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
        f"Datasets for the {n_list} lists, in order:{sep}"
        + f"{sep}".join(reference.as_context(name, rows)
                        for name, rows, _ in datasets)
        + f"{sep}Current headlines, for the {n_normal} normal tweets:{sep}"
        + f"{general}{sep}Write today's {n_list + n_normal} tweets.",
        ["list", "normal"],
    )
    # Tag each list with the subject it was asked for, so the pool can
    # record what has been used without re-deriving it from the title.
    lists = iter((name, year) for name, _, year in datasets)
    for tweet in out:
        if tweet.get("kind") == "list":
            tweet["subject"], tweet["year"] = next(lists, ("", ""))
    return [clean_list_rows(t, t.get("subject", ""), t.get("year", ""))
            for t in out]


def interleave(generated: list[dict]) -> list[dict]:
    """Spread the kinds across the day instead of posting them in blocks.

    Drained one per hour, an unshuffled pool would post twelve ranked lists
    back to back and then six Musk-adjacent ones. Two lists, a normal, a
    reply-bait matches the 12/6/6 split exactly and keeps the timeline varied
    whatever order the model returned.
    """
    buckets: dict[str, list[dict]] = {}
    for t in generated:
        buckets.setdefault(t.get("kind", "normal"), []).append(t)

    order, pattern = [], ["list", "list", "normal", "reply_bait"]
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
    return {"generated_at": None, "tweets": [], "list_history": {}}


def refill(force: bool = False) -> dict:
    """Generate a new pool if the current one is exhausted or from a past day."""
    pool = assets.read_json(POOL_FILE, _empty_pool())
    remaining = [t for t in pool.get("tweets", []) if not t.get("used")]

    today = datetime.now(timezone.utc).date().isoformat()
    stale = (pool.get("generated_at") or "")[:10] != today

    if not force and remaining and not stale:
        return pool

    # The history outlives the pool: it is the only thing stopping the
    # catalogue from orbiting the same handful of rankings.
    history = dict(pool.get("list_history") or {})
    generated = interleave(generate_pool(history=history))
    for tweet in generated:
        if tweet.get("subject"):
            history[tweet["subject"]] = today

    pool = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "list_history": history,
        "tweets": [
            {"id": i, "text": t["text"], "topic": t.get("topic", ""),
             "kind": t.get("kind", "normal"), "subject": t.get("subject", ""),
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
    pool.setdefault("list_history", {})
    for tweet in pool.get("tweets", []):
        if tweet["id"] == tweet_id:
            tweet["used"] = True
            tweet["post_id"] = post_id
            tweet["posted_at"] = datetime.now(timezone.utc).isoformat()
    assets.write_json(POOL_FILE, pool, f"tweet {tweet_id} posted")
