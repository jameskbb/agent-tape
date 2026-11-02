"""Redaction of secret values and common credential patterns.

Applied by every persistence path (event sink, raw logs, exports, error messages) before
data reaches disk or the database. Redaction is conservative: false positives are
acceptable; leaking a credential is not.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from agenttape.domain.prompts import secret_reference_spans

MIN_SECRET_LENGTH = 4

#: A PEM private key is redacted from a BEGIN line to the first END line after it
#: (:func:`_redact_private_keys`), found with two marker searches rather than one lazy
#: ``BEGIN[\s\S]*?END`` pattern: that pattern rescans the rest of the text from every BEGIN
#: with no END after it, which is quadratic (seconds for a few hundred KiB of BEGIN lines).
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_PRIVATE_KEY_END = re.compile(r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----")

#: (kind, pattern, group index of the sensitive part or 0 for the full match), applied
#: after private keys.
_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"), 0),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"), 0),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})"),
        0,
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), 0),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), 0),
    (
        "bearer_token",
        re.compile(r"(?i)\b(bearer|token)\s+([A-Za-z0-9._\-~+/]{16,}=*)"),
        2,
    ),
    (
        "url_credentials",
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+:)([^@\s/]+)(@)"),
        2,
    ),
    (
        "assignment",
        re.compile(
            # Not preceded by "[" so AgentTape's own "[secret:NAME]" markers are left alone.
            r"(?i)(?<![\[\w\-])([A-Za-z0-9_\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key)[A-Za-z0-9_\-]*)"
            r"(\s*[=:]\s*)(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;&\"']+)"
        ),
        3,
    ),
]

_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_\-]?key|access[_\-]?key|authorization|credential|private[_\-]?key)"
)


_BENIGN_VALUES = frozenset({"", "true", "false", "null", "none", "undefined", "***", "<redacted>"})


_STATUS_WORDS = frozenset(
    {"pass", "passed", "fail", "failed", "skipped", "warning", "error", "ok", "unknown", "n/a"}
)


def marker(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def _redact_private_keys(text: str, rep: RedactionReport) -> str:
    """Every ``-----BEGIN ... PRIVATE KEY-----`` through the first ``-----END ... PRIVATE
    KEY-----`` after it, replaced by one marker: exactly what the lazy pattern matched, in
    one pass. A BEGIN with no END after it ends the scan, since no later BEGIN can have one.
    """
    if "PRIVATE KEY-----" not in text:
        return text
    parts: list[str] = []
    pos = 0
    while True:
        begin = _PRIVATE_KEY_BEGIN.search(text, pos)
        if begin is None:
            break
        end = _PRIVATE_KEY_END.search(text, begin.end())
        if end is None:
            break
        parts.append(text[pos : begin.start()])
        parts.append(marker("private_key"))
        rep.add("private_key")
        pos = end.end()
    if not parts:
        return text
    parts.append(text[pos:])
    return "".join(parts)


def _inside_secret_reference(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    """Whether ``text[start:end]`` lies entirely inside AgentTape's own ``{{ secret:NAME }}``
    references (``spans`` from :func:`secret_reference_spans`). Adjacent references
    (``{{secret:A}}{{secret:B}}``) count as one covered run, since the value pattern's
    charset does not exclude ``}`` and a tight reference's value can swallow the next one.
    Any other ``{{ keyword: value }}`` text is not covered, so it redacts exactly as it
    would unwrapped."""
    covered_to = -1
    for s, e in spans:
        if covered_to < 0:
            if s <= start < e:
                covered_to = e
        elif s == covered_to:
            covered_to = e
        if covered_to >= end:
            return True
        if s > end:
            break
    return covered_to >= end


@dataclass
class RedactionReport:
    count: int = 0
    kinds: set[str] = field(default_factory=set)

    def add(self, kind: str, n: int = 1) -> None:
        self.count += n
        self.kinds.add(kind)

    def as_metadata(self) -> dict[str, Any]:
        return {"redacted": self.count > 0, "count": self.count, "kinds": sorted(self.kinds)}


class Redactor:
    """Redacts configured secret values plus built-in credential patterns."""

    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        self._values: set[str] = set()
        self._value_re: re.Pattern[str] | None = None
        self.add_secrets(secret_values)

    def add_secrets(self, values: Iterable[str]) -> None:
        changed = False
        for v in values:
            if v and len(v) >= MIN_SECRET_LENGTH and v not in self._values:
                self._values.add(v)
                changed = True
        if changed:
            ordered = sorted(self._values, key=len, reverse=True)
            self._value_re = re.compile("|".join(re.escape(v) for v in ordered))

    def copy(self) -> Redactor:
        """A new redactor with the same configured values (add per-attempt secrets to it)."""
        clone = Redactor()
        clone.add_secrets(self._values)
        return clone

    def redact_text(self, text: str, report: RedactionReport | None = None) -> str:
        rep = report if report is not None else RedactionReport()
        if not text:
            return text
        if self._value_re is not None:
            text, n = self._value_re.subn(marker("secret"), text)
            if n:
                rep.add("secret", n)
        text = _redact_private_keys(text, rep)
        for kind, pattern, group in _PATTERNS:
            text = self._apply(pattern, kind, group, text, rep)
        return text

    @staticmethod
    def _apply(
        pattern: re.Pattern[str], kind: str, group: int, text: str, rep: RedactionReport
    ) -> str:
        ref_spans = secret_reference_spans(text) if kind == "assignment" and "{{" in text else []

        def sub(m: re.Match[str]) -> str:
            if group == 0:
                rep.add(kind)
                return marker(kind)
            value = m.group(group)
            if value.startswith(("[REDACTED:", "[secret:", "{{")):
                return m.group(0)
            if kind == "assignment" and value.strip("\"'").lower() in _BENIGN_VALUES:
                return m.group(0)
            if kind == "assignment" and value.strip("\"'").replace(".", "").isdigit():
                return m.group(0)
            if (
                kind == "assignment"
                and ref_spans
                and _inside_secret_reference(ref_spans, m.start(0), m.end(group))
            ):
                # A prompt template's own {{ secret:NAME }} reference (NAME matching the
                # secret reference name rule), not a pasted credential assignment - never
                # mangle the name a later RESOLVE-mode render needs to find
                # (docs/SECURITY.md T4). Only that exact token is exempt: any other
                # "{{ keyword: value }}" text, or a "secret:" value that is not a reference
                # name, still redacts.
                return m.group(0)
            rep.add(kind)
            start, end = m.span(group)
            base = m.start(0)
            whole = m.group(0)
            return whole[: start - base] + marker(kind) + whole[end - base :]

        return pattern.sub(sub, text)

    def redact_value(self, value: Any, report: RedactionReport | None = None) -> Any:
        """Recursively redact strings in JSON-like data; values of sensitive keys are masked."""
        rep = report if report is not None else RedactionReport()
        if isinstance(value, str):
            return self.redact_text(value, rep)
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in value.items():
                key = str(k)
                if (
                    isinstance(v, str)
                    and v
                    and _SENSITIVE_KEY.search(key)
                    and not v.startswith("[REDACTED:")
                    and not v.startswith("[secret:")
                    and not key.endswith(("_ref", "_refs", "_name", "_names", "_prefix", "_tokens"))
                    # Human labels ("Token budget") and status words are not credential fields.
                    and not any(ch.isspace() for ch in key)
                    and v.strip().lower() not in _BENIGN_VALUES | _STATUS_WORDS
                ):
                    rep.add("sensitive_field")
                    out[key] = marker("sensitive_field")
                else:
                    out[key] = self.redact_value(v, rep)
            return out
        if isinstance(value, list | tuple):
            return [self.redact_value(v, rep) for v in value]
        return value


DEFAULT_REDACTOR = Redactor()


def redact(text: str) -> str:
    """Redact credential patterns only (no configured secret values)."""
    return DEFAULT_REDACTOR.redact_text(text)
