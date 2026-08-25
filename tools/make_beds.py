"""Generate the bundled Reel music.

Synthesised from scratch with the standard library, so there are no
third-party rights involved at all -- the safe default for content that
auto-publishes with no human review. Replace with Meta Sound Collection
tracks any time; the pipeline just plays whatever is in audio/.

The earlier version stacked four sine waves into one sustained chord, which
was a drone, not music. This writes actual tracks: a four-chord progression,
kick and hats on the grid, a bass line following the root, and a plucked
arpeggio over the top.

    python tools/make_beds.py
"""

import math
import random
import struct
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg

OUT = Path(__file__).resolve().parent.parent / "audio"
SR = 44100
BARS = 8
BEATS_PER_BAR = 4

# Semitone offsets from the root for each chord of the progression, plus the
# scale degrees the arpeggio may use. Minor-key pop progressions: familiar,
# and they carry a card about markets or layoffs without sounding jolly.
PROGRESSIONS = {
    "drift":  [0, -3, -5, -7],
    "climb":  [0, 5, 3, 7],
    "pulse":  [0, -5, -3, -1],
    "steady": [0, 3, 5, 3],
}
TRACKS = {
    "beat-amber":  ("drift", 110.0, 92),
    "beat-slate":  ("pulse", 98.0, 84),
    "beat-ivory":  ("climb", 130.81, 100),
    "beat-cobalt": ("steady", 116.54, 88),
    "beat-rust":   ("drift", 123.47, 96),
    "beat-fern":   ("pulse", 103.83, 90),
}


def _env(i: int, n: int, attack: float = 0.01, release: float = 0.35) -> float:
    """Simple attack/decay envelope, 0..1."""
    t = i / n
    if t < attack:
        return t / attack
    return max(0.0, (1.0 - (t - attack) / (1.0 - attack)) ** (1.0 / release))


def _note(buf: list[float], start: int, dur: int, freq: float,
          gain: float, harmonics=(1.0, 0.5, 0.25)):
    """Additive-synth note with a soft envelope."""
    for i in range(dur):
        if start + i >= len(buf):
            break
        e = _env(i, dur)
        s = 0.0
        for h, amp in enumerate(harmonics, start=1):
            s += amp * math.sin(2 * math.pi * freq * h * (i / SR))
        buf[start + i] += s * e * gain


def _kick(buf: list[float], start: int, gain: float = 0.9):
    dur = int(0.18 * SR)
    for i in range(dur):
        if start + i >= len(buf):
            break
        t = i / SR
        freq = 120 * math.exp(-t * 28) + 45      # pitch drop = thump
        buf[start + i] += math.sin(2 * math.pi * freq * t) * math.exp(-t * 11) * gain


def _hat(buf: list[float], start: int, gain: float = 0.16):
    dur = int(0.045 * SR)
    for i in range(dur):
        if start + i >= len(buf):
            break
        buf[start + i] += random.uniform(-1, 1) * math.exp(-i / dur * 7) * gain


def build(name: str, prog_name: str, root: float, bpm: int) -> Path:
    random.seed(name)                      # same track every run
    prog = PROGRESSIONS[prog_name]
    beat = 60.0 / bpm
    bar = beat * BEATS_PER_BAR
    total = int(bar * BARS * SR)
    buf = [0.0] * total

    for b in range(BARS):
        chord_root = root * (2 ** (prog[b % len(prog)] / 12))
        bar_start = int(b * bar * SR)

        # pad: root + fifth + octave, held across the bar
        for mult, gain in ((1.0, 0.16), (1.5, 0.11), (2.0, 0.07)):
            _note(buf, bar_start, int(bar * SR), chord_root * mult, gain,
                  harmonics=(1.0, 0.3))

        # bass on beats 1 and 3
        for k in (0, 2):
            _note(buf, bar_start + int(k * beat * SR), int(beat * SR * 0.9),
                  chord_root / 2, 0.30, harmonics=(1.0, 0.45, 0.2))

        # drums
        for k in range(BEATS_PER_BAR):
            at = bar_start + int(k * beat * SR)
            if k in (0, 2):
                _kick(buf, at)
            _hat(buf, at + int(beat * SR / 2))

        # arpeggio: eighth notes over the chord, two octaves up
        steps = [1.0, 1.5, 2.0, 1.5, 2.5, 2.0, 1.5, 1.0]
        for k, mult in enumerate(steps):
            at = bar_start + int(k * (beat / 2) * SR)
            _note(buf, at, int(beat * SR * 0.45), chord_root * 2 * mult,
                  0.10, harmonics=(1.0, 0.25))

    peak = max(abs(v) for v in buf) or 1.0
    scale = 0.89 / peak

    wav_path = OUT / f"{name}.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, v * scale)) * 32767))
            for v in buf
        ))

    # Peak-normalising sparse music leaves the RMS low: the drum transients
    # hit 0dB while everything between them sits far below, so the track
    # measures loud and sounds quiet. Compress first, then loudness-normalise
    # over the whole 20s -- loudnorm is unreliable on a 7s excerpt but
    # accurate given a full track.
    mp3_path = OUT / f"{name}.mp3"
    proc = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", str(wav_path),
         "-af", ("acompressor=threshold=-20dB:ratio=4:attack=5:release=120,"
                 "loudnorm=I=-12:TP=-1.0:LRA=7"),
         "-c:a", "libmp3lame", "-q:a", "3", str(mp3_path)],
        capture_output=True, text=True,
    )
    wav_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise SystemExit(f"{name} encode failed:\n{proc.stderr[-600:]}")
    return mp3_path


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("bed-*.mp3"):       # retire the old drones
        old.unlink()
        print(f"  removed {old.name}")
    for name, (prog, root, bpm) in TRACKS.items():
        p = build(name, prog, root, bpm)
        print(f"  {p.name:<18} {bpm:>3} BPM  {p.stat().st_size // 1024:>4} KB")
