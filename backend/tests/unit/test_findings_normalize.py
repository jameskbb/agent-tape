"""The normalization fixture suite.

One of the two files (with ``tests/unit/test_findings_verify.py``) that
must pass for any verdict to be trusted on a screen; it runs in ``make check``. Pure
unit tests: no database, no ``Session``, exactly ``normalize.py``'s own "pure, no I/O"
convention.
"""

from __future__ import annotations

from agenttape.findings.normalize import (
    QuoteMatch,
    locate_quote,
    normalize,
    strip_cat_n,
    to_cat_n,
)


def numbered_output(lines: list[str], start: int = 1) -> str:
    """Render ``lines`` in the ``Read`` tool's ``cat -n`` layout: a six-wide right-aligned
    line number, a TAB, then the line - exactly what ``strip_cat_n`` parses."""
    return "\n".join(f"{i:6d}\t{line}" for i, line in enumerate(lines, start=start))


def test_crlf_content_matches_an_lf_quote() -> None:
    assert normalize("a\r\nb\rc") == "a\nb\nc"
    output = numbered_output(["def f():\r", "    return 1\r"])
    lines = strip_cat_n(output)
    assert lines == [(1, "def f():\r"), (2, "    return 1\r")]
    assert locate_quote(lines, "def f():\n    return 1") == QuoteMatch(1, 2)


def test_tabs_are_never_folded_into_spaces() -> None:
    output = numbered_output(["\tif True:", "\t\treturn 1"])
    lines = strip_cat_n(output)
    assert locate_quote(lines, "\tif True:\n\t\treturn 1") == QuoteMatch(1, 2)
    # A model-introduced tab/space substitution is a genuine mismatch: verification only
    # normalizes line endings, NFKC, and trailing whitespace - never meaning-bearing
    # leading/internal whitespace.
    assert locate_quote(lines, "    if True:\n        return 1") is None


def test_nfkc_folds_compatibility_forms() -> None:
    # Full-width A/B/C, a NO-BREAK SPACE, and the "fi" ligature + "le" - all NFKC-canonicalize
    # to their plain ASCII forms ("ABC file").
    content = "ＡＢＣ ﬁle"  # noqa: RUF001 -- intentionally ambiguous: this is the fixture
    output = numbered_output([content])
    lines = strip_cat_n(output)
    assert locate_quote(lines, "ABC file") == QuoteMatch(1, 1)


def test_curly_quotes_are_not_folded() -> None:
    """NFKC does not fold curly quotes (U+201C/U+201D/U+2018/U+2019) to their straight ASCII
    equivalents, and verification adds no quote-style folding beyond NFKC: this is intentional.
    A model that changes a quote's punctuation has changed the text it quotes, and that
    mismatch must surface as quote_not_found, never be silently bridged."""
    output = numbered_output(["say “hi”"])
    lines = strip_cat_n(output)
    assert locate_quote(lines, 'say "hi"') is None
    assert locate_quote(lines, "say “hi”") == QuoteMatch(1, 1)


def test_trailing_whitespace_is_ignored() -> None:
    output = numbered_output(["def greet():  ", "    return 'hi'"])
    lines = strip_cat_n(output)
    assert locate_quote(lines, "def greet():\n    return 'hi'") == QuoteMatch(1, 2)
    # A quote ending in blank lines still matches: normalize() strips trailing newlines at
    # the end of the text, so the trailing blank lines contribute nothing to the match.
    assert locate_quote(lines, "def greet():\n\n\n") == QuoteMatch(1, 1)


def test_off_by_one_claim_does_not_move_the_true_range() -> None:
    output = numbered_output(
        ["def outer():", "    pass", "def target():", "    return 42", "def other():"],
        start=8,
    )
    lines = strip_cat_n(output)
    assert locate_quote(lines, "def target():\n    return 42") == QuoteMatch(10, 11)


def test_partial_read_keeps_absolute_line_numbers() -> None:
    body_lines = [f"line {i}" for i in range(50, 70)]
    output = numbered_output(body_lines, start=50)
    lines = strip_cat_n(output)
    assert lines[0][0] == 50
    assert lines[-1][0] == 69
    assert locate_quote(lines, "line 55") == QuoteMatch(55, 55)


def test_cat_n_prefix_with_tab_or_arrow_is_stripped() -> None:
    tab_output = "     1\tfoo\tbar\n[Note: truncated]\n     2\tbaz"
    assert strip_cat_n(tab_output) == [(1, "foo\tbar"), (2, "baz")]

    arrow_output = "     1\u2192foo\n     2\u2192bar"
    assert strip_cat_n(arrow_output) == [(1, "foo"), (2, "bar")]


def test_semantic_flip_is_not_a_match() -> None:
    output = numbered_output(["if not authenticated:", "    return 403"])
    lines = strip_cat_n(output)
    assert locate_quote(lines, "if authenticated:\n    return 403") is None


def test_mid_line_and_multi_line_quotes_map_to_absolute_lines() -> None:
    output = numbered_output(["prefix = 1", "value = compute(x, y)", "suffix = 2"], start=5)
    lines = strip_cat_n(output)
    assert locate_quote(lines, "compute(x, y)") == QuoteMatch(6, 6)
    assert locate_quote(lines, "value = compute(x, y)\nsuffix = 2") == QuoteMatch(6, 7)


def test_empty_quote_never_matches() -> None:
    output = numbered_output(["something"])
    lines = strip_cat_n(output)
    assert locate_quote(lines, "") is None
    assert locate_quote(lines, "   \n  ") is None


_IDEMPOTENCE_FIXTURES = [
    "a\r\nb\rc",
    "\tif True:\n\t\treturn 1",
    "ＡＢＣ ﬁle",  # noqa: RUF001
    "say “hi”",
    "def greet():  \n    return 'hi'",
    "if not authenticated:\n    return 403",
    "",
    "   \n  ",
]


def test_normalize_is_idempotent() -> None:
    for text in _IDEMPOTENCE_FIXTURES:
        once = normalize(text)
        assert normalize(once) == once


def test_to_cat_n_round_trips_through_strip_cat_n() -> None:
    lines = ["def greet():", "", "    return 'hi'"]
    assert to_cat_n(lines) == "     1\tdef greet():\n     2\t\n     3\t    return 'hi'"
    assert strip_cat_n(to_cat_n(lines, start=40)) == [
        (40 + i, line) for i, line in enumerate(lines)
    ]
