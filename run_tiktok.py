"""Every 6 hours: repost the newest Instagram-published card to TikTok.

No image is generated here. TikTok reuses the most recent card that already
went out on Instagram, which is why this runs on the same hosting window —
the image has to still be live for TikTok to pull it.

    python run_tiktok.py [--dry-run]
"""

import argparse
import sys

# See run_meta.py -- non-ASCII captions crash the default Windows codepage.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from pipeline import assets, tiktok  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be posted without posting")
    args = parser.parse_args()

    print("→ reading manifest")
    manifest = assets.read_manifest()

    card = assets.latest_published_card(manifest)
    if card is None:
        sys.exit("No Instagram-published card is currently live — nothing to repost. "
                 "This is expected if the Meta run hasn't succeeded yet.")

    if card["posted"].get("tiktok"):
        print(f"Latest card {card['id']} is already on TikTok; nothing to do.")
        return

    caption = card.get("caption", "")
    title = caption.split("\n")[0][:90]
    print(f"  card:  {card['id']}")
    print(f"  image: {card['url']}")

    if args.dry_run:
        print(f"\n--dry-run: would post with title {title!r}")
        return

    print("→ posting to TikTok")
    result = tiktok.post_photo(card["url"], title=title, description=caption)
    print(f"  ok: publish_id={result['publish_id']} privacy={result['privacy_level']}")

    if result["privacy_level"] == "SELF_ONLY":
        print("  note: posted privately — TikTok forces SELF_ONLY until the app "
              "passes audit.")

    assets.update_card(card["id"], {"tiktok": True})
    print("\nDone.")


if __name__ == "__main__":
    main()
