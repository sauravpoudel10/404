"""Free news headlines, used instead of the paid web-search tool.

Anthropic's web_search bills $0.01 per query AND pulls 5-12k tokens of
results into context. At twelve cards a day plus the tweet pool that was the
single largest line on the bill. RSS does the same job for nothing.

Two things stop the cards repeating themselves:

1. Breadth. A handful of business queries all surface the same story, which
   is how five consecutive cards ended up about Shein. There are now a dozen
   feeds across America, Europe, Asia, space, billionaires and cost-of-
   living, so there is always somewhere else to go.
2. Rotation. Each of the twelve daily slots reads a DIFFERENT pair of feeds
   (see `rotation_for`). Consecutive cards therefore cannot see the same
   headline list, which matters more than any instruction in the prompt.
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
    "tech": _gnews("technology+OR+artificial+intelligence+OR+semiconductor"),
    # everyday life
    "citizen": _gnews("cost+of+living+OR+wages+OR+rent+OR+grocery+prices+OR+"
                      "gas+prices+OR+jobs+report+OR+social+security+OR+"
                      "health+insurance+cost"),
    "cnbc": "https://www.cnbc.com/id/10001147/device/rss/rss.html",
    # Kept out of the default set so Tesla/SpaceX don't colonise every card;
    # the reply-bait tweets ask for it explicitly.
    "musk": _gnews("Tesla+OR+SpaceX+OR+xAI+OR+Starlink", days=2),
}

# One entry per two-hour slot. Each run reads a different pair, so two
# consecutive cards are drawing from different pools entirely.
ROTATION = [
    ["america", "citizen"],          # 00:00
    ["us_local", "tech"],            # 02:00
    ["markets", "citizen"],          # 04:00
    ["citizen", "us_local"],         # 06:00
    ["space", "america"],            # 08:00
    ["deals", "us_local"],           # 10:00
    ["trump", "politics"],           # 12:00
    ["asia", "america"],             # 14:00
    ["billionaires", "citizen"],     # 16:00
    ["us_local", "america"],         # 18:00
    ["europe", "citizen"],           # 20:00
    ["startups", "markets"],         # 22:00
]

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def rotation_for(hour: int) -> list[str]:
    """Feeds this slot should read. Always includes one broad feed."""
    picked = ROTATION[(hour // 2) % len(ROTATION)]
    return list(dict.fromkeys(picked + ["cnbc"]))


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
        try:
            resp = requests.get(url, headers=UA, timeout=20)
            resp.raise_for_status()
            items = ET.fromstring(resp.content).findall(".//item")
        except Exception as e:
            # One dead feed must not take the run down; the others still work.
            print(f"  feed {topic} unavailable ({type(e).__name__})")
            continue

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
            out.append({
                "topic": topic,
                "title": title,
                "summary": _clean(item.findtext("description"))[:220],
                "published": when.isoformat() if when else "",
            })

    out.sort(key=lambda h: h["published"], reverse=True)
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
