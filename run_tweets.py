"""Every hour: post the next tweet from the day's pre-generated pool.

    python run_tweets.py [--dry-run] [--regenerate]

The pool is built by a single Haiku call per day (24 tweets), so this job
normally makes no model call at all — it just pops the next unused entry.
"""

import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime, timezone  # noqa: E402

from pipeline import assets, tweets, x  # noqa: E402

# The scheduler fires this far more often than hourly, because GitHub drops
# most scheduled events; the spacing is enforced here instead.
MIN_GAP_MINUTES = 55


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="show the next tweet without posting")
    parser.add_argument("--regenerate", action="store_true",
                        help="discard the current pool and build a fresh one")
    parser.add_argument("--force", action="store_true",
                        help="post even if the last tweet is recent")
    args = parser.parse_args()

    if not args.force and not args.dry_run:
        pool = assets.read_json(tweets.POOL_FILE, {"tweets": []})
        stamps = [t["posted_at"] for t in pool.get("tweets", [])
                  if t.get("posted_at")]
        if stamps:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(max(stamps))).total_seconds() / 60
            if age < MIN_GAP_MINUTES:
                print(f"last tweet was {age:.0f} min ago "
                      f"(< {MIN_GAP_MINUTES}); nothing to do.")
                return
            print(f"last tweet was {age:.0f} min ago -- posting")

    if args.regenerate:
        print("→ regenerating pool")
        pool = tweets.refill(force=True)
        print(f"  {len(pool['tweets'])} tweets generated")

    print("→ taking next tweet")
    tweet = tweets.take_next()
    if tweet is None:
        sys.exit("Pool is empty and could not be refilled.")

    text = x.clean_text(tweet["text"])
    print(f"  #{tweet['id']} [{tweet.get('topic', '')}] {len(text)} chars")
    print(f"  {text}")

    if args.dry_run:
        print("\n--dry-run: not posted.")
        return

    print("→ posting to X")
    post_id = x.post(text)
    print(f"  ok: {post_id}")

    tweets.mark_used(tweet["id"], post_id)
    print("\nDone.")


if __name__ == "__main__":
    main()
