"""Shared test helpers.

The only non-obvious thing here is :class:`TempDirTestCase`, which hands out
scratch directories **at fixed, pre-created paths** inside the checkout rather
than using ``mkdtemp``.

Two reasons:

1. Some environments (CI containers, sandboxed runs) cannot create or remove
   directories under the system temp path, and a failure to *clean up* would
   then be reported as a failure of the code under test.
2. Write grants are often provisioned per fixed path.  ``mkdtemp`` invents a
   fresh name every run, which such a grant does not cover, so a dynamically
   named directory can be readable but not writable.  A stable path avoids
   that entirely.

Directories are emptied between tests, so each test still starts clean and the
git workspace is left tidy (``.tmp/`` is git-ignored).
"""

from __future__ import annotations

import itertools
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
SCRATCH = REPO_ROOT / ".tmp"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_counter = itertools.count()


class TempDirTestCase(unittest.TestCase):
    """A ``TestCase`` whose scratch directories live inside the checkout."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        SCRATCH.mkdir(parents=True, exist_ok=True)
        cls._original_tempdir = tempfile.tempdir
        tempfile.tempdir = str(SCRATCH)

    @classmethod
    def tearDownClass(cls):
        tempfile.tempdir = cls._original_tempdir
        super().tearDownClass()

    def make_temp_dir(self) -> Path:
        """Return an empty scratch directory, removed at test teardown.

        The name is derived from the test's own name so it is stable across
        runs and therefore within reach of a per-path write grant.
        """
        index = next(_counter)
        name = f"{self.__class__.__name__}-{self._testMethodName}-{index}"
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
        path = SCRATCH / safe[:120]
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, path, True)
        return path
