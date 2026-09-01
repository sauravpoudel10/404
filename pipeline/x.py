"""Post to X (Twitter).

Auth is OAuth 1.0a rather than OAuth 2.0 on purpose: these tokens never
expire, so unlike TikTok there is no refresh flow to run and no rotated
secret to write back.

Media upload has two entirely different shapes depending on type — images go
up in a single POST, video must use the chunked initialize/append/finalize
dance and then be polled until X finishes transcoding. Sending a video the
simple way returns a bare 400 with no explanation.
"""

import os
import time

from requests_oauthlib import OAuth1Session

from .text import sanitize

UPLOAD_URL = "https://api.x.com/2/media/upload"
TWEETS_URL = "https://api.x.com/2/tweets"

# 280 is the standard cap. This account is X Premium, which raises it to
# 25,000 -- and a 20-row ranking needs about 550. The higher figure is kept
# well below the ceiling because nothing here should ever approach it, and
# `post` falls back to 280 if X refuses, so losing Premium degrades the
# lists rather than breaking the job.
MAX_LEN = 280
LONG_MAX_LEN = 4000


def _session() -> OAuth1Session:
    """Build an OAuth 1.0a session from the four X credentials."""
    def req(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise SystemExit(f"Missing required credential: {name}")
        return value

    return OAuth1Session(
        req("X_API_KEY"), req("X_API_SECRET"),
        req("X_ACCESS_TOKEN"), req("X_ACCESS_TOKEN_SECRET"),
    )


def clean_text(text: str, limit: int = LONG_MAX_LEN) -> str:
    """Strip links and hashtags, then fit inside X's limit.

    `sanitize` drops whole rows from a multi-line post rather than cutting
    mid-row, so a list that has to shrink loses its tail cleanly instead of
    ending on a dangling flag.
    """
    return sanitize(text, limit=limit)


def upload_image(jpeg: bytes) -> str:
    resp = _session().post(
        UPLOAD_URL,
        files={"media": ("card.jpg", jpeg, "image/jpeg")},
        data={"media_category": "tweet_image"},
        timeout=120,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"X image upload failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()["data"]["id"]


def upload_video(mp4: bytes, timeout: int = 300) -> str:
    """Chunked upload, then wait for X to finish transcoding.

    Publishing before the media reports `succeeded` fails, so the poll is
    load-bearing rather than a nicety.
    """
    session = _session()

    init = session.post(f"{UPLOAD_URL}/initialize", json={
        "total_bytes": len(mp4),
        "media_type": "video/mp4",
        "media_category": "tweet_video",
    }, timeout=60)
    if init.status_code not in (200, 201):
        raise RuntimeError(f"X video INIT failed ({init.status_code}): {init.text[:300]}")
    media_id = init.json()["data"]["id"]

    append = session.post(
        f"{UPLOAD_URL}/{media_id}/append",
        files={"media": ("chunk", mp4, "application/octet-stream")},
        data={"segment_index": 0},
        timeout=300,
    )
    if append.status_code not in (200, 201, 204):
        raise RuntimeError(f"X video APPEND failed ({append.status_code}): {append.text[:300]}")

    fin = session.post(f"{UPLOAD_URL}/{media_id}/finalize", timeout=60)
    if fin.status_code not in (200, 201):
        raise RuntimeError(f"X video FINALIZE failed ({fin.status_code}): {fin.text[:300]}")

    started = time.time()
    info = (fin.json().get("data") or {}).get("processing_info")
    while info and info.get("state") in ("pending", "in_progress"):
        if time.time() - started > timeout:
            raise RuntimeError(f"X video {media_id} still transcoding after {timeout}s")
        time.sleep(max(1, info.get("check_after_secs", 3)))
        status = session.get(UPLOAD_URL, params={"command": "STATUS", "media_id": media_id},
                             timeout=30).json()
        info = (status.get("data") or status).get("processing_info")

    if info and info.get("state") == "failed":
        raise RuntimeError(f"X could not process the video: {info.get('error')}")
    return media_id


def _send(text: str, media_id: str | None):
    payload: dict = {"text": text}
    if media_id:
        payload["media"] = {"media_ids": [media_id]}
    return _session().post(TWEETS_URL, json=payload, timeout=60)


def post(text: str, media_id: str | None = None) -> str:
    body = clean_text(text)
    resp = _send(body, media_id)

    # Posts over 280 rely on Premium. If that ever lapses X rejects the whole
    # thing, so rather than lose the slot the list is trimmed to standard
    # length -- whole rows at a time -- and sent again.
    if resp.status_code not in (200, 201) and len(body) > MAX_LEN \
            and resp.status_code in (400, 403):
        short = clean_text(text, limit=MAX_LEN)
        print(f"  X refused {len(body)} chars ({resp.status_code}); "
              f"retrying at {len(short)}")
        resp = _send(short, media_id)

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"X post failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()["data"]["id"]


def post_card(caption: str, jpeg: bytes | None = None, mp4: bytes | None = None) -> str:
    """Post a card as video when one is supplied, else as an image."""
    if mp4 is not None:
        return post(caption, upload_video(mp4))
    if jpeg is not None:
        return post(caption, upload_image(jpeg))
    return post(caption)
