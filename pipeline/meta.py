"""Publish a card to Instagram and to a Facebook Page.

Instagram publishing is a three-step handshake: create a media container
pointing at a public image URL, wait for Meta to finish fetching it, then
publish the container. The container is where fetch failures surface, so
polling it is not optional — publishing an unfinished container fails with
an error that says nothing about the real cause (usually an unreachable or
non-JPEG image).
"""

import time

import requests

from . import config


def _post(path: str, data: dict) -> dict:
    resp = requests.post(f"{config.GRAPH_API}/{path}", data=data, timeout=60)
    payload = resp.json()
    if resp.status_code != 200 or "error" in payload:
        err = payload.get("error", {})
        raise RuntimeError(
            f"Meta API {path} failed ({resp.status_code}): "
            f"{err.get('message', payload)} [type={err.get('type')} "
            f"code={err.get('code')}]"
        )
    return payload


def _wait_for_container(creation_id: str, token: str, timeout: int = 180) -> None:
    """Block until Meta has fetched the image, or explain why it couldn't."""
    started = time.time()
    while time.time() - started < timeout:
        resp = requests.get(
            f"{config.GRAPH_API}/{creation_id}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=30,
        ).json()
        status = resp.get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError(f"Instagram could not process the image: {resp.get('status')}")
        time.sleep(5)
    raise RuntimeError(f"Instagram container {creation_id} not ready after {timeout}s")


def post_instagram(image_url: str, caption: str) -> str:
    token = config.META_TOKEN()
    ig_user = config.IG_USER_ID()

    container = _post(f"{ig_user}/media", {
        "image_url": image_url,
        "caption": caption,
        "access_token": token,
    })
    creation_id = container["id"]

    _wait_for_container(creation_id, token)

    published = _post(f"{ig_user}/media_publish", {
        "creation_id": creation_id,
        "access_token": token,
    })
    return published["id"]


_page_token_cache: str | None = None


def _page_token() -> str:
    """Exchange the system-user token for a Page access token.

    Posting to a Page requires a token whose subject is the Page itself.
    A system-user token is a *user* token: Instagram publishing accepts it,
    but /{page-id}/photos does not, and Meta reports the mismatch as
    "(#200) The permission(s) publish_actions are not available" — which
    describes a permission removed in 2018 and has nothing to do with the
    actual problem. Don't chase that message; it just means wrong token type.
    """
    global _page_token_cache
    if _page_token_cache is None:
        resp = requests.get(
            f"{config.GRAPH_API}/{config.FB_PAGE_ID()}",
            params={"fields": "access_token", "access_token": config.META_TOKEN()},
            timeout=30,
        ).json()
        if "access_token" not in resp:
            raise RuntimeError(
                f"Could not derive a Page access token: {resp.get('error', resp)}"
            )
        _page_token_cache = resp["access_token"]
    return _page_token_cache


def post_instagram_reel(video_url: str, caption: str) -> str:
    """Publish a Reel.

    Same three-step handshake as a photo, but media_type=REELS and a video
    URL. Transcoding takes far longer than fetching a JPEG, so the container
    poll gets a much longer budget — a Reel container routinely sits in
    IN_PROGRESS for a minute or more.
    """
    token = config.META_TOKEN()
    ig_user = config.IG_USER_ID()

    container = _post(f"{ig_user}/media", {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "share_to_feed": "true",
        "access_token": token,
    })
    creation_id = container["id"]

    _wait_for_container(creation_id, token, timeout=600)

    published = _post(f"{ig_user}/media_publish", {
        "creation_id": creation_id,
        "access_token": token,
    })
    return published["id"]


def post_facebook(image_url: str, caption: str) -> str:
    """Publish to the Page's photo feed.

    Unlike Instagram this is a single call — the Page photos edge accepts a
    URL and publishes immediately, with no container to poll.
    """
    published = _post(f"{config.FB_PAGE_ID()}/photos", {
        "url": image_url,
        "caption": caption,
        "published": "true",
        "access_token": _page_token(),
    })
    return published.get("post_id") or published["id"]
