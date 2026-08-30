"""Turn card content into a finished JPEG.

Wraps the existing automate.py generator. The only addition is JPEG output:
Instagram's content-publishing API accepts JPEG only and rejects PNG, and
TikTok is happy with either, so JPEG is what both get.
"""

import io
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import automate  # noqa: E402


def render_jpeg(content: dict, use_batch: bool = True, quality: int = 90,
                style: str = automate.DEFAULT_STYLE) -> bytes:
    """Generate the background, compose the card, return JPEG bytes.

    `style` picks between the two card designs (see automate.STYLES). It also
    decides the aspect ratio asked of the image model: the classic card slices
    a wide band out of the top of the frame, the feature card uses all of it.
    """
    automate.ensure_fonts()

    images = automate.generate_backgrounds(
        [content["image_prompt"]], use_batch=use_batch,
        aspect=automate.aspect_for(style),
    )
    svg = automate.build_svg(content, images[0], style)

    png_path = Path(automate.SCRIPT_DIR) / "_card_tmp.png"
    if not automate.render_png(svg, png_path, style):
        raise RuntimeError("SVG rasterization failed; cannot publish without an image.")

    try:
        with Image.open(png_path) as im:
            # The card is fully opaque, but resvg emits RGBA — JPEG has no
            # alpha channel, so flatten before encoding rather than letting
            # Pillow raise on the mode mismatch.
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()
    finally:
        png_path.unlink(missing_ok=True)
