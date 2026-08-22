"""Free news headlines, used instead of the paid web-search tool.

Anthropic's web_search bills $0.01 per search AND pulls 5-12k tokens of
results into context. At twelve cards a day plus the tweet pool that was
the single largest line on the bill.

RSS gives the same job for nothing: a few hundred current headlines with
summaries, fetched in about a second, no API key. The model then picks a
story and writes the card from that context, so there is no search fee at
all and the input is roughly a fifth the size.

The trade is reach: RSS sees what these feeds publish, not the whole web.
For "what is trending in business right now" that is the same thing.
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; 404-pipeline/1.0)"}

FEEDS = {
    "business": "https://news.google.com/rss/search?q=business+OR+finance+OR+markets+when:1d&hl=en-US&gl=US&ceid=US:en",
    "tech": "https://news.google.com/rss/search?q=technology+OR+startup+OR+AI+when:1d&hl=en-US&gl=US&ceid=US:en",
    "money": "https://news.google.com/rss/search?q=funding+OR+acquisition+OR+IPO+OR+earnings+when:1d&hl=en-US&gl=US&ceid=US:en",
    "politics": "https://news.google.com/rss/search?q=politics+OR+policy+OR+regulation+when:1d&hl=en-US&gl=US&ceid=US:en",
    "cnbc": "https://www.cnbc.com/id/10001147/device/rss/rss.html",
    # Kept separate so the reply-bait tweets can be sourced without dragging
    # Tesla and SpaceX into every other tweet in the pool.
    "musk": "https://news.google.com/rss/search?q=Tesla+OR+SpaceX+OR+xAI+OR+Starlink+when:2d&hl=en-US&gl=US&ceid=US:en",
}

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


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
          only: list[str] | None = None, exclude: list[str] | None = None) -> list[dict]:
    """Return recent headlines, newest first, deduped.

    `only` / `exclude` select feeds by topic so one caller can ask for the
    Musk feed and another can ask for everything but.
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
