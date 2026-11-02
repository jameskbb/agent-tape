"""Protected-path matching (project setting ``protected_paths``).

Patterns follow the familiar ``.gitignore`` shape, kept deliberately small and
predictable:

* ``*`` matches within one path segment, ``?`` one character, ``[...]`` a class;
* ``**`` matches any number of segments (including none);
* a pattern without ``/`` matches the file name at any depth (``*.lock``, ``.env``);
* a pattern ending in ``/`` or a plain directory name followed by ``/**`` matches
  everything below that directory;
* a leading ``/`` anchors the pattern at the repository root.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    pat = pattern.strip()
    anchored = pat.startswith("/")
    pat = pat.lstrip("/")
    if pat.endswith("/"):
        pat += "**"
    has_slash = "/" in pat
    out: list[str] = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i : i + 2] == "**":
                i += 2
                if i < len(pat) and pat[i] == "/":
                    # "**/" matches zero or more directories.
                    out.append("(?:[^/]+/)*")
                    i += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = pat.find("]", i + 1)
            if end == -1:
                out.append(re.escape(c))
            else:
                body = pat[i + 1 : end].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = end
        else:
            out.append(re.escape(c))
        i += 1
    body = "".join(out)
    if has_slash or anchored:
        return re.compile(f"^{body}$")
    # No slash: match the last segment at any depth.
    return re.compile(f"^(?:.*/)?{body}$")


def matches(path: str, pattern: str) -> bool:
    if not pattern.strip():
        return False
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return _compile(pattern).match(normalized) is not None


def matching_patterns(path: str, patterns: Iterable[str]) -> list[str]:
    """The patterns (in order) that ``path`` matches."""
    return [p for p in patterns if isinstance(p, str) and matches(path, p)]


def is_protected(path: str, patterns: Iterable[str], old_path: str | None = None) -> bool:
    """True when ``path`` (or the source of a rename) matches any protected pattern."""
    pats = list(patterns)
    return bool(matching_patterns(path, pats)) or bool(
        old_path and matching_patterns(old_path, pats)
    )
