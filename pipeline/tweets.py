"""A day's worth of standalone tweets, generated once and drained every two hours.

12 posts a day in three shapes: 5 AI questions, 4 ranked statistics lists
with country flags, and 3 general questions. Generating in bulk is what
keeps this cheap -- three API calls a day rather than 12.

The mix follows what the open-sourced ranker scores. A reply is one signal
and a reply the author engages with is a second, separate one, so most of
the day is posts that end in a real question; a list earns a bookmark but
rarely a conversation. Report carries the heaviest penalty weight in the
file, which is why none of this is allowed to read as bait.

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

from . import assets, config, feeds, reference, trending, trends
from .text import normalise_list

POOL_FILE = "tweets.json"

# Counts are fixed so the daily X spend doesn't move: 12 posts either way.
# Weighted toward posts that end in a real question, because a reply and an
# author-engaged reply are two separately scored signals in the ranker,
# while a list mostly earns a silent bookmark.
KIND_COUNTS = {"ai_ask": 5, "list": 4, "ask": 3}
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

ASK_RULES = """
Every one of these posts has the same two-part shape:

  1. INFORMATION FIRST. Two or three sentences of something concrete a \
reader did not already know: a benchmark result, a capability, a price, a \
limit, a change. Specific enough to be worth reading even if nobody answers.
  2. ONE QUESTION LAST. A single, genuine, answerable question that follows \
from what you just said. It must be a question a practitioner would have an \
opinion about and could answer in one line.

Rules on the question:
- Ask about the reader's own experience and choices, not their feelings. \
"Which model do you reach for when you need long-context reasoning?" is a \
question. "Thoughts?" is not, and "Is AI going to take our jobs?" is not.
- ONE question mark in the whole post. Never two.
- Never @mention or tag anyone, and never address a person or company \
directly. No "hey @xai". No "agree?", no "RT if", no "drop a comment", no \
"who else", no engagement-bait phrasing of any kind. A post that reads as \
bait earns reports, and a report is the heaviest penalty the ranker carries.
- Do not answer your own question, and do not imply a right answer.

Rules on the information:
- ACCURACY IS ABSOLUTE. Every benchmark score, version number, price, \
context length, release date and company claim must appear in the material \
you were given in this conversation. If you did not read it here, you do \
not know it.
- Benchmarks and specs especially: do NOT write an MMLU, SWE-bench, GPQA \
or ARC score, a context window size, a parameter count or a token price \
unless that exact figure is in front of you. "Opus 4's 200K context window" \
is the kind of sentence that gets written from memory and is wrong; posts \
containing one are discarded before they reach the timeline. Where you have \
no number, write about the capability or the change instead and ask your \
question about that. An invented benchmark is the fastest way to lose this \
account's credibility with the only audience that would reply.
- Where a figure is a company's own claim, say so: "OpenAI says", "per \
Anthropic's card".
- Under 270 characters. No emoji. No hashtags. No links."""

AI_ASK_SYSTEM = """You write standalone posts for a media account followed \
by people who build with AI models and argue about them.

Write exactly {count} posts about AI models and the systems around them: \
model releases and versions, benchmark results, context windows, pricing \
and rate limits, coding and agent performance, inference cost, chips and \
compute, evaluation methodology and where benchmarks mislead, open weights \
versus closed, and AI regulation and lawsuits where they bite builders.

The audience actually uses these tools. Write for someone choosing between \
models this week, not for a general reader. Assume they know what a context \
window is; do not explain it.

Spread the {count} posts across at least five different subjects and at \
least four different companies. Do not write {count} posts about one lab.

At least three of them should ask directly which model or tool the reader \
uses for a specific named task -- long-context work, code review, agentic \
loops, cheap bulk classification, OCR and document extraction, local \
inference -- and should give a concrete reason the choice is non-obvious \
before asking.
""" + ASK_RULES

ASK_SYSTEM = """You write standalone posts for a media account covering \
business, markets, money and sport.

Write exactly {count} posts, each leading with a concrete fact from the \
material you were given and ending on one genuine question.

Cover clearly different ground: markets, company results, sovereign wealth, \
housing, energy, trade, sport economics, salaries, consumer prices. Do NOT \
write about AI models -- those are covered elsewhere.
""" + ASK_RULES


GENERAL_SYSTEM = """You write standalone tweets for a media account covering \
venture capital, asset management, market statistics and politics.

Write exactly {n_list} tweets of kind "list".

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
- LENGTH: use as many rows as the dataset gives you, up to 30. Thirty is the \
target, not a ceiling to avoid - the depth of the ranking is the point of \
the format, so do not stop at ten because it looks tidy. Never post fewer \
than 10; if the dataset cannot supply 10 usable rows, write no list for it \
at all. Keep the whole tweet under 1200 characters.
- Round the dataset's value and mark it approximate with "~": 7,645.80 \
billion becomes "~$7.6T", 1,417,492,000 becomes "~1.42B". Keep the unit the \
dataset uses; never convert between units you are guessing at.
- Shorten long official names to what a reader recognises: "Industrial and \
Commercial Bank of China" becomes "ICBC", "United States of America" becomes \
"United States".
- Title the list after the subject and put the dataset's year in brackets. \
If the dataset names no year, leave the brackets off rather than guessing.

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


# Claims a model states with total confidence and no entitlement: how big a
# context window is, how many parameters a model has, what a token costs,
# and what it scored on a named benchmark.
SPEC_RE = re.compile(
    r"\b\d[\d.,]*\s*[kKmMbB]?\s*(?:context|token|parameter)"
    r"|\bcontext\s+window\s+of\s+\d"
    r"|\b(?:MMLU|SWE-?bench|GPQA|ARC-?AGI|HumanEval|HellaSwag|MATH-500|"
    r"AIME|LiveBench|MMMU)\b",
    re.I,
)
BENCH_RE = re.compile(
    r"\b(?:MMLU|SWE-?bench|GPQA|ARC-?AGI|HumanEval|HellaSwag|"
    r"MATH-500|AIME|LiveBench|MMMU)\b", re.I)
DIGITS_RE = re.compile(r"[^a-z0-9]")


MAGNITUDE_RE = re.compile(r"(\d[\d.,]*)\s*([kmb])\b", re.I)
_SCALE = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _expand(match: re.Match) -> str:
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return match.group(0)
    return str(int(value * _SCALE[match.group(2).lower()]))


def _flat(text: str) -> str:
    """Lowercase, expand magnitudes, strip punctuation.

    "200K" and "200,000" both become "200000", so a post that abbreviates a
    figure the source spelled out is not mistaken for an invented one.
    "SWE-bench" and "SWE bench" collapse together the same way.
    """
    return DIGITS_RE.sub("", MAGNITUDE_RE.sub(_expand, text.lower()))


def unsourced_spec(text: str, grounding: str) -> str:
    """The first model-spec claim in `text` absent from `grounding`, or "".

    Checks the figure rather than the phrasing: "200K context window" is
    sourced by a headline saying "200,000 token context window", because
    the number is what could have been invented, not the wording. Benchmark
    claims are checked on the benchmark's name instead, since the score
    means nothing without it.
    """
    haystack = _flat(grounding)
    for match in SPEC_RE.finditer(text):
        claim = match.group(0).strip()
        named = BENCH_RE.search(claim)
        if named:
            needle = _flat(named.group(0))
        else:
            number = MAGNITUDE_RE.search(claim) or re.search(r"\d[\d.,]*", claim)
            if not number:
                continue
            needle = _flat(number.group(0))
        if needle and needle not in haystack:
            return claim
    return ""


def drop_unsourced(tweets: list[dict], grounding: str) -> list[dict]:
    """Remove AI posts quoting a spec the source never supplied."""
    kept = []
    for tweet in tweets:
        if tweet.get("kind") == "ai_ask":
            bad = unsourced_spec(tweet.get("text", ""), grounding)
            if bad:
                print(f"    dropped ai post: unsourced spec {bad!r}")
                continue
        kept.append(tweet)
    return kept


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

    body = normalise_list("\n".join(kept).rstrip(), min_rows=10).split("\n")
    # normalise_list only re-inserts the blank line when it actually
    # filters rows; the heading has to be separated either way.
    if len(body) > 1 and body[1].strip():
        body.insert(1, "")
    tweet["text"] = "\n".join(body)
    return tweet


def generate_pool(count: int = POOL_SIZE,
                  history: dict[str, str] | None = None) -> list[dict]:
    """Three grounded calls: AI questions, ranked lists, general questions.

    Nothing here is written from the model's own recollection. The lists get
    published tables, the AI posts get live X trends plus an AI headline
    feed, and the general posts get the rest of the RSS. A model asked what
    is trending answers with what was trending when it was trained.
    """
    n_ai = KIND_COUNTS["ai_ask"]
    n_list = KIND_COUNTS["list"]

    subjects = pick_list_subjects(n_list, history or {},
                                  datetime.now(timezone.utc).date())

    # Fetch first, then size the calls to what actually came back. A source
    # that has gone missing costs one list, not a fabricated one, and the
    # day still posts 12 times because the shortfall goes to questions.
    datasets = [(name, rows, reference.detect_year(rows)) for name, rows in
                ((name, reference.fetch(name)) for name in subjects) if rows]
    n_list = len(datasets)
    print(f"  {n_list} grounded lists: "
          f"{', '.join(name for name, _, _ in datasets)}")

    trends = trending.as_context(trending.fetch())
    ai_news = feeds.as_context(feeds.fetch(only=["ai", "ai_money", "musk"]),
                               limit=60)
    general = feeds.as_context(
        feeds.fetch(exclude=["ai", "ai_money", "musk"]), limit=80)
    print(f"  X trends: {len(trends.splitlines())} · "
          f"AI headlines: {len(ai_news.splitlines())} · "
          f"general: {len(general.splitlines())}")

    sep = chr(10) * 2          # blank line between context and task

    trend_block = (f"Trending on X right now:{sep}{trends}{sep}"
                   if trends else "")
    grounding = f"{trends}{sep}{ai_news}"
    out = _call(
        AI_ASK_SYSTEM.format(count=n_ai),
        f"{trend_block}AI headlines:{sep}{ai_news}{sep}"
        f"Write today's {n_ai} posts. Where a trend above is about AI and "
        f"has real volume behind it, write about that -- it is what people "
        f"are arguing about today.",
        ["ai_ask"],
    )
    out = drop_unsourced(out, grounding)
    # Whatever the guard removed becomes a general question, so the day
    # still posts 12 times rather than going short.
    n_ask = count - len(out) - n_list
    out += _call(
        GENERAL_SYSTEM.format(n_list=n_list),
        f"Datasets for the {n_list} lists, in order:{sep}"
        + f"{sep}".join(reference.as_context(name, rows)
                        for name, rows, _ in datasets)
        + f"{sep}Write today's {n_list} lists.",
        ["list"],
    )
    out += _call(
        ASK_SYSTEM.format(count=n_ask),
        f"{trend_block}Current headlines:{sep}{general}{sep}"
        f"Write today's {n_ask} posts.",
        ["ask"],
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

    Drained one every two hours, an unshuffled pool would post five AI
    questions back to back and then four lists. Alternating a question with a list
    keeps consecutive posts in different shapes, which matters more on a
    timeline than the order the model happened to return.
    """
    buckets: dict[str, list[dict]] = {}
    for t in generated:
        buckets.setdefault(t.get("kind", "normal"), []).append(t)

    order, pattern = [], ["ai_ask", "list", "ask", "ai_ask", "list"]
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
