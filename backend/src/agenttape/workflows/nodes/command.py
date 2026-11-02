"""``command`` node: run one program with an argument array (never through a shell).

Policy (``domain/policy.check_command``) runs first: hard-disallowed commands fail with
``policy_violation`` and can never be approved; elevated commands and executables outside
the allowlist pause for an ``elevated_command`` approval. The allowlist is the project's
``command_allowlist`` setting when present, otherwise the built-in default; the project's
own ``validation_commands`` are trusted as exact argument arrays.

Before policy, arguments rendered from templates are checked: a substitution that starts
an argument may not produce an option (``-...``) or, for ``env``/``make``, a ``NAME=value``
assignment (``injected_arguments``), because inputs and upstream outputs can carry webhook
payloads and agent output.

The working directory must be the project repository or an AgentTape-owned workspace.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from agenttape.adapters.base import (
    AttemptInfo,
    ExecutionContext,
    FileChangeDraft,
    RunInfo,
)
from agenttape.adapters.process import CommandSpec, run_process
from agenttape.application.services.common import hash_json
from agenttape.domain.enums import FailureCategory
from agenttape.domain.policy import Verdict, check_command
from agenttape.domain.workflows import NodeType, TemplateError, render_value
from agenttape.security.redaction import Redactor
from agenttape.workflows.nodes.base import (
    ApprovalDraft,
    NodeContext,
    NodeExecutor,
    NodeOutcome,
    StepData,
    register_node,
)

MAX_CAPTURE = 64 * 1024


def allowed_workdirs(nc: NodeContext) -> list[Path]:
    roots = [nc.ctx.settings.workspaces_dir.resolve()]
    if nc.run.project_path:
        roots.append(Path(nc.run.project_path).resolve())
    return roots


def default_workdir(nc: NodeContext) -> Path:
    if nc.run.project_path and Path(nc.run.project_path).is_dir():
        return Path(nc.run.project_path)
    scratch = nc.ctx.settings.workspaces_dir / "runs" / nc.run.id
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def resolve_workdir(nc: NodeContext, raw: Any) -> Path | str:
    """Canonical working directory, or an error message."""
    if raw in (None, ""):
        return default_workdir(nc)
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = default_workdir(nc) / path
    resolved = path.resolve()
    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed_workdirs(nc)):
        return (
            f"Working directory {resolved} is outside the project repository and "
            "AgentTape-owned workspaces"
        )
    if not resolved.is_dir():
        return f"Working directory {resolved} does not exist"
    return resolved


#: Programs whose ``NAME=value`` arguments change what they run (``env LD_PRELOAD=...``,
#: ``make SHELL=... CC=...``), so a template value may not start one of those either.
_ASSIGNMENT_PROGRAMS = {"env", "make", "gmake"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_.][A-Za-z0-9_.\-]*\+?:?=")


def _starts_with_template(raw: str) -> bool:
    """True when the argument's first character comes from a ``{{ ... }}`` substitution."""
    first = raw.find("{{")
    return first != -1 and raw[:first] == ""


def injected_arguments(template_argv: list[Any], argv: list[str]) -> list[str]:
    """Rendered arguments whose leading text came from a template and forms an option.

    A value substituted at the start of an argument (``{{ inputs.ref }}``) that renders to
    ``-...`` would be read as an option (``--upload-pack=...``, ``--output=...``,
    ``--eval=...``), so it is refused. The node author can still write the option name
    literally (``--branch={{ inputs.ref }}``): that prefix is author-controlled. Only
    arguments after the program (argv[1:]) are checked.
    """
    literal_names = {
        PurePosixPath(a).name for a in template_argv if isinstance(a, str) and "{{" not in a
    }
    assignments_matter = bool(literal_names & _ASSIGNMENT_PROGRAMS)
    problems: list[str] = []
    for index, (raw, rendered) in enumerate(zip(template_argv, argv, strict=False)):
        if index == 0 or not isinstance(raw, str) or not _starts_with_template(raw):
            continue
        if rendered.startswith("-"):
            problems.append(f"argument {index} renders to {rendered[:60]!r}")
        elif assignments_matter and _ASSIGNMENT.match(rendered):
            problems.append(f"argument {index} renders to the assignment {rendered[:60]!r}")
    return problems


class _NoFiles:
    async def record(self, changes: list[FileChangeDraft]) -> None:
        return None


class _NoArtifacts:
    async def write_text(self, name: str, text: str, **_: Any) -> str:
        raise RuntimeError("command nodes do not write artifacts")

    async def write_bytes(self, name: str, data: bytes, **_: Any) -> str:
        raise RuntimeError("command nodes do not write artifacts")


@register_node
class CommandNode(NodeExecutor):
    type = NodeType.COMMAND

    async def start(self, nc: NodeContext) -> NodeOutcome:
        c = nc.config
        try:
            template_argv = list(c.get("argv") or [])
            argv_raw = render_value(template_argv, nc.template_context)
            cwd_raw = render_value(c.get("cwd"), nc.template_context)
        except TemplateError as exc:
            return NodeOutcome.fail(FailureCategory.CONFIG_ERROR, str(exc), retryable=False)
        argv = [a if isinstance(a, str) else json.dumps(a) for a in argv_raw]
        injected = injected_arguments(template_argv, argv)
        if injected:
            message = (
                "Template values cannot start a command-line option or assignment: "
                + "; ".join(injected)
                + ". Write the option name literally in the node's argv (for example "
                "'--name={{ inputs.x }}') so the input can only supply its value."
            )
            await nc.events.event(
                "step",
                f"Command refused: {message}",
                {"argv": argv, "injected": injected},
                severity="error",
            )
            return NodeOutcome.fail(
                FailureCategory.POLICY_VIOLATION,
                message,
                retryable=False,
                outputs={"injected_arguments": injected},
            )
        settings = nc.run.project_settings
        allowlist = settings.get("command_allowlist")
        decision = check_command(
            argv,
            allowlist=allowlist if isinstance(allowlist, list) else None,
            elevated=bool(c.get("elevated")),
            trusted_commands=[v for v in settings.get("validation_commands") or [] if v],
        )
        fingerprint = hash_json({"argv": argv, "cwd": cwd_raw})
        if decision.verdict == Verdict.DENY:
            await nc.events.event(
                "step",
                f"Command refused by policy: {decision.reason}",
                {"argv": argv, "policy": decision.as_dict()},
                severity="error",
            )
            return NodeOutcome.fail(
                FailureCategory.POLICY_VIOLATION,
                decision.reason,
                retryable=False,
                outputs={"policy": decision.as_dict()},
            )
        granted = (nc.step.state or {}).get("approval_granted") or {}
        if (
            decision.verdict == Verdict.REQUIRE_APPROVAL
            and granted.get("fingerprint") != fingerprint
        ):
            assert decision.approval_type is not None
            return NodeOutcome(
                status="waiting",
                approval=ApprovalDraft(
                    title=f"Run command for '{nc.node.label}': {' '.join(argv)[:160]}",
                    rationale=decision.reason,
                    type=decision.approval_type.value,
                    risk_level=decision.risk_level,
                    requested_action={
                        "argv": argv,
                        "cwd": cwd_raw,
                        "fingerprint": fingerprint,
                        "node": nc.node.key,
                    },
                    expires_in_seconds=c.get("approval_expires_in_seconds"),
                ),
                state={"pending_fingerprint": fingerprint},
            )
        workdir = resolve_workdir(nc, cwd_raw)
        if isinstance(workdir, str):
            return NodeOutcome.fail(FailureCategory.POLICY_VIOLATION, workdir, retryable=False)

        stdout: list[str] = []
        stderr: list[str] = []
        sizes = {"out": 0, "err": 0}

        def collector(buf: list[str], key: str) -> Any:
            async def handle(line: str) -> bool:
                if sizes[key] < MAX_CAPTURE:
                    buf.append(line)
                    sizes[key] += len(line) + 1
                return False

            return handle

        redactor = Redactor()
        exec_ctx = ExecutionContext(
            run=RunInfo(
                id=nc.run.id,
                title=nc.run.title,
                kind="workflow",
                source="workflow",
                project_id=nc.run.project_id,
                workspace_id=nc.run.workspace_id,
                is_demo=nc.run.is_demo,
                snapshot=nc.run.snapshot,
            ),
            attempt=AttemptInfo(
                id=nc.step.id, number=nc.step.attempt_count, max_attempts=nc.node.max_attempts
            ),
            adapter_type="workflow",
            profile={},
            prompt=None,
            inputs=dict(nc.step.inputs or {}),
            workdir=workdir,
            events=nc.events,
            cancel=nc.cancel,
            artifacts=_NoArtifacts(),
            files=_NoFiles(),
            resume_state=None,
            data_dir=nc.ctx.settings.data_dir,
            log_dir=nc.log_dir,
            resolve_secret=lambda name: "",
            resolved_prompt=lambda: None,
            redact=redactor.redact_text,
            limits={
                "cancel_grace_seconds": nc.ctx.settings.cancel_grace_seconds,
                "max_raw_log_bytes": nc.ctx.settings.max_raw_log_bytes,
            },
        )
        env_allow = settings.get("env_allowlist") or []
        spec = CommandSpec(argv=argv, cwd=workdir, env=exec_ctx.build_env(list(env_allow)))
        result = await run_process(
            spec, exec_ctx, on_stdout=collector(stdout, "out"), on_stderr=collector(stderr, "err")
        )
        outputs: dict[str, Any] = {
            "argv": argv,
            "cwd": str(workdir),
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "stdout": "\n".join(stdout),
            "stderr": "\n".join(stderr),
            "stdout_lines": stdout,
        }
        if result.stopped_reason is not None:
            category = result.stopped_reason
            return NodeOutcome(
                status="timed_out" if category == FailureCategory.TIMEOUT else "failed",
                failure_category=category,
                message=nc.cancel.message or "Stopped",
                outputs=outputs,
                retryable=category == FailureCategory.TIMEOUT,
            )
        if result.spawn_error is not None:
            return NodeOutcome.fail(
                FailureCategory.CONFIG_ERROR,
                f"Could not start {argv[0]}: {result.spawn_error}",
                retryable=False,
                outputs=outputs,
            )
        if c.get("parse_json"):
            try:
                outputs["json"] = json.loads(outputs["stdout"] or "null")
            except json.JSONDecodeError as exc:
                return NodeOutcome.fail(
                    FailureCategory.VALIDATION_FAILED,
                    f"stdout is not valid JSON: {exc}",
                    outputs=outputs,
                )
        expected = c.get("expected_exit_codes") or [0]
        if result.exit_code not in expected:
            return NodeOutcome.fail(
                FailureCategory.VALIDATION_FAILED,
                f"{argv[0]} exited with {result.exit_code} (expected {expected})",
                outputs=outputs,
            )
        return NodeOutcome.ok(outputs)

    def on_approval(self, step: StepData, approved: bool, decision: dict[str, Any]) -> NodeOutcome:
        if approved:
            fingerprint = (step.state or {}).get("pending_fingerprint")
            return NodeOutcome(
                status="running",
                state={"approval_granted": {**decision, "fingerprint": fingerprint}},
            )
        return super().on_approval(step, approved, decision)
