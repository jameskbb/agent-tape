"""Scheduler integration: occurrence creation, lease ownership, restart survival, policies."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from agenttape.application.context import AppContext
from agenttape.application.schemas.schedules import ScheduleCreate, ScheduleUpdate
from agenttape.application.services.runs import RunService
from agenttape.application.services.schedules import ScheduleService
from agenttape.bootstrap import build_context
from agenttape.config import Settings
from agenttape.domain.errors import ValidationError
from agenttape.infrastructure.clock import ManualClock
from agenttape.infrastructure.db.models import Run, ScheduleOccurrence
from agenttape.scheduler.engine import SchedulerEngine
from agenttape.scheduler.policies import OccurrenceStatus
from tests.conftest import demo_profile, simple_prompt

START = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START)


@pytest.fixture
def mctx(settings: Settings, clock: ManualClock) -> Iterator[AppContext]:
    context = build_context(settings, clock=clock, migrate=True)
    yield context
    context.close()


def make_schedule(ctx: AppContext, **overrides: Any) -> str:
    profile = overrides.pop("agent_profile_id", None) or demo_profile(
        ctx, name=f"p-{overrides.get('name', 's')}"
    )
    prompt = overrides.pop("prompt_template_id", None) or simple_prompt(
        ctx, name=f"t-{overrides.get('name', 's')}"
    )
    data: dict[str, Any] = {
        "name": "Nightly",
        "kind": "interval",
        "interval_seconds": 3600,
        "timezone": "UTC",
        "agent_profile_id": profile,
        "prompt_template_id": prompt,
    }
    data.update(overrides)
    return ScheduleService(ctx).create(ScheduleCreate(**data)).id


def runs_for(ctx: AppContext, schedule_id: str) -> list[Run]:
    with ctx.session() as session:
        return list(
            session.scalars(
                select(Run).where(Run.schedule_id == schedule_id).order_by(Run.created_at)
            )
        )


def occurrences(ctx: AppContext, schedule_id: str) -> list[ScheduleOccurrence]:
    with ctx.session() as session:
        return list(
            session.scalars(
                select(ScheduleOccurrence)
                .where(ScheduleOccurrence.schedule_id == schedule_id)
                .order_by(ScheduleOccurrence.occurrence_at)
            )
        )


def finish(ctx: AppContext, run_id: str) -> None:
    RunService(ctx).cancel_run(run_id, reason="test finished it")


# --------------------------------------------------------------------------- basics


def test_due_occurrence_creates_a_normal_run(mctx: AppContext, clock: ManualClock) -> None:
    sid = make_schedule(mctx)
    engine = SchedulerEngine(mctx, owner_id="a")
    assert engine.tick()["fired"] == 0
    clock.advance(3600)
    report = engine.tick()
    assert report["leader"] and report["fired"] == 1
    runs = runs_for(mctx, sid)
    assert len(runs) == 1
    run = runs[0]
    assert run.source == "schedule"
    assert run.status == "queued"
    assert run.idempotency_key == f"schedule:{sid}:{(START + timedelta(hours=1)).isoformat()}"
    types = [e.type for e in RunService(mctx).list_events(run.id).items]
    assert "scheduler" in types
    occ = occurrences(mctx, sid)
    assert [o.status for o in occ] == ["created"]
    assert occ[0].run_id == run.id
    s = ScheduleService(mctx).get(sid)
    assert s.last_run_id == run.id
    assert s.next_run_at == START + timedelta(hours=2)


def test_ticking_again_never_duplicates(mctx: AppContext, clock: ManualClock) -> None:
    sid = make_schedule(mctx, concurrency_policy="skip")
    engine = SchedulerEngine(mctx, owner_id="a")
    clock.advance(3600)
    for _ in range(5):
        engine.tick()
    assert len(runs_for(mctx, sid)) == 1
    assert len(occurrences(mctx, sid)) == 1


def test_cron_schedule_uses_time_zone(mctx: AppContext, clock: ManualClock) -> None:
    # 07:00 in New York on 2026-03-03 is 12:00 UTC.
    sid = make_schedule(mctx, kind="cron", cron_expression="0 7 * * *", timezone="America/New_York")
    s = ScheduleService(mctx).get(sid)
    assert s.next_run_at == datetime(2026, 3, 3, 12, 0, tzinfo=UTC)
    assert s.next_run_local == "2026-03-03T07:00:00-05:00"
    clock.set(datetime(2026, 3, 3, 12, 0, 5, tzinfo=UTC))
    SchedulerEngine(mctx, owner_id="a").tick()
    assert len(runs_for(mctx, sid)) == 1


def test_one_time_runs_once_and_past_times_are_rejected(
    mctx: AppContext, clock: ManualClock
) -> None:
    with pytest.raises(ValidationError):
        make_schedule(mctx, kind="one_time", run_at=START - timedelta(minutes=1))
    sid = make_schedule(mctx, name="once", kind="one_time", run_at=START + timedelta(minutes=5))
    engine = SchedulerEngine(mctx, owner_id="a")
    clock.advance(301)
    engine.tick()
    clock.advance(3600)
    engine.tick()
    assert len(runs_for(mctx, sid)) == 1
    assert ScheduleService(mctx).get(sid).next_run_at is None


def test_naive_times_are_local_to_the_schedule_zone(mctx: AppContext) -> None:
    sid = make_schedule(
        mctx, kind="one_time", run_at=datetime(2026, 3, 10, 9, 0), timezone="Europe/Berlin"
    )
    s = ScheduleService(mctx).get(sid)
    assert s.run_at == datetime(2026, 3, 10, 8, 0, tzinfo=UTC)
    assert s.run_at_local == "2026-03-10T09:00:00+01:00"


# --------------------------------------------------------------------------- lease


def test_standby_scheduler_does_nothing_until_the_lease_expires(
    mctx: AppContext, clock: ManualClock
) -> None:
    sid = make_schedule(mctx, concurrency_policy="queue", max_queued=10)
    a = SchedulerEngine(mctx, owner_id="a", lease_seconds=4000)
    b = SchedulerEngine(mctx, owner_id="b", lease_seconds=4000)
    assert a.tick()["leader"] is True
    assert b.tick()["leader"] is False
    clock.advance(3600)
    assert b.tick() == {"leader": False}
    assert runs_for(mctx, sid) == []
    # a dies without releasing; after the TTL b takes over and creates the occurrence once.
    clock.advance(4001)
    assert b.tick()["leader"] is True
    assert len(runs_for(mctx, sid)) == 1
    assert a.tick()["leader"] is False
    assert ScheduleService(mctx).status().owner_id == "b"


def test_two_schedulers_never_create_duplicate_occurrences(
    settings: Settings, clock: ManualClock
) -> None:
    """Two scheduler instances (separate contexts, like separate processes) racing on the
    same database, both through the lease and with the lease bypassed entirely."""
    ctx_a = build_context(settings, clock=clock, migrate=True)
    ctx_b = build_context(settings, clock=clock)
    try:
        sids = [
            make_schedule(
                ctx_a, name=f"s{i}", interval_seconds=60, concurrency_policy="queue", max_queued=100
            )
            for i in range(3)
        ]
        a = SchedulerEngine(ctx_a, owner_id="a")
        b = SchedulerEngine(ctx_b, owner_id="b")
        errors: list[BaseException] = []

        def race(fn: Any) -> None:
            barrier = threading.Barrier(2)

            def work(engine: SchedulerEngine) -> None:
                try:
                    barrier.wait()
                    fn(engine)
                except BaseException as exc:  # pragma: no cover - reported below
                    errors.append(exc)

            threads = [threading.Thread(target=work, args=(e,)) for e in (a, b)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        for _step in range(6):
            clock.advance(60)
            race(lambda e: e.tick())  # through the lease
            clock.advance(60)
            race(lambda e: e.leader_pass())  # both act as leader at once
        assert not errors, errors
        for sid in sids:
            occ = occurrences(ctx_a, sid)
            times = [o.occurrence_at for o in occ]
            assert len(times) == len(set(times))
            runs = runs_for(ctx_a, sid)
            keys = [r.idempotency_key for r in runs]
            assert len(keys) == len(set(keys))
            linked = [o.run_id for o in occ if o.run_id]
            assert len(linked) == len(set(linked))
            assert len(occ) == 12  # one per minute, none lost, none doubled
    finally:
        ctx_a.close()
        ctx_b.close()


# --------------------------------------------------------------------------- restart


def test_schedules_survive_restart_and_misfires_run_once(
    settings: Settings, clock: ManualClock
) -> None:
    ctx1 = build_context(settings, clock=clock, migrate=True)
    sid = make_schedule(ctx1, misfire_policy="run_once", concurrency_policy="queue", max_queued=5)
    SchedulerEngine(ctx1, owner_id="first").tick()
    ctx1.close()  # "process" stops without releasing its lease

    clock.advance(5 * 3600 + 30 * 60)  # five occurrences pass while nothing runs
    ctx2 = build_context(settings, clock=clock)
    try:
        engine = SchedulerEngine(ctx2, owner_id="second")
        report = engine.tick()
        assert report["leader"] and report["fired"] == 1 and report["missed"] == 4
        occ = occurrences(ctx2, sid)
        assert [o.status for o in occ] == ["missed"] * 4 + ["created"]
        assert len(runs_for(ctx2, sid)) == 1
        engine.tick()
        assert len(runs_for(ctx2, sid)) == 1
        assert ScheduleService(ctx2).get(sid).next_run_at == START + timedelta(hours=6)
    finally:
        ctx2.close()


def test_misfire_skip_fires_nothing_late(mctx: AppContext, clock: ManualClock) -> None:
    sid = make_schedule(mctx, misfire_policy="skip")
    clock.advance(3 * 3600 + 600)
    report = SchedulerEngine(mctx, owner_id="a").tick()
    assert report["fired"] == 0 and report["missed"] == 3
    assert runs_for(mctx, sid) == []
    assert {o.status for o in occurrences(mctx, sid)} == {"missed"}


def test_crash_between_occurrence_and_run_is_recovered_once(
    mctx: AppContext, clock: ManualClock
) -> None:
    sid = make_schedule(mctx)
    service = ScheduleService(mctx)
    when = START + timedelta(minutes=30)
    with mctx.session() as session:
        occ = service.insert_occurrence(session, sid, when, OccurrenceStatus.PENDING)
        assert occ is not None
    engine = SchedulerEngine(mctx, owner_id="a")
    assert engine.tick()["recovered"] == 1
    engine.tick()
    runs = runs_for(mctx, sid)
    assert len(runs) == 1
    assert runs[0].idempotency_key == f"schedule:{sid}:{when.isoformat()}"


# --------------------------------------------------------------------------- concurrency


def test_concurrency_queue_waits_then_starts(mctx: AppContext, clock: ManualClock) -> None:
    sid = make_schedule(mctx, concurrency_policy="queue", max_queued=1)
    engine = SchedulerEngine(mctx, owner_id="a")
    clock.advance(3600)
    engine.tick()
    first = runs_for(mctx, sid)[0]
    clock.advance(3600)
    engine.tick()  # first still queued -> this occurrence waits
    clock.advance(3600)
    engine.tick()  # queue is full (max 1) -> skipped
    statuses = [o.status for o in occurrences(mctx, sid)]
    assert statuses == ["created", "queued", "skipped"]
    finish(mctx, first.id)
    engine.tick()
    runs = runs_for(mctx, sid)
    assert len(runs) == 2
    assert [o.status for o in occurrences(mctx, sid)] == ["created", "created", "skipped"]


def test_concurrency_skip(mctx: AppContext, clock: ManualClock) -> None:
    sid = make_schedule(mctx, concurrency_policy="skip")
    engine = SchedulerEngine(mctx, owner_id="a")
    clock.advance(3600)
    engine.tick()
    clock.advance(3600)
    engine.tick()
    assert [o.status for o in occurrences(mctx, sid)] == ["created", "skipped"]
    assert len(runs_for(mctx, sid)) == 1


def test_cancel_and_replace(mctx: AppContext, clock: ManualClock) -> None:
    sid = make_schedule(mctx, concurrency_policy="cancel_and_replace")
    engine = SchedulerEngine(mctx, owner_id="a")
    clock.advance(3600)
    engine.tick()
    clock.advance(3600)
    engine.tick()
    runs = runs_for(mctx, sid)
    assert [r.status for r in runs] == ["cancelled", "queued"]
    assert "Replaced" in (runs[0].failure_message or "")
    assert [o.status for o in occurrences(mctx, sid)] == ["replaced", "created"]


# --------------------------------------------------------------------------- run now / pause


def test_run_now_respects_policy_unless_forced(mctx: AppContext) -> None:
    sid = make_schedule(mctx, concurrency_policy="skip")
    service = ScheduleService(mctx)
    first = service.run_now(sid)
    assert first.action == "start" and first.run_id
    second = service.run_now(sid)
    assert second.action == "skip" and second.run_id is None
    forced = service.run_now(sid, force=True)
    assert forced.run_id and forced.run_id != first.run_id
    assert len(runs_for(mctx, sid)) == 2
    history = service.history(sid)
    assert history.total == 3


def test_pause_and_resume_do_not_backfill(mctx: AppContext, clock: ManualClock) -> None:
    sid = make_schedule(mctx)
    service = ScheduleService(mctx)
    engine = SchedulerEngine(mctx, owner_id="a")
    service.set_enabled(sid, False)
    clock.advance(5 * 3600)
    engine.tick()
    assert runs_for(mctx, sid) == []
    resumed = service.set_enabled(sid, True)
    assert resumed.next_run_at == clock.now() + timedelta(hours=1) - timedelta(
        seconds=(clock.now() - START).total_seconds() % 3600
    )
    engine.tick()
    assert runs_for(mctx, sid) == []


def test_run_creation_failure_is_recorded(mctx: AppContext, clock: ManualClock) -> None:
    sid = make_schedule(mctx)
    with mctx.session() as session:
        from agenttape.infrastructure.db.models import AgentProfile, Schedule

        s = session.get(Schedule, sid)
        assert s is not None
        profile = session.get(AgentProfile, s.agent_profile_id)
        assert profile is not None
        profile.archived = True
    clock.advance(3600)
    SchedulerEngine(mctx, owner_id="a").tick()
    occ = occurrences(mctx, sid)
    assert occ[0].status == "failed"
    assert "archived" in (occ[0].reason or "")


def test_update_recomputes_next_run(mctx: AppContext) -> None:
    sid = make_schedule(mctx)
    out = ScheduleService(mctx).update(
        sid, ScheduleUpdate(kind="cron", cron_expression="15 * * * *", timezone="UTC")
    )
    assert out.next_run_at == START + timedelta(minutes=15)


def test_invalid_inputs_are_rejected_at_save(mctx: AppContext) -> None:
    with pytest.raises(ValidationError):
        make_schedule(mctx, inputs={"not_a_variable": 1})


def test_scheduler_heartbeat_and_status(mctx: AppContext) -> None:
    service = ScheduleService(mctx)
    assert service.status().state == "unknown"
    engine = SchedulerEngine(mctx, owner_id="a")
    engine.tick()
    assert service.status().state == "running"
    engine.stop()
    status = service.status()
    assert status.state == "stopped"
    with mctx.session() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 0


def test_recovery_links_the_existing_run_even_if_the_schedule_changed(
    mctx: AppContext, clock: ManualClock
) -> None:
    """A run was created for an occurrence, then the process died before linking it and
    the schedule's inputs were edited. Recovery links the existing run: no second run and
    no idempotency conflict."""
    sid = make_schedule(mctx)
    service = ScheduleService(mctx)
    when = START + timedelta(minutes=30)
    with mctx.session() as session:
        occ = service.insert_occurrence(session, sid, when, OccurrenceStatus.PENDING)
        assert occ is not None
        occ_id = occ.id
    first = service.create_run_for(occ_id)
    assert first is not None
    with mctx.session() as session:
        o = session.get(ScheduleOccurrence, occ_id)
        assert o is not None
        o.status = OccurrenceStatus.PENDING.value  # simulate the crash before linking
        o.run_id = None
    service.update(sid, ScheduleUpdate(inputs={"thing": "edited"}))
    SchedulerEngine(mctx, owner_id="a").tick()
    runs = runs_for(mctx, sid)
    assert [r.id for r in runs] == [first]
    assert occurrences(mctx, sid)[0].run_id == first
    assert occurrences(mctx, sid)[0].status == "created"


# --------------------------------------------------------------------------- DST at the engine


def _walk(ctx: AppContext, clock: ManualClock, sid: str, steps: int) -> list[str]:
    """Tick the real engine at each due time (plus 5 s), finishing each run so the
    concurrency policy never interferes. Returns the created occurrences' local times."""
    engine = SchedulerEngine(ctx, owner_id="dst", lease_seconds=10**7)
    service = ScheduleService(ctx)
    for _ in range(steps):
        nxt = service.get(sid).next_run_at
        assert nxt is not None
        clock.set(nxt + timedelta(seconds=5))
        engine.tick()
        for run in runs_for(ctx, sid):
            if run.status == "queued":
                finish(ctx, run.id)
    ny = ZoneInfo("America/New_York")
    occ = occurrences(ctx, sid)
    assert all(o.status == "created" for o in occ), [o.status for o in occ]
    assert len({o.run_id for o in occ}) == len(occ)
    return [o.occurrence_at.astimezone(ny).isoformat() for o in occ]


def test_engine_cron_across_spring_forward(mctx: AppContext, clock: ManualClock) -> None:
    clock.set(datetime(2026, 3, 6, 12, 0, tzinfo=UTC))
    sid = make_schedule(
        mctx, kind="cron", cron_expression="30 2 * * *", timezone="America/New_York"
    )
    assert _walk(mctx, clock, sid, 3) == [
        "2026-03-07T02:30:00-05:00",
        "2026-03-08T03:00:00-04:00",  # 02:30 does not exist: first instant after the gap
        "2026-03-09T02:30:00-04:00",
    ]


def test_engine_cron_across_fall_back(mctx: AppContext, clock: ManualClock) -> None:
    clock.set(datetime(2026, 11, 1, 3, 50, tzinfo=UTC))  # 23:50 EDT, 31 Oct
    sid = make_schedule(
        mctx, kind="cron", cron_expression="*/30 * * * *", timezone="America/New_York"
    )
    assert _walk(mctx, clock, sid, 6) == [
        "2026-11-01T00:00:00-04:00",
        "2026-11-01T00:30:00-04:00",
        "2026-11-01T01:00:00-04:00",
        "2026-11-01T01:30:00-04:00",
        # 01:00 and 01:30 repeat in EST; each wall time fires once, so the next is 02:00.
        "2026-11-01T02:00:00-05:00",
        "2026-11-01T02:30:00-05:00",
    ]


def test_engine_interval_across_fall_back(mctx: AppContext, clock: ManualClock) -> None:
    clock.set(datetime(2026, 11, 1, 4, 30, tzinfo=UTC))  # 00:30 EDT
    sid = make_schedule(
        mctx,
        kind="interval",
        interval_seconds=1800,
        timezone="America/New_York",
        start_at=datetime(2026, 11, 1, 4, 30, tzinfo=UTC),
    )
    # Occurrences are strictly after the schedule was saved (at the 00:30 start).
    assert _walk(mctx, clock, sid, 5) == [
        "2026-11-01T01:00:00-04:00",
        "2026-11-01T01:30:00-04:00",
        "2026-11-01T01:00:00-05:00",  # elapsed time: the repeated hour is visited again
        "2026-11-01T01:30:00-05:00",
        "2026-11-01T02:00:00-05:00",
    ]
