"""Module auto-discovery used by every registry-style extension point.

Features add a *file* to a package (a router, CLI command, adapter, evaluator,
executor, background service, seed); nothing central is edited. Modules are imported in
sorted name order so registration is deterministic.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType


def import_submodules(package_name: str) -> list[ModuleType]:
    """Import every direct submodule of ``package_name`` (skipping ``_private`` modules)."""
    package = importlib.import_module(package_name)
    modules: list[ModuleType] = []
    paths = getattr(package, "__path__", None)
    if paths is None:
        return modules
    for info in sorted(pkgutil.iter_modules(paths), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        modules.append(importlib.import_module(f"{package_name}.{info.name}"))
    return modules
