"""The V1 command policy (``domain.policy``) applied to process adapters.

The executable allowlist decides *which programs* may start. This module decides *what
they may be asked to do*: every argv a process adapter is about to launch (the agent
command itself, preflight commands, validation commands) is judged by
``domain.policy.check_command`` **before anything is spawned**:

* ``deny`` (push, merge/pull/rebase, force push, destructive reset/clean/checkout,
  deleting branches, tags or worktrees, shells, sudo, ``rm``, dumping the environment...)
  fails the attempt with ``policy_violation``. An approval cannot override it.
* ``require_approval`` (``git commit``, git subcommands that are not known to be
  read-only, ``git -c ...`` config injection, profiles marked ``elevated``) pauses the run
  for a human decision; the approved commands are fingerprinted, so a changed command
  needs a new approval.

Git aliases are resolved before judging (``git st`` -> ``git status``); shell aliases
(``!cmd``) are refused. ``git-<sub>`` helper binaries are judged as ``git <sub>``.

Residual limitation: an allowlisted program that is *not* git (a build tool, an
interpreter, a wrapper script) can do anything its own code does, including running
``git push`` internally. The policy only sees the argv AgentTape itself launches.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agenttape.adapters.base import AdapterResult, ApprovalSpec, ExecutionContext
from agenttape.domain.enums import EventType, FailureCategory, RiskLevel, Severity
from agenttape.domain.policy import (
    ApprovalType,
    PolicyDecision,
    Verdict,
    check_command,
    needs_approval,
    parse_git,
)
from agenttape.security.executables import check_executable

MAX_ALIAS_DEPTH = 5
_RISK_ORDER = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class PlannedCommand:
    label: str
    #: argv with argv[0] replaced by the canonical executable path.
    argv: list[str]
    cwd: Path | None
    #: The executable as configured (before symlinks were resolved). Multi-call binaries
    #: dispatch on the name they are started as (``git-send-pack`` -> git), so the
    #: configured name is judged too and the stricter decision wins.
    requested: str | None = None


def _strictness(decision: PolicyDecision) -> int:
    return {Verdict.ALLOW: 0, Verdict.REQUIRE_APPROVAL: 1, Verdict.DENY: 2}[decision.verdict]


def judge_planned(cmd: PlannedCommand, *, elevated: bool) -> PolicyDecision:
    decisions = [judge(cmd.argv, cmd.cwd, elevated=elevated)]
    if cmd.requested and PurePosixPath(cmd.requested).name != PurePosixPath(cmd.argv[0]).name:
        decisions.append(judge([cmd.requested, *cmd.argv[1:]], cmd.cwd, elevated=elevated))
    return max(decisions, key=_strictness)


def _git_alias(git: str, argv: list[str], cwd: Path | None) -> str | None:
    """Read ``alias.<sub>`` with the same global options (read-only ``git config --get``)."""
    inv = parse_git(argv)
    if inv.sub is None:
        return None
    try:
        proc = subprocess.run(
            [git, *inv.global_opts, "config", "--get", f"alias.{inv.sub}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = proc.stdout.strip()
    return value if proc.returncode == 0 and value else None


def judge(argv: list[str], cwd: Path | None, *, elevated: bool) -> PolicyDecision:
    """Policy decision for one canonical argv, with git aliases expanded."""
    exe = PurePosixPath(argv[0]).name
    # The executable allowlist already vetted argv[0]; the command policy adds the
    # hard-disallow list and approval rules on top of it.
    current = list(argv)
    for _depth in range(MAX_ALIAS_DEPTH):
        decision = check_command(current, allowlist=[exe], elevated=elevated)
        if exe != "git" or decision.verdict != Verdict.REQUIRE_APPROVAL:
            return decision
        inv = parse_git(current)
        if inv.config or inv.sub is None:
            return decision
        alias = _git_alias(current[0], current, cwd)
        if alias is None:
            return decision
        if alias.startswith("!"):
            return PolicyDecision(
                Verdict.DENY,
                f"git alias '{inv.sub}' runs a shell command ({alias[:80]})",
                risk_level=RiskLevel.CRITICAL,
            )
        try:
            expansion = shlex.split(alias)
        except ValueError:
            return needs_approval(
                ApprovalType.ELEVATED_COMMAND,
                f"git alias '{inv.sub}' could not be parsed",
                argv=current,
            )
        current = [current[0], *inv.global_opts, *expansion, *inv.rest]
    return PolicyDecision(Verdict.DENY, "git alias chain is too deep", risk_level=RiskLevel.HIGH)


def plan_aux(ctx: ExecutionContext, cwd: Path | None) -> tuple[list[PlannedCommand], str | None]:
    """Preflight and validation commands with canonical executables (or an error)."""
    planned: list[PlannedCommand] = []
    for label, key in (("preflight", "preflight_commands"), ("validation", "validation_commands")):
        for argv in ctx.profile.get(key) or []:
            if not argv:
                continue
            check = check_executable(argv[0], ctx.data_dir)
            if not check.ok or check.path is None:
                return planned, f"{label} command refused: {check.error}"
            planned.append(
                PlannedCommand(label, [str(check.path), *argv[1:]], cwd, requested=argv[0])
            )
    return planned, None


async def policy_gate(
    ctx: ExecutionContext, commands: list[PlannedCommand]
) -> AdapterResult | None:
    """Judge every command before any is launched. None means "go ahead"."""
    elevated = bool(ctx.profile.get("elevated"))
    pending: list[tuple[PlannedCommand, PolicyDecision]] = []
    for cmd in commands:
        decision = await asyncio.to_thread(judge_planned, cmd, elevated=elevated)
        shown = [PurePosixPath(cmd.argv[0]).name, *cmd.argv[1:]]
        if decision.verdict == Verdict.DENY:
            await ctx.events.event(
                EventType.ERROR,
                f"Refused by the V1 safety policy: {' '.join(shown)[:200]}",
                {"argv": shown, "label": cmd.label, "policy": decision.as_dict()},
                severity=Severity.ERROR,
            )
            return AdapterResult.failure(
                FailureCategory.POLICY_VIOLATION,
                f"{cmd.label} command refused: {decision.reason}",
                outputs={"policy": decision.as_dict(), "argv": shown},
            )
        if decision.verdict == Verdict.REQUIRE_APPROVAL:
            pending.append((cmd, decision))
    if not pending:
        return None
    requested: list[dict[str, Any]] = [
        {
            "label": cmd.label,
            "argv": [PurePosixPath(cmd.argv[0]).name, *cmd.argv[1:]],
            "executable": cmd.argv[0],
            "reason": d.reason,
        }
        for cmd, d in pending
    ]
    fingerprint = _fingerprint(requested)
    if (ctx.resume_state or {}).get("policy_approved") == fingerprint:
        await ctx.events.event(
            EventType.LIFECYCLE,
            f"{len(pending)} command(s) approved; continuing",
            {"commands": requested, "fingerprint": fingerprint},
        )
        return None
    risk = max((d.risk_level for _c, d in pending), key=_RISK_ORDER.index)
    return AdapterResult(
        outcome="needs_approval",
        approval=ApprovalSpec(
            title="Run " + "; ".join(" ".join(r["argv"])[:80] for r in requested)[:200],
            rationale="; ".join(sorted({d.reason for _c, d in pending})),
            # Always "elevated_command": the Git service's "commit" approvals drive its own
            # commit flow and must not be confused with an agent's command.
            type=ApprovalType.ELEVATED_COMMAND.value,
            risk_level=risk,
            requested_action={"commands": requested, "fingerprint": fingerprint},
        ),
        checkpoint={"policy_approved": fingerprint},
    )
