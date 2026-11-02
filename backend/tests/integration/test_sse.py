from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
from fastapi.testclient import TestClient

from agenttape.api.app import create_app
from agenttape.application.context import AppContext
from agenttape.application.services.runs import RunService
from agenttape.worker.worker import Worker
from tests.conftest import demo_profile, drive, start_run


def parse_sse(lines: list[str]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in lines:
        if not line:
            if current:
                messages.append(current)
                current = {}
            continue
        if line.startswith(":"):
            continue
        key, _, value = line.partition(":")
        current[key] = value.lstrip(" ")
    if current:
        messages.append(current)
    return messages


def read_stream(
    client: TestClient, url: str, headers: dict[str, str] | None = None
) -> list[dict[str, str]]:
    with client.stream("GET", url, headers=headers or {}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return parse_sse(list(response.iter_lines()))


def test_stream_replays_and_ends_for_finished_run(client: TestClient, ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx))
    total = drive(ctx, run_id).event_count
    messages = read_stream(client, f"/api/v1/runs/{run_id}/events/stream")
    events = [m for m in messages if m.get("event") == "run_event"]
    assert [int(m["id"]) for m in events] == list(range(1, total + 1))
    first = json.loads(events[0]["data"])
    assert first["sequence"] == 1 and first["type"] == "lifecycle"
    end = messages[-1]
    assert end["event"] == "end" and json.loads(end["data"])["status"] == "succeeded"


def test_resume_with_last_event_id_and_after_sequence(client: TestClient, ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx))
    total = drive(ctx, run_id).event_count
    resumed = read_stream(client, f"/api/v1/runs/{run_id}/events/stream", {"Last-Event-ID": "10"})
    ids = [int(m["id"]) for m in resumed if m.get("event") == "run_event"]
    assert ids == list(range(11, total + 1))
    after = read_stream(client, f"/api/v1/runs/{run_id}/events/stream?after_sequence={total - 2}")
    assert [int(m["id"]) for m in after if m.get("event") == "run_event"] == [total - 1, total]


def test_stream_404_for_unknown_run(client: TestClient) -> None:
    assert client.get("/api/v1/runs/nope/events/stream").status_code == 404


def test_oversized_events_are_stubbed(tmp_path: object, ctx: AppContext) -> None:
    from agenttape.api.routers.runs import _sse_data
    from agenttape.application.schemas.runs import EventOut

    run_id = start_run(ctx, demo_profile(ctx))
    event = RunService(ctx).list_events(run_id).items[0]
    big = EventOut.model_validate({**event.model_dump(), "payload": {"text": "x" * 5000}})
    data = json.loads(_sse_data(big, cap=1000))
    assert data["payload"] == {"truncated_for_stream": True} and data["sequence"] == event.sequence


def test_live_events_arrive_while_running(ctx: AppContext) -> None:
    """A real server + worker: events stream in while the run executes, then the stream ends."""
    import uvicorn

    app = create_app(ctx)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    pid = demo_profile(ctx, step_delay_ms=150)
    run_id = start_run(ctx, pid)
    received: list[tuple[float, dict[str, str]]] = []
    connected = threading.Event()

    def listen() -> None:
        with httpx.stream(
            "GET", f"http://127.0.0.1:{port}/api/v1/runs/{run_id}/events/stream", timeout=30
        ) as r:
            connected.set()
            buf: list[str] = []
            for line in r.iter_lines():
                buf.append(line)
                if line == "":
                    for m in parse_sse(buf):
                        received.append((time.monotonic(), m))
                    buf = []

    listener = threading.Thread(target=listen)
    listener.start()

    finished: list[float] = []

    async def work() -> None:
        stop = asyncio.Event()
        worker = Worker(ctx, poll_interval=0.05)
        task = asyncio.create_task(worker.run(stop))
        while RunService(ctx).get_run(run_id).status not in ("succeeded", "failed"):
            await asyncio.sleep(0.05)
        finished.append(time.monotonic())
        stop.set()
        await task

    # The stream is open before the worker starts, so every event the worker writes can
    # only reach the listener live; how fast that happens depends on machine load.
    assert connected.wait(timeout=30)
    started = time.monotonic()
    asyncio.run(work())
    listener.join(timeout=30)
    server.should_exit = True
    thread.join(timeout=10)
    events = [(t, m) for t, m in received if m.get("event") == "run_event"]
    assert received[-1][1]["event"] == "end"
    assert len(events) == RunService(ctx).get_detail(run_id).event_count
    # Events were delivered progressively while the run executed, not all at the end.
    live = [t for t, _ in events if started <= t < finished[0]]
    assert len(live) >= 2
    assert events[-1][0] - events[0][0] > 0.5
