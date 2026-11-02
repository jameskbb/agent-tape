from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agenttape.application.context import AppContext
from agenttape.bootstrap import build_context
from agenttape.config import Settings
from agenttape.infrastructure.clock import ManualClock
from agenttape.infrastructure.db.models import QueueItem
from agenttape.infrastructure.leases import DbLeaseManager


def enqueue(ctx: AppContext, key: str, priority: int = 0) -> str:
    with ctx.session() as session:
        return ctx.queue.enqueue(
            session, dedupe_key=key, run_id=None, priority=priority, kind="test"
        )


def test_priority_then_fifo(ctx: AppContext) -> None:
    a = enqueue(ctx, "a")
    b = enqueue(ctx, "b", priority=5)
    c = enqueue(ctx, "c")
    order = [ctx.queue.claim("w", 30, kinds=("test",)) for _ in range(3)]
    assert [o.id for o in order if o] == [b, a, c]
    assert ctx.queue.claim("w", 30, kinds=("test",)) is None


def test_dedupe_key_is_unique(ctx: AppContext) -> None:
    enqueue(ctx, "same")
    with pytest.raises(IntegrityError):
        enqueue(ctx, "same")


def test_available_at_delays_claims(ctx: AppContext) -> None:
    with ctx.session() as session:
        ctx.queue.enqueue(
            session,
            dedupe_key="later",
            run_id=None,
            kind="test",
            available_at=ctx.clock.now() + timedelta(hours=1),
        )
    assert ctx.queue.claim("w", 30, kinds=("test",)) is None


def test_renew_and_complete_require_owner(ctx: AppContext) -> None:
    enqueue(ctx, "x")
    item = ctx.queue.claim("owner", 30, kinds=("test",))
    assert item is not None
    assert not ctx.queue.renew(item.id, "intruder", 30)
    assert ctx.queue.renew(item.id, "owner", 30)
    assert not ctx.queue.complete(item.id, "intruder")
    assert ctx.queue.complete(item.id, "owner")
    assert not ctx.queue.complete(item.id, "owner")


def test_two_workers_never_claim_the_same_item(ctx: AppContext) -> None:
    total = 60
    for i in range(total):
        enqueue(ctx, f"item-{i}", priority=i % 3)
    claimed: dict[str, list[str]] = {}
    lock = threading.Lock()
    barrier = threading.Barrier(6)

    def work(name: str) -> None:
        barrier.wait()
        while True:
            item = ctx.queue.claim(name, 30, kinds=("test",))
            if item is None:
                return
            with lock:
                claimed.setdefault(item.id, []).append(name)

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(claimed) == total
    duplicates = {k: v for k, v in claimed.items() if len(v) > 1}
    assert duplicates == {}
    with ctx.session() as session:
        owners = session.scalars(select(QueueItem.lease_owner)).all()
    assert all(owners)


def test_two_processes_style_contexts_do_not_double_claim(settings: Settings) -> None:
    """Separate engines (as two worker processes would have) racing on one database."""
    a = build_context(settings, migrate=True)
    b = build_context(settings)
    try:
        for i in range(40):
            enqueue(a, f"p-{i}")
        results: dict[str, list[str]] = {}
        lock = threading.Lock()

        def work(ctx: AppContext, name: str) -> None:
            while (item := ctx.queue.claim(name, 30, kinds=("test",))) is not None:
                with lock:
                    results.setdefault(item.id, []).append(name)

        threads = [
            threading.Thread(target=work, args=(c, n))
            for c, n in ((a, "A"), (b, "B"), (a, "A2"), (b, "B2"))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert len(results) == 40
        assert all(len(v) == 1 for v in results.values())
    finally:
        a.close()
        b.close()


def test_queue_position(ctx: AppContext) -> None:
    from tests.conftest import demo_profile, start_run

    pid = demo_profile(ctx)
    first = start_run(ctx, pid)
    second = start_run(ctx, pid)
    urgent = start_run(ctx, pid, priority=10)
    with ctx.session() as session:
        assert ctx.queue.position(session, urgent) == 1
        assert ctx.queue.position(session, first) == 2
        assert ctx.queue.position(session, second) == 3


def test_named_lease_single_owner(settings: Settings) -> None:
    clock = ManualClock()
    ctx = build_context(settings, clock=clock, migrate=True)
    try:
        leases = DbLeaseManager(ctx.session_factory, clock)
        assert leases.acquire("scheduler", "a", 10)
        assert not leases.acquire("scheduler", "b", 10)
        assert leases.acquire("scheduler", "a", 10)  # renew
        assert leases.holder("scheduler") == ("a", clock.now() + timedelta(seconds=10))
        clock.advance(11)
        assert leases.holder("scheduler") is None
        assert leases.acquire("scheduler", "b", 10)  # expired -> takeover
        leases.release("scheduler", "a")  # not the owner: no effect
        assert leases.holder("scheduler") is not None
        leases.release("scheduler", "b")
        assert leases.holder("scheduler") is None
    finally:
        ctx.close()
