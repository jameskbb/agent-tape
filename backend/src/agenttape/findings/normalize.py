"""One documented normalization pass, and the pure quote-location logic it enables.

``normalize()`` applies exactly this pass: line-ending normalization (CRLF/CR -> LF), Unicode
NFKC, and trailing-whitespace removal (per line, and at the end of the text). It never touches
anything else: no case folding, no tab/space folding, no quote-style folding beyond what NFKC
itself does, no word reordering, no fuzzy or semantic matching. A model that changes negation,
word order, or quote characters has changed the text it quotes, and the mismatch is correct,
not a bug to work around.

Pure module, no I/O, mirroring ``agenttape.findings.citations``'s own convention.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

#: A ``cat -n``-shaped line: right-aligned line number, then a TAB or U+2192 separator, then
#: the content. Claude Code's ``Read`` tool and AgentTape's demo agent both use a TAB; some
#: Claude Code versions render the separator as U+2192 (RIGHTWARDS ARROW) instead.
_CAT_N_LINE = re.compile(r"^\s*(\d+)(?:\t|→)(.*)$")

#: The marker the redaction guard writes over a secret-shaped value (``security/redaction.py``).
REDACTION_MARKER = "[REDACTED:"


def normalize(text: str) -> str:
    """The one documented normalization pass.

    CRLF and lone CR become LF; the result is Unicode-NFKC-normalized; trailing whitespace is
    stripped from every line; trailing newlines at the end of the text are removed. Idempotent:
    ``normalize(normalize(x)) == normalize(x)`` for every ``x``.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFKC", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.rstrip("\n")


def to_cat_n(lines: Iterable[str], start: int = 1) -> str:
    """``lines`` in the ``cat -n`` layout Claude Code's ``Read`` tool returns: a six-wide
    right-aligned line number from ``start``, a TAB, then the line. :func:`strip_cat_n`
    reverses it."""
    return "\n".join(f"{n:6d}\t{line}" for n, line in enumerate(lines, start=start))


def strip_cat_n(output: str) -> list[tuple[int, str]]:
    """``(absolute file line number, content)`` pairs, in the order they appear.

    A line that does not match the numbered-prefix shape (a truncation note, a system
    reminder, a warning) is skipped entirely, never given a fabricated line number. The
    numbers are absolute file line numbers even for a partial (``offset``/``limit``) read;
    they are never renumbered from 1 or treated as relative to the read's own window.
    """
    pairs: list[tuple[int, str]] = []
    for line in output.split("\n"):
        # A CR the source file's own CRLF line endings left on this line (cat -n preserves
        # a CRLF file's own terminators) is kept as content here; normalize() is what turns
        # it into a plain LF later, exactly like any other CR in the file's content.
        match = _CAT_N_LINE.match(line)
        if match is None:
            continue
        pairs.append((int(match.group(1)), match.group(2)))
    return pairs


@dataclass(frozen=True)
class QuoteMatch:
    line_start: int
    line_end: int


def locate_quote(numbered_lines: list[tuple[int, str]], quote: str) -> QuoteMatch | None:
    """Where ``quote`` actually matched in ``numbered_lines``, or ``None`` if it did not.

    ``numbered_lines`` are ``(absolute file line number, raw line text)`` pairs, typically
    from :func:`strip_cat_n`. Both the quote and every line are normalized (never fuzzy,
    never semantic); the normalized lines are joined with LF and searched for the normalized
    quote as an exact substring. The match is mapped back to the absolute line numbers it
    spans - never the agent's claimed line numbers, and never a renumbering from 1.
    """
    norm_quote = normalize(quote)
    if not norm_quote.strip():
        return None
    if not numbered_lines:
        return None
    norm_lines = [normalize(text) for _, text in numbered_lines]
    joined = "\n".join(norm_lines)
    idx = joined.find(norm_quote)
    if idx == -1:
        return None
    end_idx = idx + len(norm_quote) - 1
    start_line = end_line = numbered_lines[0][0]
    consumed = 0
    for (line_no, _text), norm_len in zip(
        numbered_lines, (len(t) + 1 for t in norm_lines), strict=True
    ):
        line_end_idx = consumed + norm_len - 1
        if consumed <= idx <= line_end_idx:
            start_line = line_no
        if consumed <= end_idx <= line_end_idx:
            end_line = line_no
            break
        consumed += norm_len
    return QuoteMatch(start_line, end_line)


def has_redaction(text: str) -> bool:
    return REDACTION_MARKER in text
