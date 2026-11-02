"""Budget math (pure): which limits apply to a run and how close it is to each.

A run's budget is merged from, lowest precedence first: the global default budget
(Settings > Limits), the project's ``settings.budget``, the agent profile's
``config.budget``, and the run's own ``budget``. Keys:

=====================  ======  ===========================================================
key                    unit    measured from
=====================  ======  ===========================================================
max_total_tokens       tokens  real reported usage (input + output, or ``total_tokens``)
max_cost_usd           USD     real reported cost (``usd`` / ``total_usd`` / USD ``amount``)
max_changed_files      files   ``file_changes`` rows of the attempt
max_diff_lines         lines   insertions + deletions of those rows
max_artifact_bytes     bytes   ``artifacts`` rows of the run
warn_ratio             ratio   when to warn before a limit (default 0.8)
=====================  ======  ===========================================================

Never inventing precision: usage marked ``simulated`` (the Demo Agent) and cost that was
not reported produce ``not_applicable`` / ``unknown`` checks that can never trip a limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_WARN_RATIO = 0.8

BUDGET_KEYS: dict[str, tuple[str, str, str]] = {
    # key: (label, unit, description)
    "max_total_tokens": (
        "Tokens",
        "tokens",
        "Input plus output tokens, only when the adapter reports real usage",
    ),
    "max_cost_usd": ("Cost", "USD", "Cost in US dollars, only when the adapter reports it"),
    "max_changed_files": ("Changed files", "files", "Files added, modified, deleted, or renamed"),
    "max_diff_lines": ("Diff lines", "lines", "Inserted plus deleted lines in text changes"),
    "max_artifact_bytes": ("Artifact storage", "bytes", "Total size of the run's artifacts"),
}


@dataclass
class Measures:
    """What a run has used so far. ``None`` means "not reported"."""

    usage: dict[str, Any] | None = None
    cost: dict[str, Any] | None = None
    changed_files: int | None = None
    diff_lines: int | None = None
    artifact_bytes: int | None = None


@dataclass
class BudgetCheck:
    key: str
    label: str
    unit: str
    limit: float
    used: float | None
    ratio: float | None
    status: str  # ok | warning | exceeded | not_applicable | unknown
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "limit": self.limit,
            "used": self.used,
            "ratio": None if self.ratio is None else round(self.ratio, 4),
            "status": self.status,
            "note": self.note,
        }

    def describe(self) -> str:
        used = "unknown" if self.used is None else _fmt(self.used)
        return f"{self.label}: {used} of {_fmt(self.limit)} {self.unit}"


@dataclass
class EffectiveBudget:
    limits: dict[str, float] = field(default_factory=dict)
    warn_ratio: float = DEFAULT_WARN_RATIO
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.limits

    def as_dict(self) -> dict[str, Any]:
        return {**self.limits, "warn_ratio": self.warn_ratio}


def _fmt(value: float) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def _positive(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value > 0 else None


def merge_budgets(layers: list[tuple[str, dict[str, Any] | None]]) -> EffectiveBudget:
    """Merge ``(source name, budget dict)`` layers; later layers override earlier ones."""
    out = EffectiveBudget()
    for source, layer in layers:
        for key, value in (layer or {}).items():
            if key == "warn_ratio":
                v = _positive(value)
                if v is not None and v < 1:
                    out.warn_ratio = v
                    out.sources["warn_ratio"] = source
            elif key in BUDGET_KEYS:
                if value is None:
                    out.limits.pop(key, None)
                    out.sources.pop(key, None)
                    continue
                v = _positive(value)
                if v is not None:
                    out.limits[key] = v
                    out.sources[key] = source
    return out


def budget_from_snapshot(
    snapshot: dict[str, Any], run_budget: dict[str, Any] | None, default: dict[str, Any] | None
) -> EffectiveBudget:
    project_settings = dict((snapshot.get("project") or {}).get("settings") or {})
    profile_config = dict((snapshot.get("agent_profile") or {}).get("config") or {})
    return merge_budgets(
        [
            ("global default", default),
            ("project", project_settings.get("budget") or project_settings.get("budgets")),
            ("agent profile", profile_config.get("budget")),
            ("run", run_budget),
        ]
    )


def reported_tokens(usage: dict[str, Any] | None) -> tuple[float | None, str | None]:
    """Real token count from reported usage, or ``(None, reason)``."""
    if not usage:
        return None, "The adapter has not reported token usage"
    if usage.get("simulated"):
        return None, "Usage is simulated (Demo Agent); it never counts against real limits"
    total = usage.get("total_tokens")
    if isinstance(total, int | float) and not isinstance(total, bool):
        return float(total), None
    parts = [usage.get(k) for k in ("input_tokens", "output_tokens")]
    nums = [p for p in parts if isinstance(p, int | float) and not isinstance(p, bool)]
    if not nums:
        return None, "The adapter reported usage without token counts"
    return float(sum(nums)), None


def reported_cost_usd(cost: dict[str, Any] | None) -> tuple[float | None, str | None]:
    if not cost:
        return None, "The adapter has not reported cost"
    if cost.get("simulated"):
        return None, "Cost is simulated; it never counts against real limits"
    for key in ("usd", "total_usd", "cost_usd"):
        value = cost.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value), None
    amount = cost.get("amount")
    if (
        isinstance(amount, int | float)
        and not isinstance(amount, bool)
        and str(cost.get("currency", "")).upper() == "USD"
    ):
        return float(amount), None
    return None, "Cost was reported in a form that is not US dollars"


def evaluate(budget: EffectiveBudget, m: Measures) -> list[BudgetCheck]:
    checks: list[BudgetCheck] = []
    for key, limit in budget.limits.items():
        label, unit, _desc = BUDGET_KEYS[key]
        used: float | None
        note: str | None = None
        na = False
        if key == "max_total_tokens":
            used, note = reported_tokens(m.usage)
            na = bool(m.usage and m.usage.get("simulated"))
        elif key == "max_cost_usd":
            used, note = reported_cost_usd(m.cost)
            na = bool(m.cost and m.cost.get("simulated")) or bool(
                m.usage and m.usage.get("simulated") and not m.cost
            )
        elif key == "max_changed_files":
            used = None if m.changed_files is None else float(m.changed_files)
        elif key == "max_diff_lines":
            used = None if m.diff_lines is None else float(m.diff_lines)
        else:
            used = None if m.artifact_bytes is None else float(m.artifact_bytes)
        if used is None:
            checks.append(
                BudgetCheck(
                    key, label, unit, limit, None, None, "not_applicable" if na else "unknown", note
                )
            )
            continue
        ratio = used / limit
        status = "exceeded" if used > limit else "warning" if ratio >= budget.warn_ratio else "ok"
        checks.append(BudgetCheck(key, label, unit, limit, used, ratio, status, note))
    return checks
