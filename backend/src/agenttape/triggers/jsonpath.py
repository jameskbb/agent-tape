"""A deliberately tiny, safe JSON path subset for mapping webhook payloads to run inputs.

Grammar::

    path     := "$" segment*
    segment  := "." name | "[" index "]" | "[" quoted "]"
    name     := [A-Za-z_][A-Za-z0-9_-]*
    index    := [0-9]+
    quoted   := '"' chars '"' | "'" chars "'"

Examples: ``$.client_payload.branch``, ``$.commits[0].id``, ``$["weird key"].value``.

There are no wildcards, filters, slices, recursive descent, or expressions, so a path
can only ever select one value and evaluation is O(len(path)). Paths are validated when a
trigger is saved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAX_PATH_LENGTH = 200
MAX_SEGMENTS = 32
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_INDEX = re.compile(r"\d{1,6}")


class JsonPathError(ValueError):
    pass


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()


@dataclass(frozen=True)
class JsonPath:
    raw: str
    segments: tuple[str | int, ...]

    def get(self, document: Any) -> Any:
        """The selected value, or :data:`MISSING` when any segment is absent."""
        current = document
        for seg in self.segments:
            if isinstance(seg, int):
                if not isinstance(current, list) or seg >= len(current):
                    return MISSING
                current = current[seg]
            else:
                if not isinstance(current, dict) or seg not in current:
                    return MISSING
                current = current[seg]
        return current


def parse_path(path: str) -> JsonPath:
    if not isinstance(path, str) or not path:
        raise JsonPathError("A path is required, e.g. $.client_payload.branch")
    if len(path) > MAX_PATH_LENGTH:
        raise JsonPathError(f"Paths are limited to {MAX_PATH_LENGTH} characters")
    if not path.startswith("$"):
        raise JsonPathError("Paths start with $, e.g. $.client_payload.branch")
    segments: list[str | int] = []
    i = 1
    n = len(path)
    while i < n:
        ch = path[i]
        if ch == ".":
            m = _NAME.match(path, i + 1)
            if m is None:
                raise JsonPathError(f"Expected a field name after '.' at position {i + 1}")
            segments.append(m.group(0))
            i = m.end()
        elif ch == "[":
            if i + 1 < n and path[i + 1] in "'\"":
                quote = path[i + 1]
                end = path.find(quote, i + 2)
                if end == -1 or end + 1 >= n or path[end + 1] != "]":
                    raise JsonPathError(f"Unterminated quoted key at position {i}")
                key = path[i + 2 : end]
                if not key:
                    raise JsonPathError("Quoted keys cannot be empty")
                segments.append(key)
                i = end + 2
            else:
                m = _INDEX.match(path, i + 1)
                if m is None or m.end() >= n or path[m.end()] != "]":
                    raise JsonPathError(
                        f"Expected an array index or quoted key inside [] at position {i}"
                    )
                segments.append(int(m.group(0)))
                i = m.end() + 1
        else:
            raise JsonPathError(
                f"Unexpected '{ch}' at position {i}; use .name, [0], or [\"key\"] segments"
            )
        if len(segments) > MAX_SEGMENTS:
            raise JsonPathError(f"Paths are limited to {MAX_SEGMENTS} segments")
    return JsonPath(raw=path, segments=tuple(segments))


def extract(document: Any, path: str) -> Any:
    return parse_path(path).get(document)
