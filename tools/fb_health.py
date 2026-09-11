"""Report the health of the Facebook Page connection.

    python tools/fb_health.py

Answers the questions that keep coming up about the Page: is the token still
good and for how long, what can it actually do, are the posts real and
public, and — once read_insights is granted — is anybody seeing them.

Reach is the one thing that separates "the posts are not there" from "the
posts are there and nobody is being shown them", and those two have
completely different fixes. Without read_insights the script says so rather
than guessing.
"""
import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from pipeline import config, meta  # noqa: E402

G = config.GRAPH_API
# Meta renamed the post metrics; older names return "must be a valid insights
# metric" rather than a permission error, which reads like a scope problem
# when it is not. Try the current names first, then the legacy ones.
REACH_METRICS = ("post_impressions_unique,post_impressions",
                 "post_reach,post_views")


def rule(title: str):
    print(f"\n{title}\n" + "-" * len(title))


def main() -> int:
    problems = []
    system_token = os.environ.get("META_SYSTEM_USER_TOKEN", "")
    if not system_token:
        sys.exit("META_SYSTEM_USER_TOKEN is not set; nothing to check.")
    page_token = meta._page_token()
    page_id = config.FB_PAGE_ID()

    rule("token")
    info = requests.get(f"{G}/debug_token", timeout=30, params={
        "input_token": system_token, "access_token": system_token,
    }).json().get("data", {})
    scopes = sorted(info.get("scopes", []))
    print(f"  app        {info.get('application')}")
    print(f"  valid      {info.get('is_valid')}")
    print(f"  scopes     {', '.join(scopes)}")

    stamp = info.get("data_access_expires_at") or info.get("expires_at")
    if stamp:
        when = datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc)
        left = (when - datetime.datetime.now(datetime.timezone.utc)).days
        print(f"  expires    {when:%Y-%m-%d}  ({left} days)")
        if left < 21:
            problems.append(f"Token data access expires in {left} days — renew it.")

    for needed in ("pages_manage_posts", "pages_read_engagement"):
        if needed not in scopes:
            problems.append(f"Missing scope {needed}.")
    if "read_insights" not in scopes:
        problems.append(
            "Missing scope read_insights — reach cannot be measured, so a "
            "distribution problem is indistinguishable from a display one.")

    rule("app")
    app_id = info.get("app_id")
    app = requests.get(f"{G}/{app_id}", timeout=30, params={
        "access_token": system_token, "fields": "name,privacy_policy_url",
    }).json()
    print(f"  name               {app.get('name')}")
    print(f"  privacy_policy_url {app.get('privacy_policy_url') or '(unset)'}")
    # Meta refuses to switch an app to Live without a privacy policy URL, so
    # an unset one means the app is still in Development mode -- and content
    # published by a Development-mode app is visible ONLY to people with a
    # role on the app. That is the whole "posts exist but nobody can see
    # them" symptom, and no amount of posting differently changes it.
    if not app.get("privacy_policy_url"):
        problems.append(
            "App has no privacy policy URL, so it cannot be Live: it is in "
            "Development mode, and Facebook shows Development-mode posts ONLY "
            "to people with a role on the app. Followers see nothing and "
            "permalinks read as invalid. Fix: developers.facebook.com -> app "
            "-> Settings -> Basic -> set Privacy Policy URL, then switch the "
            "top-bar toggle from In development to Live.")

    rule("page")
    page = requests.get(f"{G}/{page_id}", timeout=30, params={
        "access_token": page_token,
        "fields": "name,is_published,verification_status,fan_count,followers_count",
    }).json()
    for key in ("name", "is_published", "verification_status",
                "fan_count", "followers_count"):
        if key in page:
            print(f"  {key:<20} {page[key]}")
    if page.get("is_published") is False:
        problems.append("The Page itself is unpublished — nothing is visible.")

    # A page-managing token being refused on its own feed is the signature of
    # Standard rather than Advanced access, i.e. an app that has not been
    # through review or is still in development mode.
    feed = requests.get(f"{G}/{page_id}/feed", timeout=30,
                        params={"access_token": page_token, "limit": 1})
    if feed.status_code != 200:
        print(f"\n  /feed refused ({feed.status_code}): "
              f"{feed.json().get('error', {}).get('message', '')[:110]}")
        problems.append(
            "Reading /feed is refused despite the scope being granted. That "
            "usually means the app holds Standard rather than Advanced access "
            "— check whether the app is still in Development mode.")

    rule("recent posts")
    posts = requests.get(f"{G}/{page_id}/published_posts", timeout=30, params={
        "access_token": page_token, "limit": 8,
        "fields": "id,created_time,status_type,is_hidden,is_expired,permalink_url",
    }).json().get("data", [])
    if not posts:
        problems.append("No published posts returned at all.")
    for p in posts:
        flags = []
        if p.get("is_hidden"):
            flags.append("HIDDEN")
        if p.get("is_expired"):
            flags.append("EXPIRED")
        print(f"  {p['created_time'][:16]}  {p.get('status_type'):<14} "
              f"{' '.join(flags) or 'ok'}")
        print(f"      {p.get('permalink_url')}")
    if any(p.get("is_hidden") or p.get("is_expired") for p in posts):
        problems.append("Some posts are hidden or expired.")

    rule("reach")
    if posts:
        for metrics in REACH_METRICS:
            r = requests.get(f"{G}/{posts[0]['id']}/insights", timeout=30,
                             params={"access_token": page_token, "metric": metrics})
            if r.status_code == 200 and r.json().get("data"):
                for post in posts[:5]:
                    got = requests.get(f"{G}/{post['id']}/insights", timeout=30,
                                       params={"access_token": page_token,
                                               "metric": metrics}).json()
                    vals = {m["name"]: m["values"][0]["value"]
                            for m in got.get("data", [])}
                    print(f"  {post['created_time'][:16]}  {vals}")
                break
        else:
            print("  unavailable — grant read_insights to the system user.")

    rule("summary")
    if problems:
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("  Nothing wrong found at the API level.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
