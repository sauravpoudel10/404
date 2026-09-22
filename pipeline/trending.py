"""What is actually trending on X right now, for grounding the tweet pool.

`GET /2/users/personalized_trends` is the one trends endpoint this account's
tier can reach: the WOEID endpoint refuses OAuth 1.0a user context outright,
and the v1.1 trends/place endpoint is not in the subset this access level
includes. It returns a handful of trends with a category and a post count,
which is enough to write against.

Grounding matters here for the same reason it did for the ranked lists. A
model asked to write about "what is trending" writes about what was trending
when it was trained.
"""

from . import x

TRENDS_URL = "https://api.x.com/2/users/personalized_trends"


def fetch(timeout: int = 30) -> list[dict]:
    """Current trends, or [] on any failure.

    Never raises: a tweet pool grounded only in RSS is a worse pool, not a
    failed one, so this degrades rather than taking the day's run down.
    """
    try:
        resp = x._session().get(TRENDS_URL, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("data", []) or []
    except Exception as e:
        print(f"  X trends unavailable ({type(e).__name__}) -- RSS only")
        return []


def as_context(trends: list[dict]) -> str:
    """Render trends for the prompt, post counts included.

    The count is the useful part: it separates a trend with 24K posts behind
    it from one with 105, and the model should lean on the former.
    """
    if not trends:
        return ""
    lines = []
    for t in trends:
        name = (t.get("trend_name") or "").strip()
        if not name:
            continue
        count = (t.get("post_count") or "").strip()
        category = (t.get("category") or "").strip()
        bits = " · ".join(b for b in (category, count) if b)
        lines.append(f"- {name}" + (f"  [{bits}]" if bits else ""))
    return "\n".join(lines)
