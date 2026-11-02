"""The findings domain: agent findings, their citations, and how citations are checked.

``agenttape.application.services.findings.FindingsService`` is the only reader and writer
of the ``findings``, ``citations``, and ``citation_verdicts`` tables. In this package,
``normalize.py``, ``citations.py``, ``verify.py``, ``review.py``, and ``reproduce.py`` are
pure (no database, no I/O); ``evidence.py`` holds the run-scoped read and event lookups;
``coverage.py`` reads an observed run's hook coverage facts (shared with File Evidence);
``retention.py`` is the only writer of the ``evidence_blobs`` and ``evidence_reads`` tables
(and of the retained-content files a verdict records, alongside the over-cap read output
``retained_output.py`` keeps as each read is recorded); ``hooks.py`` registers the run-terminal hook that
verifies a finished run's citations and queues its automatic re-runs.

A citation verdict says what AgentTape mechanically checked (the quote is in the content the
run received, the file changed since it was read, no read covers the cited lines, and so
on); it never says the agent's conclusion is true. See ``citations.CITATION_VERDICTS`` for
the exact vocabulary and what each value records.
"""

from __future__ import annotations
