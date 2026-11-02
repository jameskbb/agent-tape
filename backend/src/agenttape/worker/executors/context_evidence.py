"""Loads the context-evidence worker hooks (M2) when the worker loads executors.

:mod:`agenttape.context_evidence.hooks` registers the attempt monitor, after-attempt, and
run-terminal hooks on import. No executor is registered here.
"""

from __future__ import annotations

import agenttape.context_evidence.hooks  # noqa: F401  (registers hooks on import)
