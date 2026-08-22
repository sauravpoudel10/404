# 404-style card pipeline

Finds a trending story, renders a "404 Media" style stat card, hosts it on
GitHub Pages, and posts it to Instagram, Facebook, and TikTok on a schedule.

```
run_meta.py     every 2h (12/day)  trending story -> card -> Instagram + Facebook + X
run_tweets.py   every 1h (24/day)  next tweet from the day's pre-generated pool -> X
run_tiktok.py   manual only        reposts the newest IG-published card to TikTok
```

X mirrors Instagram's format: video on Reel slots, image otherwise. The hourly
tweets are text-only and come from a pool of 24 built by a single model call
each day, so 24 posts cost one API call rather than 24.

Instagram alternates format by slot: even-4 hours (00, 04, 08, 12, 16, 20 UTC)
publish a **Reel**, the rest publish a **feed post** — six of each per day.
Facebook always gets the square image. Override with `--mode reel|post`.

Reels are video-only, so the square card is composed onto a blurred 1080x1920
backdrop and encoded to a 7-second H.264 MP4 (`pipeline/video.py`). ffmpeg
ships with the `imageio-ffmpeg` wheel — nothing to install.

Drop royalty-free tracks in `audio/` and one is picked per Reel, looped to
length with a fade in/out; leave it empty and Reels are silent. See
`audio/README.md` — Instagram's own catalogue is not reachable via the API,
and commercial music baked into the file gets the Reel muted by Content ID.

TikTok's schedule is commented out in its workflow: an unaudited app can only
post privately, so a timer just generates failed runs. Re-enable the cron once
the app passes audit.

TikTok generates no image of its own — it reuses the most recent card that
actually reached Instagram.

## How it fits together

| Module | Job |
|---|---|
| `pipeline/feeds.py` | Free RSS headlines — replaces the paid web-search tool, which was the largest line on the bill |
| `pipeline/trends.py` | One Claude call over those headlines: picks a trending story and writes the card copy, schema-constrained so the JSON always parses |
| `automate.py` | Renders the SVG card (Gemini generates the background photo) and rasterizes it |
| `pipeline/cards.py` | Converts to JPEG — Instagram rejects PNG |
| `pipeline/assets.py` | Publishes to the `gh-pages` branch, expires cards after 48h, holds the manifest that doubles as pipeline state |
| `pipeline/meta.py` | Instagram (3-step container handshake) and Facebook Page publishing |
| `pipeline/tiktok.py` | Token refresh + photo post via `PULL_FROM_URL` |

Images must be publicly hosted because neither Instagram nor TikTok accepts
uploaded bytes for this flow — both fetch the image over HTTPS.

## One-time setup

**1. Pages** — in repo Settings → Pages, set the source to branch `gh-pages`,
folder `/ (root)`. The branch is created by the first successful run.

**2. Repository variable** — Settings → Secrets and variables → Actions →
Variables:

```
PUBLIC_BASE_URL = https://sauravpoudel10.github.io/404
```

**3. Repository secrets** — same page, Secrets tab:

```
ANTHROPIC_API_KEY        GEMINI_API_KEY
META_SYSTEM_USER_TOKEN   IG_USER_ID          FB_PAGE_ID
TIKTOK_CLIENT_KEY        TIKTOK_CLIENT_SECRET  TIKTOK_REFRESH_TOKEN
GH_SECRETS_TOKEN
```

`GH_SECRETS_TOKEN` is a fine-grained PAT with **Secrets: write** on this repo.
TikTok may rotate the refresh token on any refresh, and the pipeline writes the
new value straight back into the secret. Without this the pipeline works until
the first rotation and then fails permanently.

The workflows push to `gh-pages` using the built-in `GITHUB_TOKEN`, so no PAT
is needed for hosting in CI. A PAT with **Contents: write** is only needed for
local runs (`ASSETS_REPO_TOKEN` in `.env`).

**4. TikTok refresh token**

```bash
python scripts/tiktok_auth.py
```

**5. TikTok domain verification** — TikTok refuses `PULL_FROM_URL` from an
unverified domain. Add the URL property in the developer console and commit
its verification file to the `gh-pages` branch root; `assets.py` is set to
never prune it.

## Running locally

```bash
pip install -r requirements.txt

python run_meta.py --dry-run        # generate + host, post nothing
python run_meta.py --no-batch       # realtime image generation instead of Batch API
python run_tiktok.py --dry-run
```

## Notes

- **Image generation uses the Gemini Batch API** (~50% cheaper). Batch is
  asynchronous by contract; the run waits up to 30 minutes and falls back to a
  plain background rather than hanging. `--no-batch` trades cost for latency.
- **Dedupe** runs off `manifest.json`. Story IDs are kept for 14 days, longer
  than the 48h image window, so the pipeline doesn't start repeating stories
  once their images expire.
- **Partial failures are recorded, not fatal.** If Instagram succeeds and
  Facebook fails, the manifest reflects that and the run exits non-zero.
- **Scheduled workflows are disabled after 60 days of repo inactivity**, and
  GitHub's cron is best-effort — slots can fire late.
