"""
One-time TikTok OAuth: get a refresh token.
==========================================
Run this once by hand. It walks the authorization-code flow and prints the
refresh token, which is what the scheduled pipeline actually uses (access
tokens last 24h and are minted from it on every run).

    python scripts/tiktok_auth.py

Before running, in your TikTok app settings:
  - Add REDIRECT_URI below to the app's "Redirect URI" list, EXACTLY as
    written (TikTok requires a byte-identical match, and a web app's URI
    must be https with no query string or fragment).
  - Make sure the app has the `video.publish` scope enabled.
"""

import os
import sys
import json
import secrets
import webbrowser
import urllib.parse
from pathlib import Path

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR.parent / ".env"
load_dotenv(ENV_PATH)

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

# Must match a Redirect URI registered on the app, character for character.
# A web app needs https; only a desktop-type app may use http://localhost.
# Your GitHub Pages domain works and is already verified with TikTok.
REDIRECT_URI = os.environ.get("TIKTOK_REDIRECT_URI", "https://YOURNAME.github.io/callback")

# video.publish covers photo posts too; user.info.basic identifies the account.
SCOPES = "user.info.basic,video.publish"


def build_authorize_url(client_key: str, state: str) -> str:
    params = {
        "client_key": client_key,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def extract_code(pasted: str, expected_state: str) -> str:
    """Pull the code out of whatever the user pasted -- a full redirected URL
    or a bare code. TikTok percent-encodes the code (it usually ends in %2A),
    so it MUST be decoded before the exchange or you get invalid_grant."""
    pasted = pasted.strip()
    if "code=" in pasted:
        query = urllib.parse.urlparse(pasted).query or pasted.lstrip("?")
        parsed = urllib.parse.parse_qs(query)   # parse_qs decodes for us
        if "error" in parsed:
            sys.exit(f"TikTok returned an error: {parsed['error'][0]} "
                     f"({parsed.get('error_description', ['no description'])[0]})")
        state = parsed.get("state", [None])[0]
        if state != expected_state:
            sys.exit("State mismatch -- the redirect didn't come from the request "
                     "this script started. Re-run and don't reuse an old URL.")
        return parsed["code"][0]
    # bare code pasted straight from the address bar: may still be encoded
    return urllib.parse.unquote(pasted)


def exchange_code(client_key: str, client_secret: str, code: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,   # must match the authorize call
        },
        timeout=30,
    )
    payload = resp.json()
    if resp.status_code != 200 or "access_token" not in payload:
        sys.exit(f"Token exchange failed ({resp.status_code}):\n"
                 f"{json.dumps(payload, indent=2)}")
    return payload


def save_refresh_token(token: str):
    """Write TIKTOK_REFRESH_TOKEN into .env, replacing any existing value."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    lines = [l for l in lines if not l.startswith("TIKTOK_REFRESH_TOKEN=")]
    lines.append(f'TIKTOK_REFRESH_TOKEN="{token}"')
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote TIKTOK_REFRESH_TOKEN to {ENV_PATH}")


def main():
    client_key = os.environ.get("TIKTOK_CLIENT_KEY")
    client_secret = os.environ.get("TIKTOK_CLIENT_SECRET")
    if not client_key or not client_secret:
        sys.exit("Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env first.")
    if "YOURNAME" in REDIRECT_URI:
        sys.exit("Edit REDIRECT_URI at the top of this file (or set "
                 "TIKTOK_REDIRECT_URI in .env) to your registered redirect URI.")

    state = secrets.token_urlsafe(16)
    url = build_authorize_url(client_key, state)

    print("\n1. Approving in the browser (log in as the account you want to post as):\n")
    print(f"   {url}\n")
    webbrowser.open(url)

    print("2. After approving, TikTok redirects to your redirect URI. The page")
    print("   itself will 404 or look blank -- that's fine, the code is in the")
    print("   address bar. Copy the ENTIRE URL from the address bar.\n")
    pasted = input("Paste the full redirected URL here: ")

    code = extract_code(pasted, state)
    tokens = exchange_code(client_key, client_secret, code)

    print("\nSuccess.\n")
    print(f"  open_id            {tokens.get('open_id')}")
    print(f"  scope              {tokens.get('scope')}")
    print(f"  access_token       expires in {tokens.get('expires_in')}s (~24h, minted per run)")
    print(f"  refresh_token      expires in {tokens.get('refresh_expires_in')}s (~365d)\n")
    print(f"  TIKTOK_REFRESH_TOKEN={tokens['refresh_token']}\n")

    if input("Write this to .env? [y/N] ").strip().lower() == "y":
        save_refresh_token(tokens["refresh_token"])
    print("\nAlso add it as a GitHub Actions secret: "
          "gh secret set TIKTOK_REFRESH_TOKEN")


if __name__ == "__main__":
    main()
