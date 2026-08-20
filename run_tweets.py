"""Every hour: post the next tweet from the day's pre-generated pool.

    python run_tweets.py [--dry-run] [--regenerate]

The pool is built by a single Haiku call per day (24 tweets), so this job
normally makes no model call at all — it just pops the next unused entry.
"""

import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from pipeline import tweets, x  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="show the next tweet without posting")
    parser.add_argument("--regenerate", action="store_true",
                        help="discard the current pool and build a fresh one")
    args = parser.parse_args()

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
