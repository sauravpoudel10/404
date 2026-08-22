"""Generate the bundled Reel music beds.

These are synthesised from scratch with ffmpeg, so they carry no third-party
rights at all -- the safe default for content that auto-publishes with no
human review. Replace them with Meta Sound Collection tracks any time; the
pipeline just picks whatever is in audio/.

    python tools/make_beds.py
"""

import subprocess
from pathlib import Path

import imageio_ffmpeg

OUT = Path(__file__).resolve().parent.parent / "audio"
SECONDS = 24          # long enough that the 7s clip never hears a loop seam

# root, third, fifth, plus a high shimmer -- one entry per bed. Varying the
# key and the shimmer is what keeps consecutive Reels from sounding identical.
BEDS = {
    "bed-warm":    (110.00, 130.81, 164.81, 329.63),   # A minor
    "bed-open":    (130.81, 164.81, 196.00, 392.00),   # C major
    "bed-deep":    ( 98.00, 116.54, 146.83, 293.66),   # G minor
    "bed-bright":  (146.83, 185.00, 220.00, 440.00),   # D major
    "bed-dusk":    (123.47, 146.83, 185.00, 369.99),   # B minor
    "bed-lift":    (164.81, 196.00, 246.94, 493.88),   # E minor
    "bed-still":   (103.83, 123.47, 155.56, 311.13),   # G# minor
    "bed-wide":    (116.54, 146.83, 174.61, 349.23),   # A# major
}


def build(name: str, freqs: tuple[float, ...]):
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    inputs, mixes = [], []
    for i, f in enumerate(freqs):
        inputs += ["-f", "lavfi", "-i", f"sine=frequency={f}:duration={SECONDS}"]
        # Roll the upper partials off so the chord sits back rather than
        # whistling, and give each voice a slow independent swell.
        gain = [0.55, 0.32, 0.24, 0.10][i]
        # ffmpeg's tremolo rejects f below 0.1, so keep the swell periods
        # under 10s -- slower than that and the filter errors out.
        period = 4.0 + i * 1.5
        mixes.append(
            f"[{i}:a]volume={gain},"
            f"tremolo=f={1 / period:.3f}:d=0.35,"
            f"lowpass=f={1800 + i * 400}[v{i}]"
        )
    graph = ";".join(mixes) + ";" + "".join(f"[v{i}]" for i in range(len(freqs)))
    graph += f"amix=inputs={len(freqs)}:normalize=0,aformat=channel_layouts=stereo"

    out = OUT / f"{name}.mp3"
    proc = subprocess.run(
        [ff, "-y", *inputs, "-filter_complex", graph,
         "-t", str(SECONDS), "-c:a", "libmp3lame", "-q:a", "4", str(out)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"{name} failed:\n{proc.stderr[-800:]}")
    return out


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, freqs in BEDS.items():
        p = build(name, freqs)
        print(f"  {p.name:<16} {p.stat().st_size // 1024:>4} KB")
