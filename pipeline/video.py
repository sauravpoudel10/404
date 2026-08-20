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
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageEnhance, ImageFilter

REEL_W, REEL_H = 1080, 1920
DURATION = 7          # inside the 5-90s window Reels requires
FPS = 30
BLUR_RADIUS = 45
BACKDROP_DIM = 0.55   # darken the backdrop so the card stays dominant


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


def render_reel(card_jpeg: bytes) -> bytes:
    """Return an H.264 MP4 of the card, ready for Instagram Reels."""
    frame = build_frame(card_jpeg)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        still, out = tmp / "frame.png", tmp / "reel.mp4"
        frame.save(still)

        proc = subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(), "-y",
                "-loop", "1", "-framerate", str(FPS), "-i", str(still),
                # Instagram has historically rejected videos with no audio
                # stream, so give it a silent one rather than find out.
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t", str(DURATION),
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-pix_fmt", "yuv420p",          # required for broad playback
                "-movflags", "+faststart",      # metadata first, so IG can stream it
                "-c:a", "aac", "-b:a", "64k",
                "-shortest",
                str(out),
            ],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed:\n{proc.stderr[-1500:]}")
        return out.read_bytes()
