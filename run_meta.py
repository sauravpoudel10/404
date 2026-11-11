"""18 cards a day: find a story, build a card, post to IG + Facebook (+ X).

    python run_meta.py [--no-batch] [--dry-run]

Instagram and Facebook are published independently. If one fails the other
still goes out, and the manifest records exactly which landed — a card that
only reached Facebook shouldn't be silently treated as fully published, and
shouldn't be eligible for the TikTok run.
"""

import argparse
import json
import sys

# Headlines and captions routinely contain em-dashes and curly quotes, and the
# default Windows console codepage (cp1252) raises on them mid-run. Actions
# runners are already UTF-8, so this only matters for local runs.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime, timezone  # noqa: E402

from pipeline import assets, cards, feeds, meta, text, trends, video, x  # noqa: E402

# Minimum spacing between cards. The cron fires far more often; this is what
# actually sets the cadence. 18 a day is one every 80 minutes; 75 because
# pulse only checks every ten minutes, so real gaps land near 80.
MIN_GAP_MINUTES = 75

# One cycle of 18 cards, a day's worth. The beat for each slot (8 trending,
# 4 US, 6 war) is in feeds.ROTATION at the same index. Twelve posters, three
# classic, three feature, and Reels alternating with feed posts. Style and
# format are laid out together rather than alternated independently, because
# independent alternation locks them: every Reel ends up in one design and
# every feed post in another. Here each design appears in both formats.
SLOT_PATTERN = [
    ("poster", True),     # 0  trending
    ("poster", False),    # 1  war
    ("classic", True),    # 2  trending
    ("poster", False),    # 3  US
    ("poster", True),     # 4  trending
    ("feature", False),   # 5  war
    ("poster", True),     # 6  trending
    ("classic", False),   # 7  US
    ("poster", True),     # 8  war
    ("poster", False),    # 9  trending
    ("poster", True),     # 10 war
    ("feature", False),   # 11 trending
    ("poster", True),     # 12 US
    ("poster", False),    # 13 trending
    ("classic", True),    # 14 war
    ("poster", False),    # 15 trending
    ("feature", True),    # 16 US
    ("poster", False),    # 17 war
]
assert len(SLOT_PATTERN) == len(feeds.ROTATION)

# Instagram and Facebook get every card (18 a day). X gets 10 of them: every
# war card (slots 1, 5, 8, 10, 14, 17) plus four others -- trending 0 and 9,
# US 3 and 12 -- for five Reels and five feed posts.
X_WAR_SLOTS = {i for i in range(len(SLOT_PATTERN))
               if feeds.beat_for(i) == "war and conflict"}
X_SLOTS = X_WAR_SLOTS | {0, 3, 9, 12}


def next_slot(cards_by_time) -> tuple[int, str, bool]:
    """(slot, style, as_reel) for this run, one on from the last card.

    The slot index is stored on the card rather than re-derived: the pattern
    now repeats (style, format) pairs, so looking one up by value would
    always find the first occurrence and collapse the rotation onto the
    first four slots.

    Reading the previous card rather than the clock is what makes this hold
    at all -- the scheduler fires far more often than it posts, so hour
    arithmetic alternates nothing.
    """
    if not cards_by_time:
        return (0,) + SLOT_PATTERN[0]
    last = cards_by_time[-1]
    previous = last.get("slot")
    if not isinstance(previous, int):
        # An older card predates the index. Fall back to its style so the
        # rotation resumes somewhere sensible instead of restarting.
        style = last.get("style") or "feature"
        previous = next((i for i, (st, _) in enumerate(SLOT_PATTERN)
                         if st == style), -1)
    index = (previous + 1) % len(SLOT_PATTERN)
    return (index,) + SLOT_PATTERN[index]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-batch", action="store_true",
                        help="realtime image generation instead of the Batch API")
    parser.add_argument("--dry-run", action="store_true",
                        help="generate and host the card but don't post anywhere")
    parser.add_argument("--mode", choices=("auto", "reel", "post"), default="auto",
                        help="Instagram format; 'auto' alternates by slot")
    parser.add_argument("--style", choices=("auto",) + cards.automate.STYLES,
                        default="auto",
                        help="card design; 'auto' alternates by slot")
    parser.add_argument("--force", action="store_true",
                        help="post even if the last card is recent")
    args = parser.parse_args()

    print("→ reading manifest")
    manifest = assets.read_manifest()
    seen = assets.recent_story_ids(manifest)
    print(f"  {len(seen)} stories covered in the last 14 days")

    # The workflow fires hourly because GitHub drops scheduled events; the
    # spacing is enforced here instead of by cron.
    if not args.force and not args.dry_run and manifest.get("cards"):
        last = max(c["created_at"] for c in manifest["cards"])
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(last)).total_seconds() / 60
        if age < MIN_GAP_MINUTES:
            print(f"  last card was {age:.0f} min ago "
                  f"(< {MIN_GAP_MINUTES}); nothing to do.")
            return
        print(f"  last card was {age:.0f} min ago -- posting")

    cards_by_time = sorted(manifest.get("cards", []),
                           key=lambda c: c["created_at"])
    slot, slot_style, slot_reel = next_slot(cards_by_time)

    style = slot_style if args.style == "auto" else args.style
    as_reel = slot_reel if args.mode == "auto" else args.mode == "reel"
    print(f"→ slot {slot}/{len(SLOT_PATTERN)}: {style} card, "
          f"{'REEL' if as_reel else 'feed post'}")

    print(f"→ searching for a {feeds.beat_for(slot) or 'trending'} story")
    content = trends.find_story(seen, style=style, slot=slot)
    print(f"  {content['story_id']}: {content.get('headline_source', '')}")
    print(f"  headline: {content['headline']}")

    if content["story_id"] in seen:
        sys.exit(f"Model returned an already-covered story ({content['story_id']}); "
                 "skipping this slot rather than reposting.")

    print("→ generating card")
    jpeg = cards.render_jpeg(content, use_batch=not args.no_batch, style=style)
    image_status = dict(cards.automate.IMAGE_STATUS)
    print(f"  {len(jpeg) / 1024:.0f} KB JPEG, photo via {image_status['source'] or '?'}"
          + (f" ({image_status['note']})" if image_status["note"] else ""))

    mp4 = None
    if as_reel:
        print("→ rendering reel (1080x1920 MP4)")
        mp4 = video.render_reel(jpeg)
        print(f"  {len(mp4) / 1024:.0f} KB MP4")

    print("→ publishing to GitHub Pages")
    card = assets.publish_card(jpeg, content, mp4=mp4, style=style, slot=slot,
                               image=image_status)
    print(f"  {card['url']}")
    if mp4:
        print(f"  {card['video_url']}")

    # Instagram fetches this URL itself, so it has to be genuinely reachable
    # before we hand it over -- the push returning is not the same as Pages
    # having deployed.
    print("→ waiting for Pages to deploy")
    for url in [card["url"]] + ([card["video_url"]] if mp4 else []):
        if not assets.wait_until_live(url):
            sys.exit(f"Never became reachable at {url} — not posting. Check that "
                     f"Pages is serving from the '{assets.config.ASSETS_BRANCH}' branch.")

    if args.dry_run:
        print("\n--dry-run: hosted but not posted.")
        print(json.dumps({"card": card, "caption": content["caption"]}, indent=2))
        return

    # One sanitise for every destination: no hashtags, no links.
    caption = text.sanitize(content["caption"])
    posted, failures = {}, []

    # Instagram and X both alternate image/video by slot; Facebook always
    # gets the square image. Meta fetches by URL, X takes raw bytes.
    targets = [
        ("instagram",
         (lambda: meta.post_instagram_reel(card["video_url"], caption)) if as_reel
         else (lambda: meta.post_instagram(card["url"], caption))),
        ("facebook", lambda: meta.post_facebook(card["url"], caption)),
        ("x",
         (lambda: x.post_card(caption, mp4=mp4)) if as_reel
         else (lambda: x.post_card(caption, jpeg=jpeg))),
    ]

    for name, fn in targets:
        if name == "x" and slot not in X_SLOTS:
            print(f"→ skipping x (slot {slot} is not one of X's 10 a day)")
            continue
        try:
            print(f"→ posting to {name}")
            post_id = fn()
            posted[name] = True
            print(f"  ok: {post_id}")
        except Exception as e:
            posted[name] = False
            failures.append(f"{name}: {e}")
            print(f"  FAILED: {e}")

    assets.update_card(card["id"], posted)

    if failures:
        # Non-zero exit so the Actions run is visibly red; the card is still
        # hosted and whatever succeeded stays published.
        sys.exit("\nFailed:\n" + "\n".join(f"  - {f}" for f in failures))
    print("\nDone.")


if __name__ == "__main__":
    main()
