"""Publish an already-hosted card image to TikTok as a photo post.

Three things about this API are easy to get wrong and worth stating plainly:

1. Photo posts support PULL_FROM_URL only — there is no FILE_UPLOAD path for
   photos — so TikTok fetches the image from GitHub Pages, and the domain
   serving it must be verified in the TikTok developer console first.
2. creator_info must be queried before posting, because privacy_level has to
   be one of the values that endpoint returns for this specific account.
   Guessing a value gets the post rejected.
3. Refreshing rotates the refresh token *sometimes*. TikTok's docs say the
   returned token "may be different" and must replace the old one. A cron
   that ignores this works until the day rotation happens and then fails
   permanently, so the new value is written straight back to the repo secret.
"""

import base64
import json

import requests

from . import config

TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
PUBLISH_URL = "https://open.tiktokapis.com/v2/post/publish/content/init/"


def _persist_refresh_token(new_token: str) -> bool:
    """Write a rotated refresh token back into the repo's Actions secrets."""
    gh_token = config.GH_SECRETS_TOKEN()
    if not gh_token:
        print("  ! TikTok rotated the refresh token but GH_SECRETS_TOKEN is unset.")
        print("    Update the TIKTOK_REFRESH_TOKEN secret by hand or the next")
        print("    run will fail to authenticate.")
        return False

    from nacl import encoding, public

    api = f"https://api.github.com/repos/{config.ASSETS_REPO}/actions/secrets"
    headers = {"Authorization": f"Bearer {gh_token}",
               "Accept": "application/vnd.github+json"}

    key = requests.get(f"{api}/public-key", headers=headers, timeout=30).json()
    sealed = public.SealedBox(
        public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    ).encrypt(new_token.encode())

    resp = requests.put(
        f"{api}/TIKTOK_REFRESH_TOKEN",
        headers=headers,
        json={"encrypted_value": base64.b64encode(sealed).decode(),
              "key_id": key["key_id"]},
        timeout=30,
    )
    resp.raise_for_status()
    print("  · rotated refresh token persisted to repo secrets")
    return True


def get_access_token() -> str:
    """Mint a 24h access token from the long-lived refresh token."""
    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": config.TIKTOK_CLIENT_KEY(),
            "client_secret": config.TIKTOK_CLIENT_SECRET(),
            "grant_type": "refresh_token",
            "refresh_token": config.TIKTOK_REFRESH_TOKEN(),
        },
        timeout=30,
    )
    payload = resp.json()
    if "access_token" not in payload:
        raise RuntimeError(f"TikTok token refresh failed: {json.dumps(payload)}")

    returned = payload.get("refresh_token")
    if returned and returned != config.TIKTOK_REFRESH_TOKEN():
        _persist_refresh_token(returned)

    return payload["access_token"]


def _creator_info(access_token: str) -> dict:
    resp = requests.post(
        CREATOR_INFO_URL,
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": "application/json; charset=UTF-8"},
        timeout=30,
    )
    payload = resp.json()
    if payload.get("error", {}).get("code") not in (None, "ok"):
        raise RuntimeError(f"TikTok creator_info failed: {json.dumps(payload['error'])}")
    return payload.get("data", {})


def _choose_privacy(options: list[str]) -> str:
    """Prefer a public post, but fall back to whatever the account allows.

    Until the app passes TikTok's audit, SELF_ONLY is the only option this
    returns — that's TikTok policy for unaudited clients, not a bug here.
    """
    for preferred in ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR"):
        if preferred in options:
            return preferred
    return "SELF_ONLY"


def post_photo(image_url: str, title: str, description: str) -> dict:
    access_token = get_access_token()

    info = _creator_info(access_token)
    privacy = _choose_privacy(info.get("privacy_level_options", []))

    body = {
        "post_info": {
            # TikTok truncates hard; keep the title short and let the
            # description carry the detail and hashtags.
            "title": title[:90],
            "description": description[:4000],
            "privacy_level": privacy,
            "disable_comment": False,
            "auto_add_music": True,
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "photo_cover_index": 0,
            "photo_images": [image_url],
        },
        "post_mode": "DIRECT_POST",
        "media_type": "PHOTO",
    }

    resp = requests.post(
        PUBLISH_URL,
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": "application/json; charset=UTF-8"},
        json=body,
        timeout=60,
    )
    payload = resp.json()
    error = payload.get("error", {})
    if error.get("code") not in (None, "ok"):
        code = error.get("code")
        hint = ""
        if code == "url_ownership_unverified":
            hint = (f"\n    The domain serving {image_url} is not verified in the "
                    "TikTok developer console. Add it as a URL property and serve "
                    "the verification file before posting.")
        raise RuntimeError(f"TikTok publish failed: {json.dumps(error)}{hint}")

    return {"publish_id": payload["data"]["publish_id"], "privacy_level": privacy}
