"""Turn a square card into a vertical Reel video.

Instagram Reels are video-only: a JPEG cannot be posted as a Reel no matter
what you set media_type to. To reach the Reels tab a clip must be H.264 MP4,
between 5 and 90 seconds, and 9:16 — anything else silently publishes as an
ordinary video post instead.

The card stays 1080x1080 and untouched. It's centered on a blurred, zoomed
copy of itself to fill 1080x1920, then held as a still for the duration.
ffmpeg comes from the imageio-ffmpeg wheel rather than a system install, so
this behaves identically on Windows and on the Actions runner.
"""

import io
import random
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageEnhance, ImageFilter

from . import config

REEL_W, REEL_H = 1080, 1920
DURATION = 7          # inside the 5-90s window Reels requires
FPS = 30
BLUR_RADIUS = 45
BACKDROP_DIM = 0.55   # darken the backdrop so the card stays dominant

# Drop royalty-free tracks in audio/ and a random one is used per Reel.
# With the folder empty the Reel gets a silent track instead -- Instagram has
# historically rejected videos with no audio stream at all, so there is
# always an audio stream, it just may be silence.
#
# Use only music you are licensed for. Instagram's own catalogue is NOT
# reachable through the publishing API, and a commercial track baked into
# the file gets the Reel auto-muted or pulled by Content ID. Meta Sound
# Collection is free and licensed for business accounts.
AUDIO_DIR = config.ROOT / "audio"
AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac")
# The bundled tracks are already compressed and loudness-normalised at
# generation (tools/make_beds.py), so a fixed gain here is predictable.
# loudnorm was used here originally and undershot badly -- asked -9 LUFS,
# delivered -17.6 -- because single-pass loudnorm cannot measure a 7s
# excerpt properly. The limiter catches peaks from louder third-party
# tracks dropped into audio/.
MUSIC_GAIN = 3.5          # past this the limiter just squashes: 7.0 buys only 0.7dB more
MUSIC_CEILING = 0.97      # limiter ceiling, just under clipping
FADE = 0.6


def build_frame(card_jpeg: bytes) -> Image.Image:
    """Compose the 9:16 frame: blurred backdrop + the card centered."""
    card = Image.open(io.BytesIO(card_jpeg)).convert("RGB")

    # Scale the card up until it covers the taller frame, crop to fill.
    scale = max(REEL_W / card.width, REEL_H / card.height)
    backdrop = card.resize(
        (round(card.width * scale), round(card.height * scale)), Image.LANCZOS
    )
    left = (backdrop.width - REEL_W) // 2
    top = (backdrop.height - REEL_H) // 2
    backdrop = backdrop.crop((left, top, left + REEL_W, top + REEL_H))
    backdrop = backdrop.filter(ImageFilter.GaussianBlur(BLUR_RADIUS))
    backdrop = ImageEnhance.Brightness(backdrop).enhance(BACKDROP_DIM)

    backdrop.paste(card, (0, (REEL_H - card.height) // 2))
    return backdrop


def pick_track() -> Path | None:
    """A random licensed track, or None to fall back to silence."""
    if not AUDIO_DIR.is_dir():
        return None
    tracks = sorted(p for p in AUDIO_DIR.iterdir()
                    if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    return random.choice(tracks) if tracks else None


def render_reel(card_jpeg: bytes) -> bytes:
    """Return an H.264 MP4 of the card, ready for Instagram Reels."""
    frame = build_frame(card_jpeg)
    track = pick_track()

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        still, out = tmp / "frame.png", tmp / "reel.mp4"
        frame.save(still)

        cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y",
               "-loop", "1", "-framerate", str(FPS), "-i", str(still)]

        if track is not None:
            # -stream_loop -1 so a track shorter than the clip repeats rather
            # than leaving silence at the end.
            cmd += ["-stream_loop", "-1", "-i", str(track),
                    "-af", (f"volume={MUSIC_GAIN},"
                            f"alimiter=limit={MUSIC_CEILING},"
                            f"afade=t=in:st=0:d={FADE},"
                            f"afade=t=out:st={DURATION - FADE}:d={FADE}"),
                    # loudnorm resamples to 96kHz and the bundled beds are
                    # mono; pin both to what the platforms expect.
                    "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
        else:
            cmd += ["-f", "lavfi",
                    "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-c:a", "aac", "-b:a", "64k", "-ar", "48000", "-ac", "2"]

        cmd += ["-t", str(DURATION),
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-pix_fmt", "yuv420p",          # required for broad playback
                "-movflags", "+faststart",      # metadata first, so IG can stream it
                "-shortest", str(out)]

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed:\n{proc.stderr[-1500:]}")
        return out.read_bytes()
