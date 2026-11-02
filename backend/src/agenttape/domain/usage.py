"""Usage aggregation that never invents numbers.

Numeric fields are summed only when reported; ``simulated`` is sticky so demo numbers
can never be presented as real usage.
"""

from __future__ import annotations

from typing import Any


def merge_usage(
    total: dict[str, Any] | None, delta: dict[str, Any] | None
) -> dict[str, Any] | None:
    if not delta:
        return total
    out: dict[str, Any] = dict(total or {})
    for key, value in delta.items():
        if key == "simulated":
            out["simulated"] = bool(out.get("simulated")) or bool(value)
        elif isinstance(value, int | float) and not isinstance(value, bool):
            prev = out.get(key)
            out[key] = (prev if isinstance(prev, int | float) else 0) + value
        elif key not in out:
            out[key] = value
    return out
