"""Filesystem path policy: canonicalization, allowlisted roots, traversal-safe joins."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from agenttape.domain.errors import PolicyViolationError, ValidationError


def canonicalize(path: str | os.PathLike[str], *, must_exist: bool = False) -> Path:
    """Expand ``~``, make absolute, and resolve symlinks and ``..`` segments."""
    raw = os.fspath(path)
    if not raw or "\x00" in raw:
        raise ValidationError("Path is empty or contains NUL bytes", details={"path": raw})
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValidationError("Path must be absolute", details={"path": raw})
    try:
        resolved = candidate.resolve(strict=must_exist)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ValidationError("Path does not exist", details={"path": raw}) from exc
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"Path cannot be resolved: {exc}", details={"path": raw}) from exc
    return resolved


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def normalize_roots(roots: Iterable[str | os.PathLike[str]]) -> list[Path]:
    """Canonicalize allowlisted roots; the filesystem root itself is never a valid root."""
    out: list[Path] = []
    for r in roots:
        try:
            p = canonicalize(r)
        except ValidationError:
            continue
        if p == Path(p.anchor):
            continue
        if p not in out:
            out.append(p)
    return out


def ensure_allowed(
    path: str | os.PathLike[str],
    roots: Iterable[str | os.PathLike[str]],
    *,
    must_exist: bool = True,
) -> Path:
    """Canonicalize ``path`` and require it to sit inside one of ``roots``.

    Raises :class:`PolicyViolationError` when the path escapes every root (including via
    symlinks, which are resolved first).
    """
    resolved = canonicalize(path, must_exist=must_exist)
    allowed = normalize_roots(roots)
    if not allowed:
        raise PolicyViolationError(
            "No project roots are allowlisted. Set AGENTTAPE_ALLOWED_ROOTS (for example "
            "'~/src') or add a root in Settings.",
            details={"path": str(resolved)},
        )
    if not any(is_within(resolved, root) for root in allowed):
        raise PolicyViolationError(
            "Path is outside the allowlisted project roots",
            details={"path": str(resolved), "allowed_roots": [str(r) for r in allowed]},
        )
    return resolved


def safe_join(base: Path, relative: str) -> Path:
    """Join ``relative`` under ``base`` and refuse anything that escapes ``base``."""
    if not relative or "\x00" in relative:
        raise PolicyViolationError("Invalid relative path", details={"path": relative})
    rel = Path(relative)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        raise PolicyViolationError("Path traversal is not allowed", details={"path": relative})
    base_resolved = base.resolve()
    target = (base_resolved / rel).resolve()
    if not is_within(target, base_resolved):
        raise PolicyViolationError("Path escapes its base directory", details={"path": relative})
    return target
