"""Verification of the demo evidence runs.

When the two evidence runs finish, the worker's terminal hook (:mod:`agenttape.findings.hooks`)
has already verified every citation with the real engine (``method="lexical_v1"``) - this
module's job shrinks to a safety net and one honest, clearly labelled stand-in.

``seed()`` re-runs the idempotent automatic pass (``VerificationService.verify_run(...,
trigger="automatic")``): it records nothing when a citation's engine verdict is already
current (the ordinary case - the worker already checked it), and only does real work when
the hook somehow never ran (a restart between the run finishing and the hook being
registered, for instance). It then appends exactly one labelled stand-in: the engine
correctly computes ``never_read`` for F7, because the demo's evidence window is complete
(AgentTape has no offline gap here and reads are backed by telemetry). A genuinely
incomplete evidence window is not possible with the adapters this seed can run: real ones
come from Claude Code hook coverage gaps (``findings.verify.OBSERVED_GAPS``) and Herdr
runtime reconnects, neither of which a scripted demo run has. The stand-in keeps
``read_not_observed`` visible in the demo: clearly labelled with its own seeded method (see
:func:`seed` below), its reason stating it stands in for an incomplete window, its evidence
naming the engine's own verdict and row id one step back in the append-only history. Every
other designed verdict is the engine's own, proven independently by
``test_demo_agent_telemetry_backs_every_designed_verdict`` and
``test_demo_evidence_run_verdicts_are_computed_by_the_engine``; if the engine
ever disagrees with ``DESIGNED_VERDICTS`` for a non-stand-in citation, this seed reports the
mismatch and writes nothing - it never papers over the engine.

Runs through the HTTP seed endpoint (inside a running event loop) only queue the evidence
runs; a worker executes them later, so this seed does nothing there - it does not wait for
them. ``agenttape demo`` (the basic Demo Agent, which records no findings) is unaffected:
this module only ever reads findings for the two evidence runs ``file_evidence_demo`` created.
"""

from __future__ import annotations

from typing import Any

from agenttape.application.context import AppContext
from agenttape.application.services.findings import FindingsService
from agenttape.application.services.verification import VerificationService
from agenttape.context_evidence.hooks import rescan_run
from agenttape.demo_agent import DESIGNED_VERDICTS
from agenttape.domain.enums import TERMINAL_STATUS_VALUES
from agenttape.infrastructure.db.models import Run
from agenttape.seeds.base import SeedReport
from agenttape.seeds.story_runs import _can_execute_inline

#: Runs after file_evidence_demo (ORDER = 75) so the two evidence runs it creates already
#: exist and (when inline execution is possible) have settled.
ORDER = 80

#: The one designed verdict this seed stands in for: the engine correctly computes
#: never_read for F7 (the demo's evidence window is complete), but the demo wants
#: read_not_observed visible: the closest honest analogue of a genuine incomplete window
#: until a run that can produce one (a Claude Code session with a hook coverage gap, or a
#: Herdr runtime reconnect) is part of this seed's scenario. Every entry here must be a
#: verdict DESIGNED_VERDICTS actually designs.
STAND_INS: dict[tuple[str, int], str] = {("F7", 0): "read_not_observed"}
assert set(STAND_INS.items()) <= set(DESIGNED_VERDICTS.items()), (
    "STAND_INS must only stand in for a verdict DESIGNED_VERDICTS actually designs"
)

#: Never claims a check ran: names exactly what it stands in for and where the engine's own
#: verdict sits in the history (see the ``engine_verdict``/``engine_verdict_id`` evidence).
STAND_IN_REASON = (
    "Seeded demo stand-in: the verifier found no read of this file in a complete evidence "
    "window (see the previous check); this row stands in for an incomplete window, which a "
    "real run has when AgentTape could not see all of it, such as a Claude Code session "
    "without hooks, one whose hooks joined late or were dropped, an AgentTape restart, or a "
    "Herdr runtime reconnect."
)


def seed(ctx: AppContext, report: SeedReport) -> None:
    run_ids: list[str] = report.data.get("evidence_runs") or []
    if not report.include_runs or not run_ids:
        return
    if not (report.execute_inline and _can_execute_inline()):
        return  # HTTP seed only queues the runs; no verdicts to seed yet.
    findings_service = FindingsService(ctx)
    for run_id in run_ids:
        with ctx.session() as session:
            run = session.get(Run, run_id)
            if run is None or not run.is_demo or run.status not in TERMINAL_STATUS_VALUES:
                continue
            title = run.title
        rescan_run(ctx, run_id)  # idempotent: makes sure every FINDING event is ingested.
        # A safety net, not the primary path: the worker's terminal hook already verified
        # this run with the real engine. Records nothing when every verdict is current.
        VerificationService(ctx).verify_run(run_id, trigger="automatic")

        created = 0
        with ctx.session() as session:
            findings = FindingsService.findings_for_run(session, run_id)
        for finding in findings:
            key = finding["key"]
            for citation in finding["citations"]:
                position = citation["position"]
                designed = DESIGNED_VERDICTS.get((key, position))
                if designed is None:
                    continue  # never invent a verdict for a citation missing from the map.
                latest: dict[str, Any] | None = citation["verdict"]
                stand_in_verdict = STAND_INS.get((key, position))
                if stand_in_verdict is not None:
                    if latest is None or latest["method"] != "lexical_v1":
                        continue  # not yet decided by the engine, or already the stand-in.
                    findings_service.record_verdict(
                        citation["id"],
                        verdict=stand_in_verdict,
                        method="seeded",
                        reason=STAND_IN_REASON,
                        evidence={
                            "note": "Seeded demo data: no check ran.",
                            "stand_in_for": "read_not_observed",
                            "engine_verdict": latest["verdict"],
                            "engine_verdict_id": latest["id"],
                        },
                    )
                    created += 1
                    continue
                # Every other designed citation must be the engine's own verdict; never
                # paper over a real disagreement by writing a seeded row on top of it.
                if latest is None or latest["verdict"] != designed:
                    seen = latest["verdict"] if latest is not None else None
                    report.add(
                        "citation_verdict_mismatch",
                        f"{title} {key}#{position}: engine said {seen!r}, designed {designed!r}",
                        created=False,
                    )
        report.add(
            "citation_verdict", f"{title} ({created})" if created else title, created=created > 0
        )
