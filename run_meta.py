"""Every 2 hours: find a trending story, build a card, post to IG + Facebook.

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

from pipeline import assets, cards, meta, text, trends, video, x  # noqa: E402

# Minimum spacing between cards. The cron fires hourly; this is what
# actually sets the cadence.
MIN_GAP_MINUTES = 100

# Twelve cards a day, split evenly two ways: six in each design, six as
# Reels and six as feed posts. Alternating the two independently off the
# previous card would lock them together -- every Reel would end up in one
# style and every feed post in the other -- so they rotate as PAIRS, and all
# four combinations come round every four cards.
SLOT_PATTERN = [
    ("feature", True),      # full-bleed portrait, as a Reel
    ("classic", False),     # square photo band, as a feed post
    ("feature", False),
    ("classic", True),
]


def next_slot(cards_by_time) -> tuple[str, bool]:
    """(style, as_reel) for this run, taken from where the last card sat.

    Derived from the previous card rather than the clock: the scheduler
    fires far more often than it posts, so hour arithmetic no longer
    alternates anything.
    """
    if not cards_by_time:
        return SLOT_PATTERN[0]
    last = cards_by_time[-1]
    previous = (last.get("style") or "feature", bool(last.get("video_file")))
    try:
        index = SLOT_PATTERN.index(previous)
    except ValueError:
        index = -1          # unrecognised (older card): restart the rotation
    return SLOT_PATTERN[(index + 1) % len(SLOT_PATTERN)]


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
    slot_style, slot_reel = next_slot(cards_by_time)

    style = slot_style if args.style == "auto" else args.style
    as_reel = slot_reel if args.mode == "auto" else args.mode == "reel"
    print(f"→ slot: {style} card, {'REEL' if as_reel else 'feed post'}")

    print("→ searching for a trending story")
    content = trends.find_story(seen, style=style)
    print(f"  {content['story_id']}: {content.get('headline_source', '')}")
    print(f"  headline: {content['headline']}")

    if content["story_id"] in seen:
        sys.exit(f"Model returned an already-covered story ({content['story_id']}); "
                 "skipping this slot rather than reposting.")

    print("→ generating card")
    jpeg = cards.render_jpeg(content, use_batch=not args.no_batch, style=style)
    print(f"  {len(jpeg) / 1024:.0f} KB JPEG")

    mp4 = None
    if as_reel:
        print("→ rendering reel (1080x1920 MP4)")
        mp4 = video.render_reel(jpeg)
        print(f"  {len(mp4) / 1024:.0f} KB MP4")

    print("→ publishing to GitHub Pages")
    card = assets.publish_card(jpeg, content, mp4=mp4, style=style)
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
