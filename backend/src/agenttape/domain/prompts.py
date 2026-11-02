"""Prompt template variables, validation, and rendering (pure).

Template syntax (deliberately tiny and safe, no expressions or code execution):

* ``{{ name }}`` substitutes the typed variable ``name``.
* ``{{ secret:NAME }}`` references a secret by name. Stored/rendered text contains the
  redacted marker ``[secret:NAME]``; the real value is substituted only at the execution
  boundary by re-rendering with :data:`RenderMode.RESOLVE` and a resolver.
* A variable of type ``secret`` holds a secret *reference name*; it renders the same way.

Values supplied by users are never re-scanned for markers, so an input containing the
literal text ``[secret:X]`` cannot pull a secret into a prompt.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenttape.domain.enums import VariableType

_TOKEN = re.compile(r"\{\{\s*(secret:)?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def secret_marker(name: str) -> str:
    return f"[secret:{name}]"


def secret_reference_spans(text: str) -> list[tuple[int, int]]:
    """Spans of this module's own ``{{ secret:NAME }}`` references in ``text``: the exact
    token :func:`render` resolves, with ``NAME`` satisfying :data:`SECRET_NAME`. Used by
    redaction to leave only these references intact; any other ``{{ ... }}`` text is not
    a secret reference."""
    return [m.span() for m in _TOKEN.finditer(text) if m.group(1) and SECRET_NAME.match(m.group(2))]


class RenderMode(StrEnum):
    REDACT = "redact"
    RESOLVE = "resolve"


class VariableValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    minimum: float | None = None
    maximum: float | None = None


class VariableDefinition(BaseModel):
    """One typed variable of a prompt version."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: VariableType = VariableType.STRING
    label: str | None = None
    required: bool = True
    default: Any = None
    help: str | None = None
    options: list[str] | None = None
    validation: VariableValidation | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not VARIABLE_NAME.match(value):
            raise ValueError("variable names must be identifiers (letters, digits, _)")
        return value

    @model_validator(mode="after")
    def _enum_needs_options(self) -> VariableDefinition:
        if self.type == VariableType.ENUM and not self.options:
            raise ValueError(f"enum variable '{self.name}' needs options")
        if self.validation and self.validation.pattern:
            try:
                re.compile(self.validation.pattern)
            except re.error as exc:
                raise ValueError(f"invalid pattern for '{self.name}': {exc}") from exc
        return self


class VariableIssue(BaseModel):
    variable: str
    message: str


class RenderResult(BaseModel):
    text: str
    values: dict[str, Any] = Field(default_factory=dict)
    secret_refs: list[str] = Field(default_factory=list)
    undeclared: list[str] = Field(default_factory=list)


def template_tokens(body: str) -> tuple[list[str], list[str]]:
    """Return (variable names, secret names) referenced by ``body`` in first-use order."""
    variables: list[str] = []
    secrets: list[str] = []
    for match in _TOKEN.finditer(body):
        target = secrets if match.group(1) else variables
        if match.group(2) not in target:
            target.append(match.group(2))
    return variables, secrets


def _coerce(defn: VariableDefinition, raw: Any) -> tuple[Any, str | None]:
    t = defn.type
    if t in (VariableType.STRING, VariableType.MULTILINE, VariableType.PATH):
        if not isinstance(raw, str):
            return None, "must be text"
        if t == VariableType.STRING and "\n" in raw:
            return None, "must be a single line (use a multiline variable for longer text)"
        if t == VariableType.PATH and ("\x00" in raw or ".." in raw.replace("\\", "/").split("/")):
            return None, "must not contain '..' segments or NUL bytes"
        return raw, None
    if t == VariableType.NUMBER:
        if isinstance(raw, bool):
            return None, "must be a number"
        try:
            num = float(raw) if not isinstance(raw, int | float) else raw
        except (TypeError, ValueError):
            return None, "must be a number"
        if isinstance(num, float) and not math.isfinite(num):
            return None, "must be a finite number"
        if isinstance(num, float) and num.is_integer() and not isinstance(raw, float):
            num = int(num)
        return num, None
    if t == VariableType.BOOLEAN:
        if isinstance(raw, bool):
            return raw, None
        if isinstance(raw, str) and raw.lower() in {"true", "false", "yes", "no", "1", "0"}:
            return raw.lower() in {"true", "yes", "1"}, None
        return None, "must be true or false"
    if t == VariableType.ENUM:
        if raw not in (defn.options or []):
            return None, f"must be one of: {', '.join(defn.options or [])}"
        return raw, None
    # VariableType.SECRET (the enum is exhaustive above)
    if not isinstance(raw, str) or not SECRET_NAME.match(raw):
        return None, "must be a secret reference name (e.g. GITHUB_TOKEN), never a value"
    return raw, None


def validate_inputs(
    definitions: list[VariableDefinition], inputs: Mapping[str, Any]
) -> tuple[dict[str, Any], list[VariableIssue]]:
    """Validate and coerce ``inputs`` against ``definitions``; apply defaults."""
    values: dict[str, Any] = {}
    issues: list[VariableIssue] = []
    declared = {d.name for d in definitions}
    for name in inputs:
        if name not in declared:
            issues.append(VariableIssue(variable=name, message="is not a declared variable"))
    for defn in definitions:
        raw = inputs.get(defn.name, None)
        if raw is None or (
            isinstance(raw, str) and raw == "" and defn.type != VariableType.MULTILINE
        ):
            if defn.default is not None:
                raw = defn.default
            elif defn.required:
                issues.append(VariableIssue(variable=defn.name, message="is required"))
                continue
            else:
                values[defn.name] = None
                continue
        value, error = _coerce(defn, raw)
        if error:
            issues.append(VariableIssue(variable=defn.name, message=error))
            continue
        rules = defn.validation
        if rules is not None:
            if isinstance(value, str):
                if rules.min_length is not None and len(value) < rules.min_length:
                    issues.append(
                        VariableIssue(
                            variable=defn.name, message=f"must be at least {rules.min_length} chars"
                        )
                    )
                if rules.max_length is not None and len(value) > rules.max_length:
                    issues.append(
                        VariableIssue(
                            variable=defn.name, message=f"must be at most {rules.max_length} chars"
                        )
                    )
                if rules.pattern and not re.fullmatch(rules.pattern, value):
                    issues.append(
                        VariableIssue(
                            variable=defn.name, message=f"must match pattern {rules.pattern}"
                        )
                    )
            if isinstance(value, int | float) and not isinstance(value, bool):
                if rules.minimum is not None and value < rules.minimum:
                    issues.append(
                        VariableIssue(variable=defn.name, message=f"must be >= {rules.minimum}")
                    )
                if rules.maximum is not None and value > rules.maximum:
                    issues.append(
                        VariableIssue(variable=defn.name, message=f"must be <= {rules.maximum}")
                    )
        values[defn.name] = value
    return values, issues


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_template(
    body: str,
    definitions: list[VariableDefinition],
    values: Mapping[str, Any],
    *,
    mode: RenderMode = RenderMode.REDACT,
    resolve_secret: Callable[[str], str] | None = None,
) -> RenderResult:
    """Render ``body`` with already-validated ``values``.

    In ``REDACT`` mode (the only mode whose output may be stored), secret references render
    as ``[secret:NAME]``. ``RESOLVE`` mode requires ``resolve_secret`` and must only be used at
    the execution boundary; its output must never be persisted.
    """
    if mode == RenderMode.RESOLVE and resolve_secret is None:
        raise ValueError("RESOLVE mode requires a secret resolver")
    by_name = {d.name: d for d in definitions}
    secret_refs: list[str] = []
    undeclared: list[str] = []

    def secret_text(name: str) -> str:
        if name not in secret_refs:
            secret_refs.append(name)
        if mode == RenderMode.RESOLVE:
            assert resolve_secret is not None
            return resolve_secret(name)
        return secret_marker(name)

    def replace(match: re.Match[str]) -> str:
        is_secret, name = bool(match.group(1)), match.group(2)
        if is_secret:
            return secret_text(name)
        defn = by_name.get(name)
        if defn is None:
            undeclared.append(name)
            return match.group(0)
        value = values.get(name)
        if defn.type == VariableType.SECRET and value:
            return secret_text(str(value))
        return _format_value(value)

    text = _TOKEN.sub(replace, body)
    return RenderResult(
        text=text, values=dict(values), secret_refs=secret_refs, undeclared=undeclared
    )
