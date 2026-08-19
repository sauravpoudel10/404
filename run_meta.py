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

from pipeline import assets, cards, meta, trends  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-batch", action="store_true",
                        help="realtime image generation instead of the Batch API")
    parser.add_argument("--dry-run", action="store_true",
                        help="generate and host the card but don't post anywhere")
    args = parser.parse_args()

    print("→ reading manifest")
    manifest = assets.read_manifest()
    seen = assets.recent_story_ids(manifest)
    print(f"  {len(seen)} stories covered in the last 14 days")

    print("→ searching for a trending story")
    content = trends.find_story(seen)
    print(f"  {content['story_id']}: {content.get('headline_source', '')}")
    print(f"  headline: {content['headline']}")

    if content["story_id"] in seen:
        sys.exit(f"Model returned an already-covered story ({content['story_id']}); "
                 "skipping this slot rather than reposting.")

    print("→ generating card")
    jpeg = cards.render_jpeg(content, use_batch=not args.no_batch)
    print(f"  {len(jpeg) / 1024:.0f} KB JPEG")

    print("→ publishing to GitHub Pages")
    card = assets.publish_card(jpeg, content)
    print(f"  {card['url']}")

    if args.dry_run:
        print("\n--dry-run: hosted but not posted.")
        print(json.dumps({"card": card, "caption": content["caption"]}, indent=2))
        return

    caption = content["caption"]
    posted, failures = {}, []

    for name, fn in (("instagram", meta.post_instagram), ("facebook", meta.post_facebook)):
        try:
            print(f"→ posting to {name}")
            post_id = fn(card["url"], caption)
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
