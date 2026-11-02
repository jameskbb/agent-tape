"""A read's full redacted output, kept for verification beyond the event text cap.

Every event's text is capped at ``max_event_text_bytes`` (16 KiB) before it is stored
(``application/events.py``), so the ``tool_result`` of a whole-file read of a larger file
stops partway through. When such a result belongs to a read (a call File Evidence records
as a read, :func:`~agenttape.file_evidence.ledger.is_read_call`), ``EventRecorder`` also
writes the same redacted output, cut at :data:`MAX_RETAINED_BYTES` on a line boundary, to
the run's evidence directory (``runs/<run_id>/evidence/<sha256>``, content-addressed and
deleted with the run), and records a small reference to it in the event payload under
:data:`RETAINED_KEY`. Verification (``findings/retention.py``) retains that copy instead of
the capped event text. The event row itself never grows.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

from agenttape.artifacts.store import ArtifactStore
from agenttape.domain.errors import PolicyViolationError

log = logging.getLogger(__name__)

#: Retained read content is capped at 1 MiB, cut at a line boundary. A module attribute
#: (not a constant folded at import time) so tests can monkeypatch it.
MAX_RETAINED_BYTES = 1024 * 1024

#: The ``tool_result`` payload key holding a :class:`RetainedOutputRef`. Only
#: ``EventRecorder`` sets it: a draft's own value is always dropped, so an agent-supplied
#: payload can never point verification at content the run did not receive.
RETAINED_KEY = "output_retained"

_SHA256 = re.compile(r"[0-9a-f]{64}")


def cut_at_line_boundary(data: bytes, limit: int) -> tuple[bytes, bool]:
    """``data`` capped at ``limit`` bytes, cut at the last LF at or before the limit."""
    if len(data) <= limit:
        return data, False
    cut = data.rfind(b"\n", 0, limit + 1)
    if cut == -1:
        return data[:limit], True
    return data[:cut], True


def evidence_key(run_id: str, sha256: str) -> str:
    """The artifact-store key of a retained copy: the run's evidence directory."""
    return f"runs/{run_id}/evidence/{sha256}"


@dataclass(frozen=True)
class RetainedOutputRef:
    """Where a read's retained output lives, and whether it was cut at the cap."""

    storage_path: str
    sha256: str
    size_bytes: int
    truncated: bool

    def as_payload(self) -> dict[str, Any]:
        return {
            "storage_path": self.storage_path,
            "sha256": self.sha256,
            "bytes": self.size_bytes,
            "truncated": self.truncated,
        }

    @classmethod
    def from_payload(cls, value: Any) -> RetainedOutputRef | None:
        """The reference in a stored payload, or ``None`` when absent or malformed."""
        if not isinstance(value, dict):
            return None
        path, sha, size, truncated = (
            value.get("storage_path"),
            value.get("sha256"),
            value.get("bytes"),
            value.get("truncated"),
        )
        if not (isinstance(path, str) and path and isinstance(sha, str)):
            return None
        if not _SHA256.fullmatch(sha):
            return None
        if not isinstance(size, int) or isinstance(size, bool) or not isinstance(truncated, bool):
            return None
        return cls(storage_path=path, sha256=sha, size_bytes=size, truncated=truncated)


def store_output(store: ArtifactStore, run_id: str, text: str) -> RetainedOutputRef:
    """Write ``text`` (already redacted by the caller) capped at :data:`MAX_RETAINED_BYTES`.

    Raises whatever the store raises (``OSError``, ``ArtifactTooLargeError``); the caller
    decides what a failed write means.
    """
    data, cut = cut_at_line_boundary(text.encode("utf-8"), MAX_RETAINED_BYTES)
    sha256 = hashlib.sha256(data).hexdigest()
    stored = store.put_bytes(evidence_key(run_id, sha256), data)
    return RetainedOutputRef(
        storage_path=stored.storage_path,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        truncated=cut,
    )


def load_output(store: ArtifactStore, run_id: str, ref: RetainedOutputRef) -> str | None:
    """The retained text for ``ref`` on ``run_id``, or ``None`` when its file is gone or
    cannot be read, is not the size recorded, or no longer matches its hash.

    The file is always looked up in ``run_id``'s own evidence directory by hash
    (:func:`evidence_key`), never at a path taken from the payload. Every ``None`` is
    logged; an unexpected error (a programming error, not a storage one) is raised.
    """
    try:
        with store.open(evidence_key(run_id, ref.sha256)) as fh:
            data = fh.read(ref.size_bytes + 1)
    except (OSError, PolicyViolationError) as exc:
        log.warning(
            "retained output sha256:%s of run %s could not be read: %s",
            ref.sha256,
            run_id,
            type(exc).__name__,
        )
        return None
    if len(data) != ref.size_bytes or hashlib.sha256(data).hexdigest() != ref.sha256:
        log.warning(
            "retained output sha256:%s of run %s is not the size or hash recorded",
            ref.sha256,
            run_id,
        )
        return None
    return data.decode("utf-8", errors="replace")
