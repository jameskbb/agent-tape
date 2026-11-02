"""The built-in Demo Agent (``demo``).

Deterministic, credential-free, and honest: it runs real commands (``git clone``,
``git log``, ``git diff``, a Python syntax check) in an AgentTape-owned clone of the
fixture repository, makes a small real code change, records the actual diff, and saves a
report artifact. Its "model" messages and token counts are scripted; usage is always
marked ``simulated: true`` so the UI never presents it as real usage.

Scenarios (profile config ``scenario``):

* ``success``          - completes the change and succeeds;
* ``fail_then_retry``  - attempt 1 fails with a retryable ``adapter_error``, attempt 2 succeeds;
* ``needs_approval``   - plans, pauses for approval, then (after approval) applies the change
                         from its checkpoint without redoing the planning steps;
* ``timeout``          - starts a long-running process that the wall-clock timeout kills;
* ``fail``             - fails permanently with ``validation_failed`` (not retried);
* ``full_evidence``    - runs the evidence demo agent (``python -m agenttape.demo_agent``)
                         as a real subprocess in the run's workdir, streams its JSON Lines
                         telemetry through :meth:`DemoAdapter.normalize`, pauses for
                         approval of a protected-path write (exit 3), then resumes it.
                         Optional ``variant`` (``a``/``b``) changes its context sources.
* ``finding_review``   - scripted, no-model reviewer for Challenge findings: re-reads each
                         cited file in its own clone and records upheld / refuted /
                         inconclusive; used by tests.

Optional config: ``step_delay_ms`` (pacing between steps; default from settings).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from agenttape.adapters.base import (
    AdapterResult,
    AgentAdapter,
    ApprovalSpec,
    DiscoveryResult,
    ExecutionContext,
    FileChangeDraft,
    ProfileIssue,
)
from agenttape.adapters.builtin._demo_repo import (
    CHANGELOG_ENTRY,
    GREETER_V2,
    TEST_FILE,
    ensure_fixture_repo,
    fixture_path,
    git_env,
)
from agenttape.adapters.process import CommandSpec, ProcessResult, run_process
from agenttape.adapters.registry import register_adapter
from agenttape.domain.enums import (
    AdapterAvailability,
    EventType,
    FailureCategory,
    RiskLevel,
    Severity,
)
from agenttape.domain.events import EventDraft
from agenttape.git.snapshot import unified_diff, working_tree_changes
from agenttape.infrastructure.db.base import new_id

SCENARIOS = (
    "success",
    "fail_then_retry",
    "needs_approval",
    "timeout",
    "fail",
    "full_evidence",
    "finding_review",
)
VARIANTS = ("a", "b")
#: Evidence demo agent exit code for "needs approval" (agenttape.demo_agent).
_AGENT_NEEDS_APPROVAL = 3
VERSION = "1.0.0"


class _Stopped(Exception):
    """Raised internally when the cancellation token fires between steps."""


@register_adapter
class DemoAdapter(AgentAdapter):
    name = "demo"
    display_name = "Demo Agent"
    description = (
        "Built-in deterministic agent. Needs no credentials; makes a real, safe change in an "
        "AgentTape-owned fixture repository. Token usage is simulated and labelled as such."
    )
    capabilities = frozenset(
        {
            "structured_events",
            "tool_calls",
            "file_changes",
            "artifacts",
            "simulated_usage",
            "approvals",
        }
    )
    #: The evidence demo scenario emits scripted FINDING events (no MCP server involved).
    findings_capture = "scripted"

    # ------------------------------------------------------------------ discovery / validation

    def discover(self) -> DiscoveryResult:
        git = shutil.which("git")
        if git is None:
            return DiscoveryResult(
                adapter=self.name,
                status=AdapterAvailability.MISCONFIGURED,
                version=VERSION,
                message="git was not found on PATH; the Demo Agent needs git for its fixture repository",
            )
        return DiscoveryResult(
            adapter=self.name,
            status=AdapterAvailability.READY,
            version=VERSION,
            executable="built-in",
            message="Built in; no credentials required",
            details={"scenarios": list(SCENARIOS), "git": git},
        )

    def validate_profile(self, config: dict[str, Any]) -> list[ProfileIssue]:
        issues: list[ProfileIssue] = []
        scenario = config.get("scenario", "success")
        if scenario not in SCENARIOS:
            issues.append(ProfileIssue("scenario", f"must be one of: {', '.join(SCENARIOS)}"))
        variant = config.get("variant")
        if variant is not None and variant not in VARIANTS:
            issues.append(ProfileIssue("variant", f"must be one of: {', '.join(VARIANTS)}"))
        delay = config.get("step_delay_ms")
        if delay is not None and (not isinstance(delay, int) or not 0 <= delay <= 10_000):
            issues.append(ProfileIssue("step_delay_ms", "must be an integer between 0 and 10000"))
        if scenario == "timeout":
            limit = config.get("timeout_seconds")
            if limit is None or limit > 120:
                issues.append(
                    ProfileIssue(
                        "timeout_seconds",
                        "set a short timeout (e.g. 5) so the timeout scenario finishes quickly",
                        severity="warning",
                    )
                )
        return issues

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "scenario": {"type": "string", "enum": list(SCENARIOS), "default": "success"},
                "step_delay_ms": {"type": "integer", "minimum": 0, "maximum": 10000},
                "variant": {"type": "string", "enum": list(VARIANTS), "default": "a"},
            },
        }

    def default_config(self) -> dict[str, Any]:
        return {"scenario": "success"}

    # ------------------------------------------------------------------ normalization

    def normalize(self, raw: dict[str, Any]) -> list[EventDraft]:
        """Map one demo stream record (shaped like agent CLI stream-json) to events."""
        kind = raw.get("type")
        src = self.name
        if kind == "assistant":
            text = str(raw.get("text", ""))
            return [
                EventDraft(
                    EventType.AGENT_MESSAGE,
                    text[:300],
                    {"role": "assistant", "text": text},
                    source=src,
                )
            ]
        if kind == "reasoning_summary":
            text = str(raw.get("text", ""))
            return [
                EventDraft(
                    EventType.REASONING_SUMMARY,
                    text[:300],
                    {"text": text, "provided_by": "adapter"},
                    source=src,
                )
            ]
        if kind == "tool_use":
            name = str(raw.get("name", "tool"))
            args = raw.get("input") or {}
            target = args.get("path") or args.get("command") or ""
            return [
                EventDraft(
                    EventType.TOOL_CALL,
                    f"{name}({target})" if target else name,
                    {"tool": name, "input": args, "tool_use_id": raw.get("id")},
                    source=src,
                )
            ]
        if kind == "tool_result":
            output = str(raw.get("output", ""))
            is_error = bool(raw.get("is_error"))
            return [
                EventDraft(
                    EventType.TOOL_RESULT,
                    ("Tool error: " if is_error else "Tool result: ") + output.splitlines()[0][:200]
                    if output
                    else "Tool result (empty)",
                    {"tool_use_id": raw.get("tool_use_id"), "output": output, "is_error": is_error},
                    severity=Severity.ERROR if is_error else Severity.INFO,
                    source=src,
                )
            ]
        if kind == "file_read":
            return [
                EventDraft(
                    EventType.FILE_READ,
                    f"Read {raw.get('path')}",
                    {"path": raw.get("path"), "bytes": raw.get("bytes")},
                    source=src,
                )
            ]
        if kind == "file_write":
            return [
                EventDraft(
                    EventType.FILE_WRITE,
                    f"Wrote {raw.get('path')}",
                    {
                        "path": raw.get("path"),
                        "bytes": raw.get("bytes"),
                        "operation": raw.get("operation", "write"),
                    },
                    source=src,
                )
            ]
        if kind == "file_search":
            paths = [str(p) for p in raw.get("paths") or []][:200]
            return [
                EventDraft(
                    EventType.FILE_READ,
                    f"Searched for '{str(raw.get('query', ''))[:80]}' ({len(paths)} matches)",
                    {"operation": "search", "query": raw.get("query"), "paths": paths},
                    source=src,
                )
            ]
        if kind == "file_rename":
            return [
                EventDraft(
                    EventType.FILE_WRITE,
                    f"Renamed {raw.get('old_path')} to {raw.get('path')}",
                    {
                        "path": raw.get("path"),
                        "old_path": raw.get("old_path"),
                        "operation": "rename",
                    },
                    source=src,
                )
            ]
        if kind == "command":
            argv = [str(a) for a in raw.get("argv") or []]
            code = raw.get("exit_code")
            payload: dict[str, Any] = {
                "argv": argv,
                "cwd": raw.get("cwd"),
                "exit_code": code,
                "started_at": raw.get("started_at"),
                "finished_at": raw.get("finished_at"),
                "category": raw.get("category"),
                "label": raw.get("label"),
                "output": raw.get("output"),
                "reported_by": "agent",
            }
            record_id = raw.get("id")
            if record_id is not None:
                payload["tool_use_id"] = str(record_id)
            return [
                EventDraft(
                    EventType.COMMAND,
                    " ".join(argv)[:300] + (f" (exit {code})" if code is not None else ""),
                    payload,
                    severity=Severity.INFO if code in (0, None) else Severity.WARNING,
                    source=src,
                )
            ]
        if kind in ("context", "correction"):
            payload = {k: v for k, v in raw.items() if k != "type"}
            if kind == "correction":
                payload["stage"] = "correction"
                summary = f"User correction: {str(raw.get('text', ''))[:200]}"
            else:
                source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
                assert isinstance(source, dict)
                summary = f"Context {raw.get('stage')}: {source.get('name') or source.get('id')}"
            return [EventDraft(EventType.CONTEXT, summary[:300], payload, source=src)]
        if kind == "finding":
            label = str(raw.get("kind") or "finding").capitalize()
            return [
                EventDraft(
                    EventType.FINDING,
                    f"{label}: {str(raw.get('text', ''))[:240]}",
                    {k: v for k, v in raw.items() if k != "type"},
                    source=src,
                )
            ]
        if kind == "approval_request":
            text = f"Requesting approval: {raw.get('title')}"
            return [
                EventDraft(
                    EventType.AGENT_MESSAGE,
                    text[:300],
                    {
                        "role": "assistant",
                        "text": text,
                        "approval_request": {k: v for k, v in raw.items() if k != "type"},
                    },
                    source=src,
                )
            ]
        if kind == "exit":
            return []  # the process exit code is the lifecycle signal
        if kind == "usage":
            usage = {
                "input_tokens": int(raw.get("input_tokens", 0)),
                "output_tokens": int(raw.get("output_tokens", 0)),
                "simulated": True,
            }
            return [
                EventDraft(
                    EventType.USAGE,
                    f"Simulated usage: {usage['input_tokens']} in / {usage['output_tokens']} out tokens",
                    {**usage, "note": "Demo Agent numbers are simulated, not real model usage."},
                    usage=usage,
                    source=src,
                )
            ]
        return [
            EventDraft(
                EventType.WARNING,
                f"Unrecognized demo record type '{kind}'",
                {"raw": raw},
                severity=Severity.WARNING,
                source=src,
            )
        ]

    # ------------------------------------------------------------------ execution

    async def execute(self, ctx: ExecutionContext) -> AdapterResult:
        scenario = str(ctx.profile.get("scenario", "success"))
        run = demo_run(ctx)
        try:
            return await run.play(scenario)
        except _Stopped:
            return AdapterResult.failure(
                ctx.cancel.reason or FailureCategory.CANCELLED, ctx.cancel.message or "Stopped"
            )


def demo_run(ctx: ExecutionContext) -> _DemoRun:
    """A Demo Agent run for ``ctx`` (also used by the Herdr runtime for its demo kind)."""
    delay_ms = ctx.profile.get("step_delay_ms", ctx.limits.get("demo_step_delay_ms", 450))
    return _DemoRun(DemoAdapter(), ctx, delay_seconds=max(0, int(delay_ms)) / 1000.0)


def evidence_approval(request: dict[str, Any]) -> ApprovalSpec:
    """The approval for the evidence demo agent's ``approval_request`` record (any runtime)."""
    paths = [str(p) for p in request.get("paths") or []]
    return ApprovalSpec(
        title=str(request.get("title") or "Approve a protected-path write")[:300],
        rationale=str(request.get("rationale") or ""),
        type="protected_path",
        risk_level=RiskLevel.HIGH,
        requested_action={"action": "write_protected_paths", "paths": paths},
    )


class _DemoRun:
    def __init__(
        self, adapter: DemoAdapter, ctx: ExecutionContext, *, delay_seconds: float
    ) -> None:
        self.adapter = adapter
        self.ctx = ctx
        self.delay = delay_seconds
        self.tool_events: dict[str, str] = {}
        self.tool_seq = 0

    # -- helpers -------------------------------------------------------------------------

    async def pause(self, factor: float = 1.0) -> None:
        if await self.ctx.cancel.sleep(self.delay * factor):
            raise _Stopped

    async def record(self, raw: dict[str, Any]) -> str | None:
        """Normalize a raw record and emit it; returns the event id of the first draft."""
        drafts = self.adapter.normalize(raw)
        first_id: str | None = None
        for draft in drafts:
            if raw.get("type") == "tool_use":
                draft.id = new_id()
                self.tool_events[str(raw.get("id"))] = draft.id
            if raw.get("type") == "tool_result":
                draft.parent_event_id = self.tool_events.get(str(raw.get("tool_use_id")))
            await self.ctx.events.emit(draft)
            first_id = first_id or draft.id
        return first_id

    async def say(self, text: str) -> None:
        await self.record({"type": "assistant", "text": text})
        await self.pause()

    async def tool(
        self, name: str, args: dict[str, Any], output: str, *, is_error: bool = False
    ) -> None:
        self.tool_seq += 1
        tid = f"toolu_demo_{self.tool_seq:02d}"
        await self.record({"type": "tool_use", "id": tid, "name": name, "input": args})
        await self.pause(0.6)
        await self.record(
            {"type": "tool_result", "tool_use_id": tid, "output": output, "is_error": is_error}
        )

    async def command(
        self, argv: list[str], cwd: Path | None, *, display: str | None = None
    ) -> ProcessResult:
        env = self.ctx.build_env([], git_env())
        result = await run_process(
            CommandSpec(argv=argv, cwd=cwd, env=env, display=display), self.ctx
        )
        if result.stopped_reason is not None:
            raise _Stopped
        return result

    def git(self) -> str:
        exe = shutil.which("git")
        if exe is None:
            raise FileNotFoundError("git not found")
        return exe

    # -- scenario ------------------------------------------------------------------------

    async def play(self, scenario: str) -> AdapterResult:
        ctx = self.ctx
        resume = ctx.resume_state or {}
        if scenario == "full_evidence":
            return await self.full_evidence(resume)
        if scenario == "finding_review":
            from agenttape.adapters.builtin._demo_review import play_review

            return await play_review(self)
        if resume.get("phase") == "planned":
            clone = Path(str(resume.get("clone", "")))
            await self.say("Approval granted. Applying the planned change without re-planning.")
            if not (clone / ".git").is_dir():
                clone = await self.prepare_clone()
            return await self.apply_change(clone)

        task = (ctx.prompt or "Improve the greeting service.").strip()
        await self.say(f"Starting on the task: {task[:240]}")
        await self.record(
            {
                "type": "reasoning_summary",
                "text": "Plan: inspect src/greeter.py, add input validation with tests, note the change "
                "in CHANGELOG.md, then run a syntax check.",
            }
        )
        await self.pause()
        clone = await self.prepare_clone()
        greeter = clone / "src" / "greeter.py"
        content = greeter.read_text(encoding="utf-8")
        await self.record(
            {"type": "file_read", "path": "src/greeter.py", "bytes": len(content.encode())}
        )
        await self.tool("read_file", {"path": "src/greeter.py"}, content)
        await self.pause()
        await self.command(
            [self.git(), "log", "--oneline", "-3"], clone, display="git log --oneline -3"
        )
        await self.pause()

        if scenario == "fail_then_retry" and ctx.attempt.number == 1:
            await self.say("Requesting the model's edit plan...")
            await self.ctx.events.event(
                EventType.STDERR,
                "ConnectionResetError: upstream closed the connection (simulated transient failure)",
                {
                    "text": "ConnectionResetError: upstream closed the connection (simulated transient failure)",
                    "stream": "stderr",
                },
                severity=Severity.NOTICE,
            )
            return AdapterResult.failure(
                FailureCategory.ADAPTER_ERROR,
                "Simulated transient failure: upstream connection reset (the next attempt will succeed)",
                exit_code=1,
            )
        if scenario == "fail":
            await self.say("Running the project checks before editing.")
            await self.command(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('collected 3 checks'); "
                    "print('FAILED check_style: line too long (simulated)', file=sys.stderr); sys.exit(1)",
                ],
                clone,
                display="python -m demo_checks",
            )
            return AdapterResult.failure(
                FailureCategory.VALIDATION_FAILED,
                "Project checks failed before any change was made (simulated)",
                exit_code=1,
            )
        if scenario == "timeout":
            await self.say("Starting a long-running analysis; this one never finishes on its own.")
            await self.command(
                [
                    sys.executable,
                    "-c",
                    "import time; print('analysing repository...', flush=True); time.sleep(3600)",
                ],
                clone,
                display="python -m demo_long_analysis",
            )
            return AdapterResult.failure(
                FailureCategory.ADAPTER_ERROR, "Long-running process exited unexpectedly"
            )
        if scenario == "needs_approval":
            plan = (
                "# Planned change\n\n"
                "1. Add type hints and input validation to `greet()` in `src/greeter.py`.\n"
                "2. Add `tests/test_greeter.py` with two unit tests.\n"
                "3. Record the change in `CHANGELOG.md`.\n\n"
                "Risk: modifies application code. Approval required before editing.\n"
            )
            artifact_id = await ctx.artifacts.write_text(
                "plan.md", plan, kind="report", media_type="text/markdown"
            )
            await self.say(
                "I have a plan. Editing application code needs approval, so I'm pausing here."
            )
            return AdapterResult(
                outcome="needs_approval",
                approval=ApprovalSpec(
                    title="Apply the Demo Agent's change to src/greeter.py",
                    rationale="The agent wants to modify application code and add tests. Review plan.md.",
                    risk_level=RiskLevel.MEDIUM,
                    requested_action={
                        "action": "edit_files",
                        "files": ["src/greeter.py", "tests/test_greeter.py", "CHANGELOG.md"],
                        "plan_artifact_id": artifact_id,
                    },
                ),
                checkpoint={
                    "phase": "planned",
                    "clone": str(clone),
                    "plan_artifact_id": artifact_id,
                },
            )
        return await self.apply_change(clone)

    async def full_evidence(self, resume: dict[str, Any]) -> AdapterResult:
        """Run the evidence demo agent as a subprocess and stream its telemetry."""
        ctx = self.ctx
        resuming = resume.get("phase") == "approved-resume"
        variant = self.variant(resume)
        workdir = await self.evidence_workdir(resume)
        approval: dict[str, Any] | None = None

        async def on_line(line: str) -> bool:
            nonlocal approval
            try:
                raw = json.loads(line)
            except ValueError:
                return False
            if not isinstance(raw, dict) or "type" not in raw:
                return False
            if raw.get("type") == "approval_request":
                approval = raw
            await self.record(raw)
            return True

        phase = "resume" if resuming else "start"
        argv = self.evidence_argv(workdir, "-", phase, variant)
        env = ctx.build_env([], {"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        result = await run_process(
            CommandSpec(
                argv=argv,
                cwd=workdir,
                env=env,
                display=f"python -m agenttape.demo_agent --phase {phase} --variant {variant}",
            ),
            ctx,
            on_stdout=on_line,
        )
        if result.stopped_reason is not None:
            raise _Stopped
        usage = (
            {"input_tokens": 240, "output_tokens": 96, "simulated": True}
            if resuming
            else {"input_tokens": 1830, "output_tokens": 412, "simulated": True}
        )
        if result.exit_code == _AGENT_NEEDS_APPROVAL and approval is not None:
            spec = evidence_approval(approval)
            return AdapterResult(
                outcome="needs_approval",
                approval=spec,
                checkpoint={
                    "phase": "approved-resume",
                    "workdir": str(workdir),
                    "variant": variant,
                    "paths": spec.requested_action["paths"],
                },
                usage=usage,
            )
        if result.exit_code == 0:
            return AdapterResult.success(
                exit_code=0,
                outputs={"summary": "Refactored greet() with full file and context evidence"},
                usage=usage,
            )
        return AdapterResult.failure(
            FailureCategory.ADAPTER_ERROR,
            f"The evidence demo agent exited with code {result.exit_code}",
            exit_code=result.exit_code,
        )

    def variant(self, resume: dict[str, Any]) -> str:
        return str(self.ctx.profile.get("variant") or resume.get("variant") or "a")

    async def evidence_workdir(self, resume: dict[str, Any]) -> Path:
        """Where the evidence demo agent runs, for every runtime: the isolated fixture
        worktree; else, when resuming, the paused attempt's directory; else a private
        clone of the fixture (file evidence then only has telemetry)."""
        workdir = self.isolated_worktree()
        if workdir is None and resume.get("phase") == "approved-resume" and resume.get("workdir"):
            candidate = Path(str(resume["workdir"]))
            workdir = candidate if (candidate / "src" / "greeter.py").is_file() else None
        return workdir if workdir is not None else await self.prepare_clone()

    def evidence_argv(self, workdir: Path, telemetry: str, phase: str, variant: str) -> list[str]:
        """The evidence demo agent's command line (the same for every runtime)."""
        return [
            sys.executable,
            "-m",
            "agenttape.demo_agent",
            "--workdir",
            str(workdir),
            "--telemetry",
            telemetry,
            "--phase",
            phase,
            "--variant",
            variant,
            "--step-delay-ms",
            str(int(self.delay * 1000)),
        ]

    def isolated_worktree(self) -> Path | None:
        """The AgentTape-owned worktree the engine prepared, when the demo project is isolated.

        Only used when the project is the AgentTape fixture repository itself, so the
        Demo Agent never edits a user's repository, not even an isolated copy of it.
        """
        ctx = self.ctx
        project = ctx.run.snapshot.get("project") or {}
        if not (project.get("settings") or {}).get("git_isolation") or ctx.workdir is None:
            return None
        fixture = fixture_path(ctx.data_dir)
        if Path(str(project.get("repo_path") or "")).resolve() != fixture.resolve():
            return None
        workdir = Path(ctx.workdir)
        if workdir.resolve() == fixture.resolve() or not (workdir / ".git").is_file():
            return None
        return workdir

    async def prepare_clone(self) -> Path:
        ctx = self.ctx
        fixture = ensure_fixture_repo(ctx.data_dir)
        isolated = self.isolated_worktree()
        if isolated is not None:
            # Git isolation: edit the worktree; the engine captures the changes.
            return isolated
        dest = ctx.data_dir / "workspaces" / "demo" / ctx.run.id / f"attempt-{ctx.attempt.number}"
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await self.command(
            [self.git(), "clone", "--quiet", "--no-hardlinks", str(fixture), str(dest)],
            None,
            display="git clone <demo fixture> <run workspace>",
        )
        return dest

    async def apply_change(self, clone: Path) -> AdapterResult:
        ctx = self.ctx
        greeter = clone / "src" / "greeter.py"
        greeter.write_text(GREETER_V2, encoding="utf-8")
        await self.tool(
            "write_file", {"path": "src/greeter.py"}, f"Wrote {len(GREETER_V2.encode())} bytes"
        )
        await self.record(
            {
                "type": "file_write",
                "path": "src/greeter.py",
                "bytes": len(GREETER_V2.encode()),
                "operation": "edit",
            }
        )
        await self.pause()
        tests = clone / "tests" / "test_greeter.py"
        tests.parent.mkdir(exist_ok=True)
        tests.write_text(TEST_FILE, encoding="utf-8")
        await self.record(
            {
                "type": "file_write",
                "path": "tests/test_greeter.py",
                "bytes": len(TEST_FILE.encode()),
                "operation": "create",
            }
        )
        changelog = clone / "CHANGELOG.md"
        changelog.write_text(
            changelog.read_text(encoding="utf-8") + CHANGELOG_ENTRY, encoding="utf-8"
        )
        await self.record(
            {
                "type": "file_write",
                "path": "CHANGELOG.md",
                "bytes": changelog.stat().st_size,
                "operation": "append",
            }
        )
        await self.pause()
        check = await self.command(
            [
                sys.executable,
                "-c",
                "import ast, pathlib\n"
                "for p in ('src/greeter.py', 'tests/test_greeter.py'):\n"
                "    ast.parse(pathlib.Path(p).read_text())\n"
                "    print('syntax ok:', p)\n",
            ],
            clone,
            display="python syntax check",
        )
        await self.command([self.git(), "diff", "--stat"], clone, display="git diff --stat")
        entries = working_tree_changes(clone)
        if self.isolated_worktree() is None:
            diff_text = unified_diff(clone)
            diff_id = await ctx.artifacts.write_text(
                "changes.diff", diff_text, kind="diff", media_type="text/x-diff"
            )
            await ctx.files.record(
                [
                    FileChangeDraft(
                        path=e.path,
                        change_type=e.change_type,
                        old_path=e.old_path,
                        insertions=e.insertions,
                        deletions=e.deletions,
                        is_binary=e.is_binary,
                        diff_artifact_id=diff_id,
                    )
                    for e in entries
                ]
            )
        await self.pause()
        summary = (
            "# Demo Agent report\n\n"
            f"- Run: `{ctx.run.id}` attempt {ctx.attempt.number}\n"
            f"- Files changed: {len(entries)}\n"
            f"- Syntax check exit code: {check.exit_code}\n\n"
            "Validated names in `greet()`, added unit tests, and updated the changelog.\n"
        )
        await ctx.artifacts.write_text(
            "summary.md", summary, kind="report", media_type="text/markdown"
        )
        prompt_len = len(ctx.prompt or "")
        usage_in = 1200 + prompt_len // 4
        usage_out = 380 + 40 * len(entries)
        await self.record({"type": "usage", "input_tokens": usage_in, "output_tokens": usage_out})
        await self.say("Done: validated names in greet(), added tests, and updated the changelog.")
        if check.exit_code != 0:
            return AdapterResult.failure(
                FailureCategory.VALIDATION_FAILED, "Syntax check failed", exit_code=check.exit_code
            )
        return AdapterResult.success(
            exit_code=0,
            outputs={
                "files_changed": len(entries),
                "summary": "Validated names in greet() and added tests",
            },
            usage={"input_tokens": usage_in, "output_tokens": usage_out, "simulated": True},
        )
