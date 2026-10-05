"""Test package.

Makes ``support`` importable regardless of which directory unittest is told to
discover from (``-s tests`` or ``-s tests -t .``), by putting this directory on
``sys.path`` before any test module is imported.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
