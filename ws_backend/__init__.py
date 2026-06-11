"""Compatibility package for older imports.

New code should import from ``backend``.  The package path includes the moved
legacy implementation so imports such as ``ws_backend.stt`` still resolve.
"""

from pathlib import Path

_LEGACY_PATH = Path(__file__).resolve().parents[1] / "backend" / "legacy"
if str(_LEGACY_PATH) not in __path__:
    __path__.append(str(_LEGACY_PATH))
