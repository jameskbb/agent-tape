"""Helpers shared by the integration tests (not collected: no ``test_`` prefix)."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import ProjectCreate
from agenttape.application.services.findings import FindingsService
from agenttape.application.services.projects import ProjectService
from agenttape.bootstrap import build_context
from agenttape.domain.errors import ConflictError
from agenttape.findings.normalize import to_cat_n

GREETER_TEXT = "def greet():\n    return 'hi'\n"
OTHER_TEXT = "X = 1\n"
DEFAULT_REPO_FILES: Mapping[str, str] = {"src/greeter.py": GREETER_TEXT, "src/other.py": OTHER_TEXT}


def git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with a hermetic environment; returns stdout."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(repo),
        },
    )
    return result.stdout


def make_repo_project(
    ctx: AppContext,
    *,
    name: str = "findings-repo",
    project_name: str = "FindingsRepo",
    files: Mapping[str, str] | None = None,
) -> str:
    """A committed git repo ``name`` under the first allowed root, registered as a project;
    returns the project id."""
    repo = Path(ctx.settings.allowed_roots[0]) / name
    for rel, text in (DEFAULT_REPO_FILES if files is None else files).items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "init")
    return ProjectService(ctx).create(ProjectCreate(name=project_name, repo_path=str(repo))).id


def run_findings(ctx: AppContext, run_id: str) -> list[dict[str, Any]]:
    """A run's findings as the findings read model returns them, in their own session."""
    with ctx.session() as session:
        return FindingsService.findings_for_run(session, run_id)


def race_two_contexts(
    ctx: AppContext, action: Callable[[AppContext], Any]
) -> list[tuple[str, Any]]:
    """Run ``action`` twice at once, in two threads, one per context (``ctx`` and a second
    one built from its settings), and return both outcomes: ``("ok", result)``,
    ``("conflict", exc)`` for a ConflictError, or ``("error", exc)`` for anything else.

    The caller supplies the determinism: a ``threading.Barrier`` placed at the seam between
    the check and the write, so both calls have passed the check before either writes.
    """
    ctx_b = build_context(ctx.settings)
    outcomes: list[tuple[str, Any]] = []

    def run(c: AppContext) -> None:
        try:
            outcomes.append(("ok", action(c)))
        except ConflictError as exc:
            outcomes.append(("conflict", exc))
        except BaseException as exc:  # a crashed caller must fail the test
            outcomes.append(("error", exc))

    try:
        threads = [threading.Thread(target=run, args=(c,)) for c in (ctx, ctx_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        ctx_b.close()
    return outcomes


# ------------------------------------------------------------------ findings event records


def read_records(
    tool_use_id: str,
    rel_path: str,
    text: str,
    *,
    offset: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """A Claude Code ``Read`` TOOL_CALL/TOOL_RESULT pair returning ``text`` in the ``cat
    -n`` layout; with ``offset``/``limit``, only that window, numbered from ``offset``."""
    lines = text.splitlines()
    tool_input: dict[str, Any] = {"file_path": rel_path}
    start = 1
    if offset is not None and limit is not None:
        tool_input.update(offset=offset, limit=limit)
        lines = lines[offset - 1 : offset - 1 + limit]
        start = offset
    return [
        {
            "type": "tool_call",
            "payload": {"tool": "Read", "input": tool_input, "tool_use_id": tool_use_id},
        },
        {
            "type": "tool_result",
            "payload": {
                "tool_use_id": tool_use_id,
                "output": to_cat_n(lines, start=start),
                "is_error": False,
            },
        },
    ]


def bash_tool_records(
    tool_use_id: str, command: str, *, output: str, is_error: bool = False
) -> list[dict[str, Any]]:
    """A Claude Code Bash TOOL_CALL/TOOL_RESULT pair for ``command`` (a replayable shape
    beside a ``command`` event)."""
    return [
        {
            "type": "tool_call",
            "payload": {"tool": "Bash", "input": {"command": command}, "tool_use_id": tool_use_id},
        },
        {
            "type": "tool_result",
            "payload": {"tool_use_id": tool_use_id, "output": output, "is_error": is_error},
        },
    ]


def command_record(
    tool_use_id: str,
    argv: list[str],
    output: str = "",
    *,
    exit_code: int = 0,
    cwd: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "argv": argv,
        "exit_code": exit_code,
        "output": output,
        "tool_use_id": tool_use_id,
    }
    if cwd is not None:
        payload["cwd"] = cwd
    return {"type": "command", "payload": payload}


def file_write_record(path: str, operation: str, num_bytes: int) -> dict[str, Any]:
    return {
        "type": "file_write",
        "payload": {"path": path, "operation": operation, "bytes": num_bytes},
    }


def finding_record(
    finding_id: str,
    path: str,
    line_start: int,
    line_end: int,
    quote: str,
    *,
    kind: str = "claim",
    text: str | None = None,
) -> dict[str, Any]:
    """A finding with one file citation."""
    return link_finding(
        finding_id,
        [{"path": path, "line_start": line_start, "line_end": line_end, "quote": quote}],
        kind=kind,
        text=f"{path} says something" if text is None else text,
    )


def event_finding_record(finding_id: str, event_ref: str, quote: str) -> dict[str, Any]:
    """A finding with one event citation."""
    return link_finding(
        finding_id, [{"event_ref": event_ref, "quote": quote}], text=f"{event_ref} says something"
    )


def link_finding(
    finding_id: str, links: list[dict[str, Any]], *, kind: str = "claim", text: str = "claim"
) -> dict[str, Any]:
    return {
        "type": "finding",
        "payload": {"id": finding_id, "kind": kind, "text": text, "links": links},
    }


# ------------------------------------------------------------------ findings read model


def findings_of(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    """``GET /runs/{run_id}/context/findings``."""
    resp = client.get(f"/api/v1/runs/{run_id}/context/findings")
    assert resp.status_code == 200, resp.text
    result: list[dict[str, Any]] = resp.json()
    return result


def citation_of(client: TestClient, run_id: str, key: str) -> dict[str, Any]:
    """The single citation of finding ``key`` on ``run_id``."""
    [entry] = [f for f in findings_of(client, run_id) if f["key"] == key]
    [citation] = entry["citations"]
    result: dict[str, Any] = citation
    return result
