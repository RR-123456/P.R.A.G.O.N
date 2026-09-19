"""
run_pragon_moss.py
════════════════════════════════════════════════════════════════════════════
Launch P.R.A.G.O.N with MOSS attached — without editing pragon_main.py.

    python run_pragon_moss.py          # PRAGON + Moss
    python pragon_main.py              # PRAGON exactly as before, untouched

The import order matters and is the whole trick: pragon_main is imported
(which only defines things), Moss wraps the handful of methods it augments,
and then PRAGON's own main() runs. JarvisLive registers its processors with
the UI inside __init__, which happens after the patch, so the UI ends up
holding the Moss-augmented callables with the originals still inside them.

Every flag from pragon_moss/bridge.py works here too, e.g.:

    PRAGON_MOSS_GUARDRAIL=enforce python run_pragon_moss.py
    PRAGON_MOSS_EMBED=local       python run_pragon_moss.py
    PRAGON_MOSS=0                 python run_pragon_moss.py   # Moss off
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    import pragon_main  # defines JarvisLive, _rag_query, main() — runs nothing

    try:
        from pragon_moss import install
        install(pragon_main)
    except Exception as exc:
        # Moss must never be the reason PRAGON doesn't start.
        print(f"[moss] integration unavailable ({type(exc).__name__}: {exc}) — "
              f"starting PRAGON without it.")

    pragon_main.main()


if __name__ == "__main__":
    main()
