"""Shared configuration and credential loading.

Every credential comes from the environment. Locally that's the .env file
next to this package; in GitHub Actions it's repository secrets, which the
workflows map onto the same variable names — so nothing in the pipeline
knows or cares which it is.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _req(name: str) -> str:
    """Fail loudly and early on a missing credential.

    A cron job that half-runs and dies mid-publish is much worse than one
    that refuses to start, so entrypoints call this up front rather than
    discovering the gap after an image has already been generated.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required credential: {name}")
    return value


def _opt(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# --- content ---------------------------------------------------------------
ANTHROPIC_API_KEY = lambda: _req("ANTHROPIC_API_KEY")          # noqa: E731
GEMINI_API_KEY = lambda: _req("GEMINI_API_KEY")                # noqa: E731

# Sonnet rather than Opus: this runs 12x/day and the task (pick a trending
# story, write 60 words of copy) is well inside Sonnet's range. Bump to
# claude-opus-5 here if the copy quality doesn't hold up.
COPY_MODEL = _opt("COPY_MODEL", "claude-sonnet-5")

TOPICS = _opt(
    "TOPICS",
    "business, technology, finance, startup founders, politics",
)

# --- hosting ---------------------------------------------------------------
# Instagram and TikTok both fetch the image over HTTPS from these URLs, so
# this must stay publicly reachable for the lifetime of a post's creation.
ASSETS_REPO = _opt("ASSETS_REPO", "sauravpoudel10/404")
PUBLIC_BASE_URL = _opt("PUBLIC_BASE_URL", "https://sauravpoudel10.github.io/404").rstrip("/")
ASSETS_BRANCH = _opt("ASSETS_BRANCH", "gh-pages")
RETENTION_HOURS = int(_opt("RETENTION_HOURS", "48"))

# In Actions this is the workflow's built-in GITHUB_TOKEN; locally it's a PAT.
GITHUB_TOKEN = lambda: _req("ASSETS_REPO_TOKEN")               # noqa: E731

# --- destinations ----------------------------------------------------------
META_TOKEN = lambda: _req("META_SYSTEM_USER_TOKEN")            # noqa: E731
IG_USER_ID = lambda: _req("IG_USER_ID")                        # noqa: E731
FB_PAGE_ID = lambda: _req("FB_PAGE_ID")                        # noqa: E731
GRAPH_API = "https://graph.facebook.com/v21.0"

TIKTOK_CLIENT_KEY = lambda: _req("TIKTOK_CLIENT_KEY")          # noqa: E731
TIKTOK_CLIENT_SECRET = lambda: _req("TIKTOK_CLIENT_SECRET")    # noqa: E731
TIKTOK_REFRESH_TOKEN = lambda: _req("TIKTOK_REFRESH_TOKEN")    # noqa: E731
GH_SECRETS_TOKEN = lambda: _opt("GH_SECRETS_TOKEN")            # noqa: E731
