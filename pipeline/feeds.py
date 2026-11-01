"""Free news headlines, used instead of the paid web-search tool.

Anthropic's web_search bills $0.01 per query AND pulls 5-12k tokens of
results into context. At twelve cards a day plus the tweet pool that was the
single largest line on the bill. RSS does the same job for nothing.

Cards split two ways across the day's twelve slots:

1. Eight TRENDING slots read what is hot right now, on any subject: Google
   Trends' US search spikes (with search volume), Google News top stories,
   and one rotating Google News section -- entertainment, sport, tech,
   world, business, science. The rotating section is what keeps two
   trending cards in a row from reading the identical list.
2. Four US slots read Google News' US section, spread evenly through the
   day.

All of it is free RSS, and each slot still hands the model at most 90
headlines, so the per-card Claude cost did not move when the niche beats
(AI, sport, conflict...) were replaced.

The older topical feeds stay for the tweet pool, which still reads them.
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; 404-pipeline/1.0)"}


def _gnews(query: str, days: int = 1) -> str:
    return (f"https://news.google.com/rss/search?q={query}+when:{days}d"
            "&hl=en-US&gl=US&ceid=US:en")


def _gnews_section(section: str) -> str:
    # Redirects to an opaque /topics/ URL; requests follows it.
    return (f"https://news.google.com/rss/headlines/section/topic/{section}"
            "?hl=en-US&gl=US&ceid=US:en")


# Google Trends' RSS puts the search volume and the matching news stories in
# its own namespace.
HT = "{https://trends.google.com/trending/rss}"


FEEDS = {
    # money
    "markets": _gnews("stock+market+OR+earnings+OR+wall+street"),
    "startups": _gnews("startup+funding+OR+venture+capital+OR+IPO"),
    "billionaires": _gnews("billionaire+OR+richest+OR+net+worth+OR+fortune"),
    "deals": _gnews("acquisition+OR+merger+OR+buyout"),
    # places
    "america": _gnews("United+States+economy+OR+American+business"),
    "us_local": _gnews("factory+OR+plant+opening+OR+hiring+OR+layoffs+OR+"
                       "small+business+OR+American+workers+OR+hometown"),
    "europe": _gnews("Europe+economy+OR+European+Union+business"),
    "asia": _gnews("Asia+economy+OR+China+business+OR+India+business+OR+Japan+economy"),
    # people and power
    "trump": _gnews("Trump+policy+OR+White+House+economy", days=2),
    "politics": _gnews("politics+OR+policy+OR+regulation"),
    # frontier
    "space": _gnews("SpaceX+OR+NASA+OR+rocket+launch+OR+satellite", days=2),
    "tech": _gnews("technology+OR+semiconductor+OR+cybersecurity"),
    # AI gets its own feed rather than sharing "tech", which buried it under
    # phone launches and chip supply stories.
    "ai": _gnews("OpenAI+OR+ChatGPT+OR+Anthropic+OR+Google+Gemini+OR+"
                 "artificial+intelligence+jobs+OR+AI+datacenter+OR+"
                 "AI+regulation+OR+AI+lawsuit"),
    "ai_money": _gnews("AI+funding+OR+AI+valuation+OR+Nvidia+OR+"
                       "AI+chip+OR+AI+investment", days=2),
    # sport, as a business and as a result
    "sports": _gnews("NFL+OR+NBA+OR+MLB+OR+college+football+OR+"
                     "Super+Bowl+OR+World+Cup"),
    # Long OR-chains starve this query -- eight terms returned 2 headlines
    # where the plain phrase returns 40. Google News is matching the phrase,
    # not the union.
    "sports_money": _gnews("sports+business", days=3),
    # conflict and contested stories
    "conflict": _gnews("Ukraine+OR+Gaza+OR+Israel+OR+Russia+war+OR+"
                       "ceasefire+OR+military+strike", days=2),
    "controversy": _gnews("lawsuit+OR+investigation+OR+scandal+OR+"
                          "protest+OR+boycott+OR+recall+OR+ban", days=2),
    # everyday life
    "citizen": _gnews("cost+of+living+OR+wages+OR+rent+OR+grocery+prices+OR+"
                      "gas+prices+OR+jobs+report+OR+social+security+OR+"
                      "health+insurance+cost"),
    "cnbc": "https://www.cnbc.com/id/10001147/device/rss/rss.html",
    # Kept out of the default set so Tesla/SpaceX don't colonise every card;
    # the reply-bait tweets ask for it explicitly.
    "musk": _gnews("Tesla+OR+SpaceX+OR+xAI+OR+Starlink", days=2),
    # --- card feeds: what is hot now, and US news --------------------------
    "trending_searches": "https://trends.google.com/trending/rss?geo=US",
    "top_stories": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
    "hot_entertainment": _gnews_section("ENTERTAINMENT"),
    "hot_sports": _gnews_section("SPORTS"),
    "hot_tech": _gnews_section("TECHNOLOGY"),
    "hot_world": _gnews_section("WORLD"),
    "hot_business": _gnews_section("BUSINESS"),
    "hot_science": _gnews_section("SCIENCE"),
    "us_news": _gnews_section("NATION"),
}

# Read only when asked for by name. The tweet pool fetches "everything but
# the AI feeds", and these would otherwise change what it sees.
OPT_IN = {"trending_searches", "top_stories", "hot_entertainment",
          "hot_sports", "hot_tech", "hot_world", "hot_business",
          "hot_science", "us_news"}

_TRENDING = ["trending_searches", "top_stories"]

# One entry per two-hour slot: eight trending, four US, with a US slot every
# third. Each trending slot adds a different section, so two consecutive
# trending cards are not drawing from the identical list.
ROTATION = [
    _TRENDING + ["hot_entertainment"],   # 00:00
    ["us_news"],                         # 02:00
    _TRENDING + ["hot_sports"],          # 04:00
    _TRENDING + ["hot_tech"],            # 06:00
    ["us_news"],                         # 08:00
    _TRENDING + ["hot_world"],           # 10:00
    _TRENDING + ["hot_business"],        # 12:00
    ["us_news"],                         # 14:00
    _TRENDING + ["hot_entertainment"],   # 16:00
    _TRENDING + ["hot_sports"],          # 18:00
    ["us_news"],                         # 20:00
    _TRENDING + ["hot_science"],         # 22:00
]

# Feeds whose slots benefit from CNBC's business wire alongside them. A
# themed slot does not: adding a business feed there simply hands the model
# an easier money story and the slot reverts to type.
MONEY_FEEDS = {"markets", "startups", "billionaires", "deals", "america",
               "us_local", "europe", "asia", "citizen"}

# What the FIRST feed of a slot commits that slot to covering. Naming it to
# the model is what makes a themed slot hold: paired with a money feed and
# left to its own judgement, the model picked the money story every time.
BEATS = {
    "ai": "artificial intelligence",
    "ai_money": "artificial intelligence and what the AI buildout costs",
    "sports": "sport",
    "sports_money": "the business of sport",
    "conflict": "war and conflict",
    "controversy": "a contested story: a lawsuit, investigation, recall, "
                   "boycott, protest or ban",
    "trump": "Trump and White House policy",
    "space": "space",
    "trending_searches": "trending",
    "us_news": "US news",
}

# Extra direction for a beat, beyond naming it. The system prompt's subject
# list was written for the old niche beats; a trending slot has to be told
# that any subject is fair game.
BEAT_NOTES = {
    "trending_searches": (
        "Pick whatever is genuinely hottest right now, on ANY subject: "
        "entertainment, celebrities, music, TV and film, sport, viral "
        "moments, tech, politics, weather, money. Lines tagged "
        "[trending_searches] are what Americans are searching for this "
        "hour, with the search volume -- lean toward the biggest volume "
        "that has a real news story attached. Treat deaths, crimes and "
        "disasters with the same rules as conflict stories."
    ),
    "us_news": (
        "Pick the biggest story happening in the United States. Treat "
        "deaths, crimes and disasters with the same rules as conflict "
        "stories."
    ),
}


def note_for(hour: int) -> str:
    """Extra direction for this slot's beat, or ""."""
    return BEAT_NOTES.get(ROTATION[(hour // 2) % len(ROTATION)][0], "")


def beat_for(hour: int) -> str:
    """The beat this slot must cover, or "" where the slot is open."""
    return BEATS.get(ROTATION[(hour // 2) % len(ROTATION)][0], "")

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def rotation_for(hour: int) -> list[str]:
    """Feeds this slot should read.

    CNBC is added only to money slots. On a sport or conflict slot it gave
    the model a business story to fall back on, and the slot quietly turned
    back into another markets card.
    """
    picked = ROTATION[(hour // 2) % len(ROTATION)]
    # Only on a slot whose LEAD feed is money. Testing "any" let CNBC onto
    # the sport slot through its paired feed, and the slot produced a
    # factory story.
    if picked[0] in MONEY_FEEDS:
        picked = picked + ["cnbc"]
    return list(dict.fromkeys(picked))


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return WS_RE.sub(" ", TAG_RE.sub(" ", text)).strip()


def _published(item: ET.Element) -> datetime | None:
    raw = item.findtext("pubDate")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def fetch(max_age_hours: int = 36, per_feed: int = 40,
          only: list[str] | None = None,
          exclude: list[str] | None = None) -> list[dict]:
    """Return recent headlines, newest first, deduped.

    `only` / `exclude` select feeds by name, so one caller can ask for the
    Musk feed and another for everything but.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    seen: set[str] = set()
    out: list[dict] = []

    for topic, url in FEEDS.items():
        if only and topic not in only:
            continue
        if exclude and topic in exclude:
            continue
        if topic in OPT_IN and not (only and topic in only):
            continue
        try:
            resp = requests.get(url, headers=UA, timeout=20)
            resp.raise_for_status()
            items = ET.fromstring(resp.content).findall(".//item")
        except Exception as e:
            # One dead feed must not take the run down; the others still work.
            print(f"  feed {topic} unavailable ({type(e).__name__})")
            continue

        if topic == "trending_searches":
            items = _trend_items(items)

        for item in items[:per_feed]:
            title = _clean(item.findtext("title"))
            if not title:
                continue
            key = title.lower()[:70]
            if key in seen:
                continue
            when = _published(item)
            if when and when < cutoff:
                continue
            seen.add(key)
            summary = _clean(item.findtext("description"))[:220]
            if topic in OPT_IN and topic != "trending_searches":
                # Google News "summaries" are the same headline again plus
                # other outlets' versions of it: tokens with nothing new.
                summary = ""
            out.append({
                "topic": topic,
                "title": title,
                "summary": summary,
                "published": when.isoformat() if when else "",
            })

    out.sort(key=lambda h: h["published"], reverse=True)
    # Search spikes first: they are the "what is hot" signal and are few, so
    # the 90-line cap in as_context must never be what cuts them.
    out.sort(key=lambda h: h["topic"] != "trending_searches")
    return out


def _trend_items(items: list[ET.Element]) -> list[ET.Element]:
    """Turn Google Trends entries into ordinary headline items.

    A bare search term is not something the model may write from -- it has
    no facts in it. Each entry is rewritten as the term, its volume, and the
    news story Google attaches to it; terms with no story are dropped.
    """
    out = []
    for item in items:
        term = _clean(item.findtext("title"))
        stories = [_clean(n.findtext(f"{HT}news_item_title"))
                   for n in item.findall(f"{HT}news_item")]
        stories = [t for t in stories if t]
        if not term or not stories:
            continue
        traffic = _clean(item.findtext(f"{HT}approx_traffic"))
        new = ET.Element("item")
        ET.SubElement(new, "title").text = (
            f"{term} ({traffic} US searches): {stories[0]}" if traffic
            else f"{term}: {stories[0]}")
        ET.SubElement(new, "description").text = " | ".join(stories[1:2])
        ET.SubElement(new, "pubDate").text = item.findtext("pubDate")
        out.append(new)
    return out


def as_context(headlines: list[dict], limit: int = 90) -> str:
    """Render headlines as compact text for the model prompt."""
    lines = []
    for h in headlines[:limit]:
        line = f"- [{h['topic']}] {h['title']}"
        if h["summary"] and h["summary"].lower() not in h["title"].lower():
            line += f" :: {h['summary']}"
        lines.append(line)
    return "\n".join(lines)


# --- suppressing stories already covered ------------------------------------
# Excluding by story_id alone does not work: the model writes a slightly
# different slug for the same story every time, so "dicks-sporting-goods-
# 25-percent-drop" never matches "dicks-sporting-goods-30-percent-crash" and
# the same company ran five days straight. Filtering the HEADLINES before the
# model ever sees them is what actually stops it.

STOP = {
    "the", "and", "for", "with", "from", "that", "this", "have", "has", "was",
    "are", "its", "into", "over", "after", "amid", "says", "said", "new",
    "more", "than", "will", "billion", "million", "trillion", "percent",
    "stock", "shares", "market", "markets", "year", "years", "week", "day",
    "report", "reports", "first", "second", "third", "record", "high", "low",
    "drop", "falls", "fall", "rise", "rises", "jump", "surge", "crash", "up",
    "down", "deal", "plan", "plans", "could", "would", "about", "how", "why",
    # generic long words, so they never count as a distinctive name
    "company", "companies", "workers", "economy", "economic", "prices",
    "growth", "revenue", "profit", "profits", "quarter", "global", "united",
    "states", "american", "america", "government", "federal", "business",
    "industry", "investors", "billionaire", "another", "against", "before",
    "during", "between", "million", "billions", "biggest", "largest",
    # short filler now that three-letter words count
    "its", "new", "now", "one", "two", "but", "not", "all", "can", "may",
    "out", "top", "get", "see", "big", "hit", "cut", "set", "end", "key",
    "use", "own", "far", "yet", "off", "per", "via", "amid", "who", "you",
}
WORD_RE = re.compile(r"[a-z]+")


def _keywords(text: str) -> set[str]:
    """Significant words: what the story is ABOUT, not how it moved.

    Three letters, not four: acronyms carry the subject in this beat -- gdp,
    cpi, ipo, fed, oil, tax. Cutting at four made "GDP hits 1.5%" and "GDP
    slumps to 1.5%" look like unrelated stories.
    """
    return {w for w in WORD_RE.findall(text.lower())
            if len(w) >= 3 and w not in STOP}


def drop_covered(headlines: list[dict], covered: list[str],
                 overlap: int = 2, name_len: int = 6) -> list[dict]:
    """Remove headlines that clearly retell a story already posted.

    `covered` is the recent story_ids -- kebab-case slugs, which tokenise
    into exactly the words we want to match on.

    Two words in common is the general test, but one distinctive name is
    enough on its own. "volkswagen-50000-job-cuts" and "Volkswagen to cut
    50,000 jobs" share only "volkswagen" once the generic words are stripped,
    and requiring two let the same story run three slots straight.
    """
    if not covered:
        return headlines
    signatures = [_keywords(c.replace("-", " ")) for c in covered]
    signatures = [sig for sig in signatures if sig]

    kept = []
    for h in headlines:
        words = _keywords(h["title"])
        repeat = False
        for sig in signatures:
            shared = words & sig
            if not shared:
                continue
            # A signature with only one or two significant words IS that word
            # -- "us-gdp-1-5-percent" reduces to {gdp}, so demanding two
            # matches would never fire.
            need = 1 if len(sig) <= 2 else overlap
            if len(shared) >= need or any(len(w) >= name_len for w in shared):
                repeat = True
                break
        if not repeat:
            kept.append(h)
    return kept
