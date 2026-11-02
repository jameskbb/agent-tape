"""Worker extension hooks (registry-style, so features add behaviour without editing the
executor).

* :func:`register_workdir_preparer` - choose/prepare the directory an attempt runs in
  (Git worktree isolation installs one here). The last registered preparer wins;
  the default runs in the project repository (or a scratch dir when there is none).
* :func:`register_after_attempt` - called after the adapter returns and before the
  attempt is finalized (Git isolation captures file changes and diffs; budgets can veto).
* :func:`register_on_run_terminal` - called after a run reaches a terminal status or
  pauses for approval (the workflow engine re-queues parents here). Every run end
  (launched, workflow, observed, or cancelled) fires them through :func:`fire_run_terminal`
  (or :func:`fire_run_terminal_sync` from synchronous code); there is no second loop.
* :func:`register_attempt_monitor` - an async task started next to the executor's
  watchdogs for the whole attempt and cancelled when it ends (budgets watch usage, file
  changes, and artifacts here and stop the attempt through ``exec_ctx.cancel``).

``register_after_attempt`` takes an optional ``order`` (default 50, lower runs first) so a
hook that must see other hooks' results (budget checks after diff capture) can run late.

Hooks run inside the worker process. They must be idempotent and must not raise for
expected conditions (return data or emit events instead).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agenttape.plugins import import_submodules

if TYPE_CHECKING:
    from agenttape.adapters.base import AdapterResult, EventEmitter, ExecutionContext
    from agenttape.application.context import AppContext

log = logging.getLogger(__name__)


@dataclass
class WorkdirRequest:
    run_id: str
    attempt_number: int
    snapshot: dict[str, Any]
    project_path: Path | None
    data_dir: Path


@dataclass
class PreparedWorkdir:
    path: Path | None
    isolated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


WorkdirPreparer = Callable[["AppContext", WorkdirRequest], Awaitable[PreparedWorkdir]]
AfterAttemptHook = Callable[
    ["AppContext", "ExecutionContext", "AdapterResult"], Awaitable["AdapterResult"]
]
RunTerminalHook = Callable[["AppContext", str, str], Awaitable[None]]
"""(ctx, run_id, status)"""
AttemptMonitor = Callable[
    ["AppContext", "ExecutionContext", "EventEmitter"], Coroutine[Any, Any, None]
]
"""(ctx, exec_ctx, system_events): runs for the attempt's lifetime; emit on the system
emitter (source ``agenttape``) so monitor events never count as agent activity."""


async def default_workdir(_ctx: AppContext, req: WorkdirRequest) -> PreparedWorkdir:
    if req.project_path is not None and req.project_path.is_dir():
        return PreparedWorkdir(path=req.project_path)
    scratch = req.data_dir / "workspaces" / "runs" / req.run_id
    scratch.mkdir(parents=True, exist_ok=True)
    return PreparedWorkdir(path=scratch, metadata={"scratch": True})


_workdir_preparer: WorkdirPreparer = default_workdir
_after_attempt: list[tuple[int, AfterAttemptHook]] = []
_on_terminal: list[RunTerminalHook] = []
_attempt_monitors: list[AttemptMonitor] = []


def register_workdir_preparer(fn: WorkdirPreparer) -> None:
    global _workdir_preparer
    _workdir_preparer = fn


def workdir_preparer() -> WorkdirPreparer:
    return _workdir_preparer


def register_after_attempt(fn: AfterAttemptHook, *, order: int = 50) -> None:
    if all(existing is not fn for _o, existing in _after_attempt):
        _after_attempt.append((order, fn))


def after_attempt_hooks() -> list[AfterAttemptHook]:
    # Stable sort: equal orders keep registration order (the behaviour before orders existed).
    return [fn for _order, fn in sorted(_after_attempt, key=lambda pair: pair[0])]


def register_on_run_terminal(fn: RunTerminalHook) -> None:
    if fn not in _on_terminal:
        _on_terminal.append(fn)


def run_terminal_hooks() -> list[RunTerminalHook]:
    return list(_on_terminal)


async def fire_run_terminal(ctx: AppContext, run_id: str, status: str) -> None:
    """Run every registered run-terminal hook for ``run_id``: the one firing path for a
    launched run, a workflow run, an observed session run, and a cancel. A failing hook is
    logged and never stops the others. Imports the ``worker.executors`` submodules first so
    the hooks are registered even in a process (the API process handling a hooks POST, or a
    CLI cancel) that never loaded the worker executor."""
    import_submodules("agenttape.worker.executors")
    for hook in run_terminal_hooks():
        try:
            await hook(ctx, run_id, status)
        except Exception:
            log.exception(
                "run-terminal hook %s failed for run %s", getattr(hook, "__name__", "?"), run_id
            )


def fire_run_terminal_sync(ctx: AppContext, run_id: str, status: str) -> None:
    """:func:`fire_run_terminal` for a synchronous call site (``RunService.cancel_run``).
    Safe whether or not the caller is already inside a running event loop (the workflow
    engine cancels children from inside one; the CLI and the API route do not)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(fire_run_terminal(ctx, run_id, status))
        return
    # Already on a running loop: block this thread on a fresh loop in a worker thread
    # rather than raise ("asyncio.run() cannot be called from a running event loop").
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, fire_run_terminal(ctx, run_id, status)).result()


def register_attempt_monitor(fn: AttemptMonitor) -> None:
    if fn not in _attempt_monitors:
        _attempt_monitors.append(fn)


def attempt_monitors() -> list[AttemptMonitor]:
    return list(_attempt_monitors)
