"""Loads the Git isolation worker hooks (no executor of its own).

The worker imports every module in this package at startup; importing
:mod:`agenttape.git.hooks` registers the Git workdir preparer and the after-attempt
change capture (see ``docs/EXTENDING.md``, worker hooks).
"""

from __future__ import annotations

import agenttape.git.hooks  # noqa: F401  (registers hooks on import)
