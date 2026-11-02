"""Not an executor: installs budget and concurrency-limit enforcement into the worker.

Modules in this package are imported whenever a worker loads its executors, which is the
startup point EXTENDING.md names for worker hooks. See :mod:`agenttape.limits`.
"""

from __future__ import annotations

from agenttape.limits import install

install()
