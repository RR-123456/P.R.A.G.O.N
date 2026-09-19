"""
pragon_moss
════════════════════════════════════════════════════════════════════════════
MOSS ↔ P.R.A.G.O.N integration layer.

Purely additive: `moss.py` is vendored at the project root exactly as
supplied, and everything in this package wraps PRAGON rather than editing it.
No existing PRAGON file is modified by installing or using this package.

    from pragon_moss import install, get_bridge

    install()                      # patch PRAGON's processors (idempotent)
    moss = get_bridge()            # blocking, fail-soft handle on the engine
    moss.remember("...", {"type": "safety"})
    moss.recall("what did we confirm?")

Turn the whole thing off at any time with  PRAGON_MOSS=0  — PRAGON then runs
exactly as it did before, with zero Moss code on any hot path.
"""

from .bridge import MossBridge, get_bridge
from .integration import install, uninstall

__all__ = ["MossBridge", "get_bridge", "install", "uninstall"]
__version__ = "1.0.0"
