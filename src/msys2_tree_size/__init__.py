"""Public API surface for :mod:`msys2_tree_size`.

The traversal function is re-exported as :func:`walk_tree` rather than as
``walk``.  The submodule :mod:`msys2_tree_size.walk` owns that name, and
shadowing it would make ``import msys2_tree_size.walk`` and
``from msys2_tree_size import walk`` disagree about what ``walk`` means
(one would be the module, the other the function).  Keeping the names distinct
removes the ambiguity for callers.
"""

from __future__ import annotations

from .walk import Entry
from .walk import walk as walk_tree

__all__ = ["Entry", "walk_tree", "__version__"]

__version__ = "0.2.0"
