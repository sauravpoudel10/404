# 404-style card pipeline

Finds a trending story, renders a "404 Media" style stat card, hosts it on
GitHub Pages, and posts it to Instagram, Facebook, and TikTok on a schedule.

```
run_meta.py     every 2h (12/day)  trending story -> card -> Instagram + Facebook
run_tiktok.py   every 6h (4/day)   reposts the newest IG-published card to TikTok
```

TikTok generates no image of its own — it reuses the most recent card that
actually reached Instagram.

## How it fits together

| Module | Job |
|---|---|
| `pipeline/trends.py` | One Claude call with the server-side `web_search` tool: finds a trending story and writes the card copy, schema-constrained so the JSON always parses |
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
