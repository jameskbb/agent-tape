"""Citation vocabulary and the pure parser for citation-shaped links.

A FINDING event's ``links`` may be a legacy source link (``source_id``, ``relation``) or a
citation: a file line range and quote (``path``, ``line_start``, ``line_end``, ``quote``), or
a reference to a recorded event (``event_ref``, ``quote``). Only citation-shaped links -
those carrying a ``path`` or an ``event_ref`` - become :class:`CitationDraft` rows here;
legacy links are left to the existing context-evidence source-link path.

Nothing in this module touches the database or the filesystem: path canonicalization (which
needs the run's working directory) and redaction (applied at the persistence boundary) are
the caller's job - ``agenttape.application.services.findings.FindingsService``.
"""

from __future__ import annotations

from dataclasses import dataclass

from agenttape.context_evidence.common import RELATIONS

#: What AgentTape mechanically checked about a citation, named for exactly what was checked -
#: never a bare "verified". A verdict never claims the agent's conclusion is true.
CITATION_VERDICTS: tuple[str, ...] = (
    "quote_matches",  # the quote is in the content the run received
    "stale",  # it matched what was read, and the file changed after that read
    "never_read",  # no observed read covers the cited lines
    "read_not_observed",  # the run's evidence window is incomplete, so a missing read proves nothing
    "quote_not_found",  # the file was read and the quote is not in what was returned
    "not_checkable",  # the read content was not retained; the reason says why
)

#: A finding-level state derived from zero citations. Never stored on a citation.
DECLARED_ONLY = "declared_only"
assert DECLARED_ONLY not in CITATION_VERDICTS

#: Caps on untrusted input from a FINDING event's ``links``. Cuts and drops are recorded by
#: the caller (``FindingsService``), never silently applied.
MAX_CITATIONS = 50
MAX_QUOTE_CHARS = 4000
MAX_EVENT_REF_CHARS = 200
MAX_PATH_CHARS = 4096
MAX_LINE = 10_000_000


@dataclass(frozen=True)
class CitationDraft:
    kind: str  #: "file" or "event"
    #: Raw, stripped; canonicalization against the run's working directory is the service's
    #: job (it knows the workdir, this module does not).
    path: str | None
    line_start: int | None
    line_end: int | None
    quote: str | None
    quote_truncated: bool
    event_ref: str | None
    relation: str


def _line(value: object) -> int | None:
    """An int line number in [1, MAX_LINE] (bools are ints in Python; reject them)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1 or value > MAX_LINE:
        return None
    return value


def _quote(value: object) -> tuple[str | None, bool]:
    if not isinstance(value, str):
        return None, False
    if len(value) > MAX_QUOTE_CHARS:
        return value[:MAX_QUOTE_CHARS], True
    return value, False


def parse_citations(links: object) -> tuple[list[CitationDraft], int]:
    """Turn a FINDING event's ``links`` into citation drafts.

    Only links carrying a string ``path`` or ``event_ref`` are citation-shaped; a legacy
    ``{source_id, relation}`` link and any non-dict entry yield no draft and are not counted
    as dropped. Among citation-shaped links, positions are contiguous (the returned list's
    order); a link with an invalid path (containing NUL, or longer than ``MAX_PATH_CHARS``)
    is dropped, and citation-shaped links beyond ``MAX_CITATIONS`` are dropped too. Returns
    ``(drafts, dropped)``.
    """
    if not isinstance(links, list):
        return [], 0
    drafts: list[CitationDraft] = []
    dropped = 0
    citation_shaped = 0
    for link in links:
        if not isinstance(link, dict):
            continue
        raw_path = link.get("path")
        raw_event_ref = link.get("event_ref")
        has_path = isinstance(raw_path, str) and raw_path.strip() != ""
        has_event_ref = isinstance(raw_event_ref, str) and raw_event_ref.strip() != ""
        if not has_path and not has_event_ref:
            continue  # not citation-shaped: legacy source link, or junk
        citation_shaped += 1
        if citation_shaped > MAX_CITATIONS:
            dropped += 1
            continue
        path_value: str | None = None
        if has_path:
            path_text = raw_path.strip()  # type: ignore[union-attr]
            if "\x00" in path_text or len(path_text) > MAX_PATH_CHARS:
                dropped += 1
                continue
            path_value = path_text
        event_ref_value: str | None = None
        if has_event_ref:
            event_ref_text = raw_event_ref.strip()  # type: ignore[union-attr]
            event_ref_value = event_ref_text[:MAX_EVENT_REF_CHARS]
        quote, quote_truncated = _quote(link.get("quote"))
        relation = link.get("relation")
        relation = relation if relation in RELATIONS else "supports"
        drafts.append(
            CitationDraft(
                kind="file" if path_value is not None else "event",
                path=path_value,
                line_start=_line(link.get("line_start")),
                line_end=_line(link.get("line_end")),
                quote=quote,
                quote_truncated=quote_truncated,
                event_ref=event_ref_value,
                relation=relation,
            )
        )
    return drafts, dropped
