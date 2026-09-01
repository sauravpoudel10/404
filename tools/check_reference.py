"""Verify every ranked-list source still resolves to a usable table.

    python tools/check_reference.py [--show]

Wikipedia articles get renamed, split and restructured, and a source that
quietly stops resolving costs a list every time its subject comes round. Run
this after editing reference.SOURCES, and any time the lists look thin.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pipeline import reference  # noqa: E402

show = "--show" in sys.argv
ok, bad = [], []

for subject in sorted(reference.SOURCES):
    lines = reference.fetch(subject)
    if lines:
        ok.append(subject)
        print(f"  ok   {subject}  ({len(lines) - 1} rows)")
        if show:
            for line in lines[:4]:
                print(f"         {line[:110]}")
    else:
        bad.append(subject)
        print(f"  FAIL {subject}  -> {reference.SOURCES[subject]!r}")

print(f"\n{len(ok)} usable, {len(bad)} failed")
if bad:
    print("Remove or repoint these in pipeline/reference.py:")
    for subject in bad:
        print(f"  - {subject}: {reference.SOURCES[subject]!r}")
sys.exit(1 if bad else 0)
