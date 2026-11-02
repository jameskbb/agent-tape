"""The deterministic verdict rules for a citation, named for exactly what was checked.

Pure module: no ``Session``, no I/O. Every input here is an already-fetched Python value
(:class:`CitationView`, a sequence of :class:`ReadView`); every output is a
:class:`VerdictDraft` or ``None`` (no verdict recorded). ``agenttape.findings.evidence`` does
all the database work; ``agenttape.application.services.verification.VerificationService``
wires the two together and writes through ``FindingsService.record_verdict``.

:func:`decide_file_citation` decides ``quote_matches``, ``stale``, ``never_read``,
``read_not_observed``, ``quote_not_found``, and ``not_checkable``; :func:`decide_event_citation`
decides event citations. Keys in the evidence JSON are only ever added, never renamed.

Evidence JSON contract:

* ``rule``: the method that decided the verdict (``METHOD``, i.e. ``"lexical_v1"``).
* ``line_range``: ``[start, end]``, the true line range the quote actually matched.
* ``claimed_range``: ``[start, end]`` the agent cited, or ``None`` when it cited none.
* ``claimed_range_matches``: whether ``claimed_range`` equals ``line_range``.
* ``read``: ``{evidence_id, call_event_id, result_event_id, sequence, window, truncated,
  blob_ref}`` - the read the verdict was decided from.

A ``stale`` verdict adds ``change`` (the earliest later write: ``evidence_id``,
``source_event_id``, ``operation``, ``ts``, ``sequence``, ``source``), ``later_writes``
(count), ``hash_at_read``, ``hash_at_read_matches_recorded``, and ``hash_after`` - both
hashes are ``None`` when they cannot be established honestly, never guessed.
``hash_at_read`` is only computed from a read that :func:`could_be_whole_file` (from line
1, no gaps, not cut by the size cap or by the call's own ``limit``); it is the Git blob id
of the lines that read returned, and it is proven to be the whole file's hash at the time
of the read only when ``hash_at_read_matches_recorded`` is true, since an adapter's own
unrecorded default line cap can still end a read before the file does.

``inputs_digest`` (:func:`inputs_digest`) is added to every verdict's evidence: a
sha256 of the exact recorded inputs the verdict was decided from, clock-free and
random-free, so ``VerificationService`` can tell an automatic pass's "the recorded inputs
have not changed" apart from "something did change" without re-deciding the verdict.

Evidence keys never contain the words token, secret, password, credential, authorization,
api_key, access_key, or private_key: the before_flush redaction guard masks values under such
keys, and a masked evidence field would silently misrepresent what was actually checked.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agenttape.file_evidence.ledger import blob_hash
from agenttape.findings.normalize import (
    QuoteMatch,
    has_redaction,
    locate_quote,
    normalize,
    strip_cat_n,
)

METHOD = "lexical_v1"
#: Bumped whenever what a verdict means changes for the same recorded inputs. "2": a
#: missing read on a run read only through Claude Code hooks (observed, or Herdr-hosted) is
#: ``never_read`` only when those hooks covered every session whole (:func:`observed_gaps`);
#: "1" decided it with no coverage check. Part of :func:`inputs_digest`, so an automatic
#: pass re-records a verdict an older verifier decided; ``VerificationService.
#: recheck_outdated_never_read`` re-checks the finished runs whose "1" verdicts it may
#: overturn.
VERIFIER_VERSION = "2"

#: Every ``not_checkable`` sub-reason this module can decide, closed - never a bare
#: "not checkable" with no detail.
NOT_CHECKABLE_REASONS: tuple[str, ...] = (
    "outside_boundary",
    "no_quote",
    "quote_truncated",
    "no_content_recorded",
    "content_not_line_numbered",
    "size_cap_exceeded",
    "redacted_at_cited_span",
    "blob_unavailable",
)

#: Past-tense verbs for a write operation, for a stale citation's reason sentence.
_PAST_TENSE = {"create": "created", "modify": "modified", "delete": "deleted", "rename": "renamed"}


@dataclass(frozen=True)
class CitationView:
    kind: str
    path: str | None
    inside_boundary: bool | None
    line_start: int | None
    line_end: int | None
    quote: str | None
    quote_truncated: bool
    event_ref: str | None
    event_id: str | None


@dataclass(frozen=True)
class ReadView:
    evidence_id: str
    path: str
    tool: str | None
    call_event_id: str | None
    result_event_id: str | None
    result_sequence: int
    #: ``(absolute file line number, content)`` pairs from ``strip_cat_n``, or ``None`` when
    #: no content was recorded for this read.
    numbered_lines: tuple[tuple[int, str], ...] | None
    truncated: bool
    blob_ref: str | None
    #: "numbered" (``numbered_lines`` is non-empty), "none" (no output at all), "unnumbered"
    #: (non-empty output with no ``cat -n`` line numbers), or "unavailable" (the read's
    #: content used to exist but retention has pruned it).
    content_state: str = "numbered"
    unavailable_detail: str | None = None
    #: The read call's own ``limit`` (lines requested), when it recorded one.
    limit: int | None = None


@dataclass(frozen=True)
class VerdictDraft:
    verdict: str
    reason: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class EventView:
    """What an event citation resolved to: the recorded event's own content, never the
    agent's prose about what it ran."""

    event_id: str
    type: str
    sequence: int
    content_key: str | None
    content: str | None
    truncated: bool
    placeholder: bool
    #: ``content`` is the read's retained output (up to 1 MiB), not the capped event text.
    retained: bool = False


@dataclass(frozen=True)
class WriteView:
    """A write (or rename) to a citation's path, recorded after some read of it.

    ``ts`` is the ISO-8601 timestamp of the underlying ``file_evidence_events`` row; ordering
    decisions are made by the caller (:func:`agenttape.findings.evidence.writes_after`) using
    the same ``(ts, sequence)`` total order the ledger itself uses - this view only carries
    the already-decided result forward into the evidence JSON.
    """

    evidence_id: str
    source_event_id: str | None
    operation: str
    path: str
    previous_path: str | None
    ts: str
    sequence: int
    source: str
    before_hash: str | None
    after_hash: str | None


@dataclass(frozen=True)
class EvidenceWindow:
    """The run's evidence-window completeness, as far as citation decisions need it.

    ``complete`` is true only when reads are backed by telemetry, there is no offline
    window, and (for a run read only through hooks) :func:`observed_gaps` found no gap in its hook
    coverage (:func:`agenttape.findings.evidence.run_window` derives it from
    ``file_evidence.store.completeness()`` and :func:`observed_gaps`; this module never
    re-derives the completeness logic itself).

    ``observed`` is ``None`` for a run AgentTape launched and reads itself; for an observed
    run or a Herdr-hosted Claude Code session (reads only through hooks) it is
    :meth:`ObservedCoverage.as_evidence` (the recorded coverage facts plus their gaps).
    """

    complete: bool
    reads: str
    offline_windows: tuple[dict[str, Any], ...]
    statement: str
    observed: dict[str, Any] | None = None


#: Every reason an observed session's hook record cannot show AgentTape saw all of its
#: reads, closed. Any one of them makes a missing read ``read_not_observed``, never
#: ``never_read``.
OBSERVED_GAPS: tuple[str, ...] = (
    "no_hooks",  # the run was created by an MCP finding; no hook ever reached it
    "coverage_unknown",  # recorded before AgentTape tracked when a session's hooks began
    "joined_mid_session",  # the first hook recorded was not a fresh SessionStart
    "dropped_hook_events",  # hooks refused (rate limit, full queue) were never recorded
    "observer_restarted",  # AgentTape restarted mid-session; hooks sent meanwhile were lost
)

#: The ``SessionStart`` sources that prove a session began under observation, with nothing
#: in its context from before it: ``startup`` (a new ``claude``), and ``clear`` (after
#: ``/clear`` Claude Code starts a new session with an empty conversation, so no read made
#: before it can back a citation made after it). ``resume``/``fork``/``compact`` all carry
#: on a conversation (or a summary of one) whose earlier reads this run never saw.
FRESH_SESSION_STARTS: frozenset[str] = frozenset({"startup", "clear"})


@dataclass(frozen=True)
class ObservedCoverage:
    """What a hook-read run's hook record proves about which of its reads AgentTape saw
    (an observed run, or a Herdr-hosted Claude Code session).

    Claude Code ``http`` hooks are fire-and-forget pushes: AgentTape learns of a read only
    if the hook reached it. ``agenttape.findings.coverage.observed_coverage`` fills this in
    from every one of the run's ``observed_sessions`` rows; :func:`observed_gaps` decides.
    """

    #: The run's source: ``"hook"`` (created by a Claude Code hook) or ``"mcp"`` (created
    #: by an MCP ``record_finding`` call with no hook session to attach to).
    source: str
    #: Whether the row recorded ``first_hook`` at all (rows from before it was tracked
    #: did not).
    recorded: bool
    first_hook: str | None
    #: ``SessionStart``'s ``source`` when ``first_hook`` is ``SessionStart``.
    first_hook_source: str | None
    dropped_hook_events: int
    observer_restarts: int
    #: How many Claude Code sessions (``observed_sessions`` rows) the run's hooks came
    #: from: one for an observed run, possibly more for a Herdr run whose pane ran several.
    sessions: int = 1

    def as_evidence(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "first_hook": self.first_hook,
            "first_hook_source": self.first_hook_source,
            "dropped_hook_events": self.dropped_hook_events,
            "observer_restarts": self.observer_restarts,
            "sessions": self.sessions,
            "gaps": list(observed_gaps(self)),
        }


def observed_gaps(coverage: ObservedCoverage) -> tuple[str, ...]:
    """Every :data:`OBSERVED_GAPS` code ``coverage`` shows, in that order; empty only when
    AgentTape provably received every hook of the session from its very start.

    Session-wide on purpose, never scoped to the hooks before a finding's sequence: a gap
    is recorded when AgentTape notices it, which can be after the finding although the
    lost hook came first (a hook abandoned before its body was read, a drop no session
    could be named for, the hooks sent while AgentTape was down), so no gap has a lower
    bound it can prove. A gap after every finding therefore still withholds
    ``never_read``, erring toward the neutral verdict."""
    if coverage.source != "hook":
        return ("no_hooks",)
    gaps: list[str] = []
    if not coverage.recorded:
        gaps.append("coverage_unknown")
    elif (
        coverage.first_hook != "SessionStart"
        or coverage.first_hook_source not in FRESH_SESSION_STARTS
    ):
        gaps.append("joined_mid_session")
    if coverage.dropped_hook_events > 0:
        gaps.append("dropped_hook_events")
    if coverage.observer_restarts > 0:
        gaps.append("observer_restarted")
    return tuple(gaps)


def observed_statement(coverage: ObservedCoverage) -> str:
    """One sentence per gap in ``coverage``, for a ``read_not_observed`` reason (``""``
    when there is no gap)."""
    sentences: list[str] = []
    for gap in observed_gaps(coverage):
        if gap == "no_hooks":
            sentences.append(
                "AgentTape received no Claude Code hooks from this session, so it saw none "
                "of its reads."
            )
        elif gap == "coverage_unknown":
            sentences.append(
                "This session was recorded before AgentTape tracked when a session's hooks "
                "began, so it cannot show they covered the whole session."
            )
        elif gap == "joined_mid_session":
            first = coverage.first_hook or "an unknown hook"
            if coverage.first_hook == "SessionStart" and coverage.first_hook_source:
                first = f"SessionStart ({coverage.first_hook_source})"
            which = (
                "this session"
                if coverage.sessions <= 1
                else f"one of this run's {coverage.sessions} Claude Code sessions"
            )
            sentences.append(
                f"Claude Code hooks reached AgentTape only after {which} had started "
                f"(the first was {first}), so earlier reads were not seen."
            )
        elif gap == "dropped_hook_events":
            n = coverage.dropped_hook_events
            sentences.append(
                f"{n} Claude Code hook event{'' if n == 1 else 's'} from this session "
                f"{'was' if n == 1 else 'were'} not recorded."
            )
        elif gap == "observer_restarted":
            which = "this session" if coverage.sessions <= 1 else "this run's sessions"
            sentences.append(
                f"AgentTape restarted while observing {which}; hooks sent while it was "
                "down were lost."
            )
    return " ".join(sentences)


def evidence_window(
    *,
    reads: str,
    offline_windows: Sequence[Mapping[str, Any]],
    statement: str,
    observed: ObservedCoverage | None,
) -> EvidenceWindow:
    """The run's :class:`EvidenceWindow` from its completeness facts: complete only when
    reads are backed by telemetry, AgentTape was never offline, and (for an observed run or
    a Herdr-hosted Claude Code session) the session's hooks show no gap. ``observed`` is
    ``None`` for any other launched run.

    For an observed run with a gap, the statement is the gap(s), since that, not the File
    Evidence write statement, is why a missing read proves nothing. With an offline window
    too it keeps the File Evidence statement, which already names the gaps (it is built
    from the same :func:`observed_statement`); the gaps are added only if it does not.
    """
    offline = tuple(dict(w) for w in offline_windows)
    gaps = observed_gaps(observed) if observed is not None else ()
    if gaps:
        assert observed is not None
        gap_text = observed_statement(observed)
        if not offline:
            statement = gap_text
        elif gap_text not in statement:
            statement = f"{gap_text} {statement}".strip()
    return EvidenceWindow(
        complete=reads == "telemetry" and not offline and not gaps,
        reads=reads,
        offline_windows=offline,
        statement=statement,
        observed=observed.as_evidence() if observed is not None else None,
    )


def _sha256_text(text: str | None) -> str | None:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None


def inputs_digest(
    citation: CitationView,
    *,
    reads: Sequence[ReadView] = (),
    writes_by_read: Mapping[str, Sequence[WriteView]] | None = None,
    window: EvidenceWindow,
    event: EventView | None = None,
    content_digests: Mapping[str, str | None] | None = None,
) -> str:
    """A sha256 hex digest of the exact recorded inputs a verdict was (or would be) decided
    from - never the verdict itself, never a timestamp of the check, never anything random.

    Two calls with the same citation, reads, writes, window, and event content always
    produce the same digest; any change to what was actually recorded (a read's content
    state, a new later write, the evidence window, the resolved event's content) changes
    it. ``content_digests`` names each read's content by ``evidence_id`` (its ``blob_ref``
    - already content-addressed, so it changes exactly when the retained bytes do); a read
    with no entry (never retained) contributes ``None``, distinguishable from a read whose
    content is known.
    """
    writes_by_read = writes_by_read or {}
    content_digests = content_digests or {}
    reads_payload = []
    for read in sorted(reads, key=lambda r: r.result_sequence):
        later = writes_by_read.get(read.evidence_id) or ()
        reads_payload.append(
            {
                "result_event_id": read.result_event_id,
                "content_state": read.content_state,
                "content_digest": content_digests.get(read.evidence_id),
                "truncated": read.truncated,
                "window": (
                    [read.numbered_lines[0][0], read.numbered_lines[-1][0]]
                    if read.numbered_lines
                    else None
                ),
                "later_writes": sorted(w.evidence_id for w in later),
            }
        )
    event_payload: dict[str, Any] | None = None
    if event is not None:
        event_payload = {
            "event_id": event.event_id,
            "content_key": event.content_key,
            "truncated": event.truncated,
            "placeholder": event.placeholder,
            "content_sha256": _sha256_text(event.content),
        }
        if event.retained:
            # Only then, so the digest of every other event citation is unchanged.
            event_payload["retained"] = True
    window_payload: dict[str, Any] = {
        "complete": window.complete,
        "reads": window.reads,
        "offline_windows": [dict(w) for w in window.offline_windows],
    }
    if window.observed is not None:
        # Only a hook-read run carries this key, so a launched run's digest is unchanged.
        window_payload["observed"] = dict(window.observed)
    payload = {
        "method": METHOD,
        "verifier_version": VERIFIER_VERSION,
        "citation": {
            "kind": citation.kind,
            "path": citation.path,
            "inside_boundary": citation.inside_boundary,
            "line_start": citation.line_start,
            "line_end": citation.line_end,
            "event_id": citation.event_id,
            "quote_truncated": citation.quote_truncated,
            "quote_sha256": _sha256_text(citation.quote),
        },
        "reads": reads_payload,
        "window": window_payload,
        "event": event_payload,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def could_be_whole_file(
    numbered_lines: Sequence[tuple[int, str]], *, truncated: bool, limit: int | None
) -> bool:
    """Whether a read *could* be the whole file, as far as its own recording can show.

    True only when the read starts at line 1 with no gaps, was not cut by the size cap
    (``truncated``), and was not cut by the call's own ``limit``: a call that asked for
    ``limit`` lines and got that many back may have stopped before the end of the file, so
    it only counts when fewer lines came back than it asked for. This is a necessary
    condition, not proof - an adapter's own default line cap (unrecorded) can still end a
    read early, so a hash computed from such a read is the file's hash only when it matches
    a recorded whole-file hash (a Git blob, a File Evidence before/start hash).
    """
    if truncated or not numbered_lines or numbered_lines[0][0] != 1:
        return False
    if limit is not None and len(numbered_lines) >= limit:
        return False
    return all(
        numbered_lines[i][0] == numbered_lines[i - 1][0] + 1 for i in range(1, len(numbered_lines))
    )


def blob_id_candidates(numbered_lines: Sequence[tuple[int, str]]) -> tuple[str, str]:
    """The Git blob id of ``numbered_lines`` joined with LF, with and without a final LF.

    Two candidates, never a third guess: a read's numbered lines are line-numbered text, not
    the file's exact original bytes, so whether the file itself ended in a trailing newline
    is not knowable from the numbered lines alone.
    """
    joined = "\n".join(text for _, text in numbered_lines)
    with_trailing_lf = blob_hash((joined + "\n").encode("utf-8"))
    without_trailing_lf = blob_hash(joined.encode("utf-8"))
    return with_trailing_lf, without_trailing_lf


def _not_checkable(
    reason_code: str, message: str, *, extra: dict[str, Any] | None = None
) -> VerdictDraft:
    evidence: dict[str, Any] = {"rule": METHOD, "not_checkable_reason": reason_code}
    if extra:
        evidence.update(extra)
    return VerdictDraft(verdict="not_checkable", reason=message, evidence=evidence)


def _window_verdict(
    citation: CitationView,
    window: EvidenceWindow | None,
    *,
    windows: Sequence[tuple[int, int]] | None = None,
) -> VerdictDraft:
    """``never_read`` (complete window) or ``read_not_observed`` (incomplete window).

    The one place either verdict is decided, for file and event citations alike: an
    observed run's hook-coverage gaps (:func:`observed_gaps`) already made
    ``window.complete`` false, so they never reach ``never_read``.

    ``windows`` is given only from the coverage fallback (some numbered read(s) exist but do
    not cover the claimed lines) - it names the lines this run actually returned instead of
    claiming the file was never read at all.
    """
    win = (
        window
        if window is not None
        else EvidenceWindow(complete=False, reads="unavailable", offline_windows=(), statement="")
    )
    window_evidence: dict[str, Any] = {
        "complete": win.complete,
        "reads": win.reads,
        "offline_windows": list(win.offline_windows),
        "statement": win.statement,
    }
    if win.observed is not None:
        window_evidence["observed"] = dict(win.observed)
    evidence: dict[str, Any] = {"rule": METHOD, "window": window_evidence}
    if win.complete:
        if windows:
            window_text = ", ".join(f"{a}-{b}" for a, b in windows)
            if citation.line_start is not None:
                end = citation.line_end if citation.line_end is not None else citation.line_start
                claim_text = f"{citation.line_start}-{end}"
            else:
                claim_text = "cited"
            reason = (
                f"This run read lines {window_text} of {citation.path}; the {claim_text} "
                "lines were never returned."
            )
        else:
            reason = f"This run has no recorded read of {citation.path}."
        return VerdictDraft(verdict="never_read", reason=reason, evidence=evidence)
    if windows:
        window_text = ", ".join(f"{a}-{b}" for a, b in windows)
        observed_text = (
            f"No read of the cited lines of {citation.path} was observed (this run's "
            f"recorded reads returned lines {window_text})"
        )
    else:
        observed_text = f"No read of {citation.path} was observed"
    reason = f"{observed_text}, and this run's evidence window is incomplete: {win.statement}"
    return VerdictDraft(verdict="read_not_observed", reason=reason, evidence=evidence)


def _lines_in_range(numbered_lines: Sequence[tuple[int, str]], start: int, end: int) -> list[str]:
    return [text for line_no, text in numbered_lines if start <= line_no <= end]


def _claimed_lines_covered(windows: Sequence[tuple[int, int]], start: int, end: int) -> bool:
    return all(
        any(w_start <= line <= w_end for w_start, w_end in windows)
        for line in range(start, end + 1)
    )


def _match_evidence(
    citation: CitationView, read: ReadView, match: QuoteMatch, claimed_range: list[int] | None
) -> tuple[dict[str, Any], str]:
    assert read.numbered_lines is not None
    true_range = [match.line_start, match.line_end]
    claimed_range_matches = claimed_range == true_range
    reason = (
        f"The quote appears in lines {match.line_start}-{match.line_end} of what this "
        f"run's read of {citation.path} returned."
    )
    if claimed_range is not None and not claimed_range_matches:
        reason += f" The agent cited lines {claimed_range[0]}-{claimed_range[1]}."
    window_range = [read.numbered_lines[0][0], read.numbered_lines[-1][0]]
    evidence: dict[str, Any] = {
        "rule": METHOD,
        "line_range": true_range,
        "claimed_range": claimed_range,
        "claimed_range_matches": claimed_range_matches,
        "read": {
            "evidence_id": read.evidence_id,
            "call_event_id": read.call_event_id,
            "result_event_id": read.result_event_id,
            "sequence": read.result_sequence,
            "window": window_range,
            "truncated": read.truncated,
            "blob_ref": read.blob_ref,
        },
    }
    return evidence, reason


def _stale_draft(
    citation: CitationView,
    read: ReadView,
    match: QuoteMatch,
    later_writes: Sequence[WriteView],
    start_hashes: Mapping[str, str | None],
    base_evidence: dict[str, Any],
) -> VerdictDraft:
    assert read.numbered_lines is not None
    change = later_writes[0]
    verb = _PAST_TENSE.get(change.operation, change.operation)
    reason = (
        f"The quote matched lines {match.line_start}-{match.line_end} of what this run "
        f"read from {citation.path}, and {citation.path} was {verb} later in the run."
    )
    hash_at_read: str | None = None
    hash_at_read_matches_recorded = False
    if could_be_whole_file(read.numbered_lines, truncated=read.truncated, limit=read.limit):
        start_hash_value = start_hashes.get(citation.path) if citation.path is not None else None
        with_lf, without_lf = blob_id_candidates(read.numbered_lines)
        if with_lf in (change.before_hash, start_hash_value):
            hash_at_read, hash_at_read_matches_recorded = with_lf, True
        elif without_lf in (change.before_hash, start_hash_value):
            hash_at_read, hash_at_read_matches_recorded = without_lf, True
        else:
            hash_at_read = with_lf
    hash_after = next((w.after_hash for w in later_writes if w.after_hash), None)
    evidence = {
        **base_evidence,
        "change": {
            "evidence_id": change.evidence_id,
            "source_event_id": change.source_event_id,
            "operation": change.operation,
            "ts": change.ts,
            "sequence": change.sequence,
            "source": change.source,
        },
        "later_writes": len(later_writes),
        "hash_at_read": hash_at_read,
        "hash_at_read_matches_recorded": hash_at_read_matches_recorded,
        "hash_after": hash_after,
    }
    return VerdictDraft(verdict="stale", reason=reason, evidence=evidence)


def decide_file_citation(
    citation: CitationView,
    reads: Sequence[ReadView],
    *,
    writes_by_read: Mapping[str, Sequence[WriteView]] | None = None,
    window: EvidenceWindow | None = None,
    start_hashes: Mapping[str, str | None] | None = None,
) -> VerdictDraft:
    """One of the six citation verdicts for a file citation - always, never ``None``.

    Order: (1) ``inside_boundary`` false is ``outside_boundary`` regardless of evidence.
    (2) No recorded read of the path at all is the window rule (``never_read`` /
    ``read_not_observed``). (3) No quote, or a normalized-blank one, is ``no_quote``.
    (4) A quote AgentTape itself had to truncate at ingest is ``quote_truncated``. (5) The
    newest numbered read (by ``result_sequence``) whose content contains the (normalized)
    quote wins: ``redacted_at_cited_span`` when the matched span itself is a redaction
    marker, else ``stale`` when that read has a later write, else ``quote_matches``.
    (6) No read matches: ``blob_unavailable`` when a read is unavailable; ``size_cap_exceeded``
    when a truncated numbered read's last returned line is before the claimed end (or there
    is no claim); ``redacted_at_cited_span`` when the claimed span (or, with no claim, any
    numbered read) contains the marker; then coverage - ``quote_not_found`` when every claimed
    line (or, with no claim, any numbered read) was actually returned, else
    ``no_content_recorded`` / ``content_not_line_numbered`` / the window rule, in that order.
    """
    writes_by_read = writes_by_read or {}
    start_hashes = start_hashes or {}

    if citation.inside_boundary is False:
        return _not_checkable(
            "outside_boundary",
            f"{citation.path} is outside the run's project boundary; AgentTape does not "
            "check citations outside it.",
        )

    if not reads:
        return _window_verdict(citation, window)

    quote = citation.quote
    if quote is None or not normalize(quote).strip():
        return _not_checkable("no_quote", "This citation names no quote to check.")

    if citation.quote_truncated:
        return _not_checkable(
            "quote_truncated",
            "The agent's quote was cut before AgentTape recorded it; a truncated quote "
            "cannot be checked.",
        )

    claim_start = citation.line_start
    claim_end: int | None = None
    claimed_range: list[int] | None = None
    if claim_start is not None:
        claim_end = citation.line_end if citation.line_end is not None else claim_start
        claimed_range = [claim_start, claim_end]

    numbered_reads = [r for r in reads if r.numbered_lines]
    for read in sorted(numbered_reads, key=lambda r: r.result_sequence, reverse=True):
        assert read.numbered_lines is not None
        match = locate_quote(list(read.numbered_lines), quote)
        if match is None:
            continue
        base_evidence, reason = _match_evidence(citation, read, match, claimed_range)
        matched_text = "\n".join(
            _lines_in_range(read.numbered_lines, match.line_start, match.line_end)
        )
        if has_redaction(matched_text):
            return VerdictDraft(
                verdict="not_checkable",
                reason=(
                    f"The matched text in lines {match.line_start}-{match.line_end} of "
                    f"{citation.path} is redacted; AgentTape cannot compare redacted content."
                ),
                evidence={**base_evidence, "not_checkable_reason": "redacted_at_cited_span"},
            )
        later_writes = list(writes_by_read.get(read.evidence_id) or ())
        if later_writes:
            return _stale_draft(citation, read, match, later_writes, start_hashes, base_evidence)
        return VerdictDraft(verdict="quote_matches", reason=reason, evidence=base_evidence)

    # No read's recorded content contains the quote.
    if any(r.content_state == "unavailable" for r in reads):
        return _not_checkable(
            "blob_unavailable",
            f"The content AgentTape retained for {citation.path} is missing or could not be "
            "loaded, so it cannot be checked.",
        )

    for read in numbered_reads:
        assert read.numbered_lines is not None
        if not read.truncated:
            continue
        last_line = read.numbered_lines[-1][0]
        if claim_end is None or claim_end > last_line:
            return _not_checkable(
                "size_cap_exceeded",
                f"AgentTape's recorded read of {citation.path} was cut before the cited "
                "lines; content beyond the cut cannot be checked.",
            )

    for read in numbered_reads:
        assert read.numbered_lines is not None
        if claimed_range is not None:
            text_to_check = "\n".join(
                _lines_in_range(read.numbered_lines, claimed_range[0], claimed_range[1])
            )
        else:
            text_to_check = "\n".join(text for _, text in read.numbered_lines)
        if text_to_check and has_redaction(text_to_check):
            return _not_checkable(
                "redacted_at_cited_span",
                f"What AgentTape recorded for {citation.path} at the cited span is "
                "redacted; it cannot compare redacted content.",
            )

    windows = [
        (r.numbered_lines[0][0], r.numbered_lines[-1][0])
        for r in numbered_reads
        if r.numbered_lines
    ]
    covered = (
        _claimed_lines_covered(windows, claimed_range[0], claimed_range[1])
        if claimed_range is not None
        else bool(numbered_reads)
    )
    if covered:
        return VerdictDraft(
            verdict="quote_not_found",
            reason=f"{citation.path} was read, and the quote is not in what the run received.",
            evidence={"rule": METHOD, "line_range": None, "claimed_range": claimed_range},
        )

    if any(r.content_state == "none" for r in reads):
        return _not_checkable(
            "no_content_recorded",
            f"{citation.path} was read, but no content was recorded for that read.",
        )
    if any(r.content_state == "unnumbered" for r in reads):
        return _not_checkable(
            "content_not_line_numbered",
            f"{citation.path} was read, but the recorded output has no line numbers to "
            "check against.",
        )
    return _window_verdict(citation, window, windows=windows)


def _event_evidence(event: EventView) -> dict[str, Any]:
    out: dict[str, Any] = {
        "event_id": event.event_id,
        "type": event.type,
        "sequence": event.sequence,
        "content_key": event.content_key,
    }
    if event.retained:
        out["retained"] = True
    return out


def decide_event_citation(
    citation: CitationView, event: EventView | None, *, window: EvidenceWindow
) -> VerdictDraft:
    """One of the six citation verdicts for an event citation.

    Checked against the recorded event's own stored output only (for a read's result over
    the event text cap, the read's retained output, ``event.retained``) - never the agent's
    prose about what it ran. An unresolved reference (``event is None``) gets the same window rule
    a file citation with no read gets. A ``Read`` tool_result's ``cat -n`` layout is mapped
    through :func:`~agenttape.findings.normalize.strip_cat_n` exactly like a file citation's
    read content; other event content is numbered from 1.
    """
    if event is None:
        return _window_verdict(citation, window)
    if event.placeholder:
        return _not_checkable(
            "size_cap_exceeded",
            "The recorded event's payload was replaced by a size-cap placeholder; its "
            "content cannot be checked.",
            extra={"event": _event_evidence(event)},
        )
    if event.content is None:
        return _not_checkable(
            "no_content_recorded",
            "No content was recorded for the referenced event.",
            extra={"event": _event_evidence(event)},
        )
    quote = citation.quote
    if quote is None or not normalize(quote).strip():
        return _not_checkable(
            "no_quote",
            "This citation names no quote to check.",
            extra={"event": _event_evidence(event)},
        )
    numbered = strip_cat_n(event.content)
    if not numbered:
        numbered = list(enumerate(event.content.split("\n"), start=1))
    match = locate_quote(numbered, quote)
    if match is not None:
        return VerdictDraft(
            verdict="quote_matches",
            reason=(
                f"The quote appears in lines {match.line_start}-{match.line_end} of the "
                f"recorded event's {event.content_key or 'content'}."
            ),
            evidence={
                "rule": METHOD,
                "line_range": [match.line_start, match.line_end],
                "claimed_range": None,
                "event": _event_evidence(event),
            },
        )
    if event.truncated:
        return _not_checkable(
            "size_cap_exceeded",
            "The recorded event's content was cut before the cited text; content beyond "
            "the cut cannot be checked.",
            extra={"event": _event_evidence(event)},
        )
    if has_redaction(event.content):
        return _not_checkable(
            "redacted_at_cited_span",
            "The recorded event's content is redacted; AgentTape cannot compare redacted content.",
            extra={"event": _event_evidence(event)},
        )
    return VerdictDraft(
        verdict="quote_not_found",
        reason="The event was recorded, and the quote is not in what it reported.",
        evidence={"rule": METHOD, "line_range": None, "event": _event_evidence(event)},
    )
