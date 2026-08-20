"""Publish card images to GitHub Pages, and expire them after 48 hours.

Instagram and TikTok both fetch images over HTTPS rather than accepting
uploaded bytes, so every card has to be publicly hosted before it can be
posted anywhere. This module serves them from a `gh-pages` branch of the
same repo.

The branch is rebuilt as a SINGLE orphan commit on every run. That is what
makes the 48-hour expiry real: a plain `git rm` would drop the file from the
tree while leaving the blob in history forever, so the repo would grow by
roughly 300MB a month and never shrink. Force-pushing a fresh root commit
means deleted images are genuinely unreferenced.

The same branch doubles as the pipeline's state store — manifest.json is
what the TikTok run reads to find the latest Meta-published card, and what
the dedupe check reads to avoid covering a story twice.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

MANIFEST = "manifest.json"
CARDS_DIR = "cards"
HISTORY_DAYS = 14

# The gh-pages branch REPLACES whatever Pages served before, so the site's
# existing static files have to be carried onto it on every rebuild. Losing
# them is not cosmetic: tiktok*.txt is the domain-ownership proof without
# which TikTok refuses every PULL_FROM_URL post, and the privacy/terms pages
# are required for TikTok's app audit.
#
# These are copied from the checked-out main branch, which Actions already
# has on disk -- so editing them on main is all it takes to update the site.
STATIC_GLOBS = ("*.html", "tiktok*.txt", "CNAME")


def _rmtree(path: Path):
    """Delete a directory that contains a .git dir.

    Git marks objects read-only, which makes plain shutil.rmtree raise
    PermissionError on Windows (POSIX only checks the parent directory, so
    this only bites locally, never on the Actions runner).
    """
    def _force(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=_force)


def _redact(text: str) -> str:
    """Strip the push token out of anything that might be logged.

    The token is in the remote URL, so a raw CalledProcessError prints it
    verbatim -- straight into the Actions build log on any git failure.
    """
    token = os.environ.get("ASSETS_REPO_TOKEN", "").strip()
    return text.replace(token, "***") if token else text


def _run(args: list[str], cwd: Path):
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        # Surface git's own stderr: check=True alone reports only an exit
        # code, which turns a permissions problem into an unexplained 128.
        raise RuntimeError(
            f"`{_redact(' '.join(args))}` failed ({proc.returncode}):\n"
            f"{_redact(proc.stderr).strip()}"
        )


def _remote() -> str:
    return f"https://x-access-token:{config.GITHUB_TOKEN()}@github.com/{config.ASSETS_REPO}.git"


def _clone(work: Path) -> Path:
    """Shallow-clone the assets branch, or start an empty tree if it's new."""
    repo = work / "site"
    try:
        _run(["git", "clone", "--depth", "1", "--branch", config.ASSETS_BRANCH,
              _remote(), str(repo)], cwd=work)
        _rmtree(repo / ".git")                # discard history; we re-root below
    except RuntimeError:
        # Branch doesn't exist yet -- first run starts from an empty tree.
        repo.mkdir(parents=True, exist_ok=True)
    return repo


def _load_manifest(repo: Path) -> dict:
    path = repo / MANIFEST
    if not path.exists():
        return {"cards": [], "history": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"cards": [], "history": []}
    data.setdefault("cards", [])
    data.setdefault("history", [])
    return data


def _prune(repo: Path, manifest: dict) -> dict:
    """Drop cards past the retention window, and their files with them."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=config.RETENTION_HOURS)
    keep = []
    for card in manifest["cards"]:
        created = datetime.fromisoformat(card["created_at"])
        if created >= cutoff:
            keep.append(card)
        else:
            # Both artifacts, or expired Reels linger on the branch forever.
            (repo / card["file"]).unlink(missing_ok=True)
            if card.get("video_file"):
                (repo / card["video_file"]).unlink(missing_ok=True)
    manifest["cards"] = keep

    # story_ids outlive their images: the dedupe window should be longer than
    # the hosting window, or the pipeline starts repeating stories after 48h.
    hist_cutoff = now - timedelta(days=HISTORY_DAYS)
    manifest["history"] = [
        h for h in manifest["history"]
        if datetime.fromisoformat(h["seen_at"]) >= hist_cutoff
    ]
    return manifest


def _copy_static(repo: Path):
    """Carry the site's existing static files onto the assets branch."""
    for pattern in STATIC_GLOBS:
        for src in config.ROOT.glob(pattern):
            if src.is_file():
                shutil.copy2(src, repo / src.name)
    # Jekyll would otherwise ignore paths it considers special.
    (repo / ".nojekyll").write_text("", encoding="utf-8")


def _push(repo: Path, message: str):
    _run(["git", "init", "-b", config.ASSETS_BRANCH], cwd=repo)
    _run(["git", "config", "user.email", "actions@github.com"], cwd=repo)
    _run(["git", "config", "user.name", "404 pipeline"], cwd=repo)
    _run(["git", "add", "-A"], cwd=repo)
    _run(["git", "commit", "-m", message], cwd=repo)
    _run(["git", "push", "--force", _remote(),
          f"{config.ASSETS_BRANCH}:{config.ASSETS_BRANCH}"], cwd=repo)


def read_manifest() -> dict:
    """Fetch the current manifest without publishing anything."""
    with tempfile.TemporaryDirectory() as tmp:
        return _load_manifest(_clone(Path(tmp)))


def publish_card(jpeg: bytes, content: dict, mp4: bytes | None = None) -> dict:
    """Upload one card (and optionally its Reel), expire stale ones."""
    stamp = datetime.now(timezone.utc)
    slug = f"{stamp:%Y%m%d-%H%M%S}-{content['story_id'][:40]}"
    rel = f"{CARDS_DIR}/{slug}.jpg"

    with tempfile.TemporaryDirectory() as tmp:
        repo = _clone(Path(tmp))
        manifest = _prune(repo, _load_manifest(repo))

        (repo / CARDS_DIR).mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(jpeg)

        card = {
            "id": slug,
            "story_id": content["story_id"],
            "file": rel,
            "url": f"{config.PUBLIC_BASE_URL}/{rel}",
            "created_at": stamp.isoformat(),
            "caption": content.get("caption", ""),
            "posted": {"instagram": False, "facebook": False,
                       "x": False, "tiktok": False},
        }

        if mp4 is not None:
            video_rel = f"{CARDS_DIR}/{slug}.mp4"
            (repo / video_rel).write_bytes(mp4)
            card["video_file"] = video_rel
            card["video_url"] = f"{config.PUBLIC_BASE_URL}/{video_rel}"
        manifest["cards"].append(card)
        manifest["history"].append(
            {"story_id": content["story_id"], "seen_at": stamp.isoformat()}
        )
        manifest["updated_at"] = stamp.isoformat()

        _copy_static(repo)
        (repo / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _push(repo, f"card {slug}")

    return card


def update_card(card_id: str, posted: dict):
    """Record which platforms a card actually reached."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _clone(Path(tmp))
        manifest = _load_manifest(repo)
        for card in manifest["cards"]:
            if card["id"] == card_id:
                card["posted"].update(posted)
        manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
        _copy_static(repo)
        (repo / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _push(repo, f"mark {card_id} {','.join(k for k, v in posted.items() if v)}")


def recent_story_ids(manifest: dict) -> list[str]:
    return [h["story_id"] for h in manifest.get("history", [])]


def latest_published_card(manifest: dict) -> dict | None:
    """The newest card that actually made it to Instagram.

    TikTok reuses this image rather than generating its own, so it must only
    ever pick from cards that were really published — a card whose Meta post
    failed shouldn't leak onto TikTok on its own.
    """
    published = [c for c in manifest.get("cards", []) if c["posted"].get("instagram")]
    if not published:
        return None
    return max(published, key=lambda c: c["created_at"])


def wait_until_live(url: str, timeout: int = 300) -> bool:
    """Block until the published image is actually served over HTTPS.

    Pushing to gh-pages does not make a file reachable -- GitHub Pages still
    has to build and deploy, which takes roughly 30-60s. Instagram and TikTok
    both fetch the image themselves, so posting the instant the push returns
    races the deploy and fails with an unhelpful "media could not be fetched".
    """
    import requests

    started = time.time()
    delay = 3
    while time.time() - started < timeout:
        try:
            if requests.head(url, timeout=15, allow_redirects=True).status_code == 200:
                print(f"  live after {int(time.time() - started)}s")
                return True
        except requests.RequestException:
            pass
        time.sleep(delay)
        delay = min(delay * 1.5, 15)
    return False


def read_json(name: str, default: dict | None = None) -> dict:
    """Read a JSON file from the assets branch."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _clone(Path(tmp)) / name
        if not path.exists():
            return default if default is not None else {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default if default is not None else {}


def write_json(name: str, data: dict, message: str):
    """Write a JSON file onto the assets branch, preserving everything else.

    Deliberately does NOT prune cards -- expiry belongs to publish_card, and
    running it here would delete images mid-cycle behind Instagram's back.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = _clone(Path(tmp))
        (repo / name).write_text(json.dumps(data, indent=2), encoding="utf-8")
        _copy_static(repo)
        _push(repo, message)
