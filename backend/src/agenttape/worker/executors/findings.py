"""Loads the findings verification worker hook when the worker loads executors.

Importing the sibling ``findings`` module below registers the automatic run-terminal
verification hook (after the context-evidence hook it imports first). No executor is
registered here.
"""

from __future__ import annotations

import agenttape.findings.hooks  # noqa: F401  (registers hooks on import)
