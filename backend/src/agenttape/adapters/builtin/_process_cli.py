"""Shared building blocks for process-based adapters (generic-command, claude-code, codex-cli).

Profile keys understood here (all optional unless an adapter says otherwise):

``executable``           absolute path or bare name on PATH; must be allowlisted
``args``                 argument array (list of strings); never a shell string
``workdir_mode``         ``project`` (default: the run's project or scratch directory) or
                         ``subdirectory`` (``working_dir`` relative to it)
``working_dir``          relative path used with ``workdir_mode: subdirectory``
``env_allowlist``        names of host environment variables passed to the child
``secret_env``           ``{ENV_NAME: SECRET_REFERENCE_NAME}``; values are resolved at
                         execution only and registered for redaction
``preflight_commands``   argument arrays run before the agent (non-zero exit fails the
                         attempt with ``validation_failed``)
``validation_commands``  argument arrays run after a successful agent process
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from agenttape.adapters.base import AdapterResult, ExecutionContext, ProfileIssue
from agenttape.adapters.builtin._command_policy import (
    PlannedCommand,
    judge,
    plan_aux,
    policy_gate,
)
from agenttape.adapters.policy import ProfilePolicy
from agenttape.adapters.process import CommandSpec, ProcessResult, run_process
from agenttape.domain.enums import AdapterAvailability, EventType, FailureCategory, Severity
from agenttape.domain.errors import PolicyViolationError
from agenttape.domain.policy import Verdict, forbidden_env_name
from agenttape.domain.prompts import SECRET_NAME
from agenttape.security.executables import check_executable, resolve_executable
from agenttape.security.paths import safe_join

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
WORKDIR_MODES = ("project", "subdirectory")
MAX_ARGS = 200
MAX_ARG_LEN = 8192
VERSION_TIMEOUT_SECONDS = 5.0
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?)")
#: Discovery results are cached briefly; ``--version`` should not run on every page load.
DISCOVERY_TTL_SECONDS = 60.0
_discovery_cache: dict[tuple[str, str], tuple[float, str | None, str | None]] = {}

#: Profile keys that the process adapters share (merged into each adapter's config schema).
COMMON_SCHEMA: dict[str, Any] = {
    "executable": {"type": "string", "description": "Absolute path or name on PATH"},
    "args": {"type": "array", "items": {"type": "string"}},
    "workdir_mode": {"type": "string", "enum": list(WORKDIR_MODES), "default": "project"},
    "working_dir": {"type": "string"},
    "env_allowlist": {"type": "array", "items": {"type": "string"}},
    "secret_env": {"type": "object", "additionalProperties": {"type": "string"}},
    "preflight_commands": {
        "type": "array",
        "items": {"type": "array", "items": {"type": "string"}},
    },
    "validation_commands": {
        "type": "array",
        "items": {"type": "array", "items": {"type": "string"}},
    },
    "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
    "idle_timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
    "max_attempts": {"type": "integer", "minimum": 1, "maximum": 20},
    "elevated": {"type": "boolean", "default": False},
}


# --------------------------------------------------------------------------- validation


def is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def validate_argv_list(field: str, value: Any, *, allow_empty: bool = True) -> list[ProfileIssue]:
    if value is None:
        return []
    if isinstance(value, str):
        return [ProfileIssue(field, "must be an argument array (list of strings), not a string")]
    if not is_str_list(value):
        return [ProfileIssue(field, "must be a list of strings")]
    issues: list[ProfileIssue] = []
    if not allow_empty and not value:
        issues.append(ProfileIssue(field, "must not be empty"))
    if len(value) > MAX_ARGS:
        issues.append(ProfileIssue(field, f"at most {MAX_ARGS} arguments"))
    for idx, arg in enumerate(value):
        if "\x00" in arg:
            issues.append(ProfileIssue(f"{field}[{idx}]", "must not contain NUL bytes"))
        if len(arg) > MAX_ARG_LEN:
            issues.append(ProfileIssue(f"{field}[{idx}]", f"at most {MAX_ARG_LEN} characters"))
    return issues


def validate_commands(field: str, value: Any) -> list[ProfileIssue]:
    if value is None:
        return []
    if not isinstance(value, list):
        return [ProfileIssue(field, "must be a list of argument arrays")]
    issues: list[ProfileIssue] = []
    for idx, cmd in enumerate(value):
        issues.extend(validate_argv_list(f"{field}[{idx}]", cmd, allow_empty=False))
    return issues


def validate_shape(config: dict[str, Any]) -> list[ProfileIssue]:
    """Policy-free checks shared by every process adapter."""
    issues: list[ProfileIssue] = []
    exe = config.get("executable")
    if exe is not None and (not isinstance(exe, str) or not exe.strip() or "\x00" in exe):
        issues.append(ProfileIssue("executable", "must be a non-empty path or command name"))
    elif isinstance(exe, str) and (os.sep in exe) and not Path(exe).expanduser().is_absolute():
        issues.append(ProfileIssue("executable", "a path must be absolute"))
    issues.extend(validate_argv_list("args", config.get("args")))
    mode = config.get("workdir_mode", "project")
    if mode not in WORKDIR_MODES:
        issues.append(ProfileIssue("workdir_mode", f"must be one of: {', '.join(WORKDIR_MODES)}"))
    wd = config.get("working_dir")
    if mode == "subdirectory":
        if not isinstance(wd, str) or not wd.strip():
            issues.append(ProfileIssue("working_dir", "is required for workdir_mode subdirectory"))
        else:
            rel = Path(wd)
            if rel.is_absolute() or ".." in rel.parts or "\x00" in wd:
                issues.append(
                    ProfileIssue("working_dir", "must be relative and must not contain '..'")
                )
    for idx, name in enumerate(config.get("env_allowlist") or []):
        if not isinstance(name, str) or not ENV_NAME.match(name):
            issues.append(ProfileIssue(f"env_allowlist[{idx}]", "must be an environment name"))
    secret_env = config.get("secret_env")
    if secret_env is not None:
        if not isinstance(secret_env, dict):
            issues.append(ProfileIssue("secret_env", "must map ENV_NAME to a secret reference"))
        else:
            for env_name, ref in secret_env.items():
                if not ENV_NAME.match(str(env_name)):
                    issues.append(
                        ProfileIssue(f"secret_env.{env_name}", "key must be an env var name")
                    )
                if not isinstance(ref, str) or not SECRET_NAME.match(ref):
                    issues.append(
                        ProfileIssue(
                            f"secret_env.{env_name}",
                            "must be a secret reference name (never a secret value)",
                        )
                    )
    env_names = [n for n in config.get("env_allowlist") or [] if isinstance(n, str)]
    if isinstance(secret_env, dict):
        env_names += [str(k) for k in secret_env]
    for name in env_names:
        if forbidden_env_name(name):
            issues.append(
                ProfileIssue(
                    "env_allowlist" if name not in (secret_env or {}) else f"secret_env.{name}",
                    f"{name} changes what git or the loader does without appearing in the "
                    "command, so it is never passed to agents",
                )
            )
    elevated = config.get("elevated")
    if elevated is not None and not isinstance(elevated, bool):
        issues.append(ProfileIssue("elevated", "must be true or false"))
    issues.extend(validate_commands("preflight_commands", config.get("preflight_commands")))
    issues.extend(validate_commands("validation_commands", config.get("validation_commands")))
    return issues


def validate_policy(
    config: dict[str, Any],
    policy: ProfilePolicy,
    *,
    default_executable: str | None,
    default_names: tuple[str, ...] = (),
) -> list[ProfileIssue]:
    """Checks that need local state: allowlisted executables and known secret references."""
    issues: list[ProfileIssue] = []
    exe = config.get("executable") or default_executable
    if isinstance(exe, str) and exe.strip():
        check = check_executable(exe, policy.data_dir, default_names=default_names)
        if not check.ok:
            issues.append(ProfileIssue("executable", check.error or "not allowed"))
    # A missing executable is reported once by the adapter's shape checks.
    for field in ("preflight_commands", "validation_commands"):
        for idx, cmd in enumerate(config.get(field) or []):
            if is_str_list(cmd) and cmd:
                check = check_executable(cmd[0], policy.data_dir)
                if not check.ok:
                    issues.append(ProfileIssue(f"{field}[{idx}]", check.error or "not allowed"))
    secret_env = config.get("secret_env")
    if isinstance(secret_env, dict):
        for env_name, ref in secret_env.items():
            if isinstance(ref, str) and ref not in policy.secret_names:
                issues.append(
                    ProfileIssue(
                        f"secret_env.{env_name}",
                        f"secret reference '{ref}' is not defined; it will be read from the "
                        f"environment variable {ref} at execution time",
                        severity="warning",
                    )
                )
    return issues


# --------------------------------------------------------------------------- discovery


def detect_version(
    exe: Path, *, name_hint: str, timeout: float = VERSION_TIMEOUT_SECONDS
) -> tuple[str | None, str | None]:
    """Return (version, error) for ``exe`` without credentials or model calls.

    Many installers put the version in the resolved path (``.../versions/2.1.280`` or
    ``.../releases/0.156.1-x86_64.../bin/codex``); that is used first. Otherwise the
    documented ``--version`` flag is run with a short timeout (argument array, no shell).
    """
    key = (name_hint, str(exe))
    cached = _discovery_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < DISCOVERY_TTL_SECONDS:
        return cached[1], cached[2]
    version: str | None = None
    error: str | None = None
    for part in reversed(exe.parts):
        match = _VERSION_RE.match(part)
        if match:
            version = match.group(1).split("-x86")[0].split("-aarch")[0]
            break
    if version is None:
        try:
            proc = subprocess.run(
                [str(exe), "--version"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                stdin=subprocess.DEVNULL,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "NO_COLOR": "1"},
            )
            match = _VERSION_RE.search(proc.stdout + proc.stderr)
            if proc.returncode == 0 and match:
                version = match.group(1)
            else:
                error = f"`{exe.name} --version` exited with {proc.returncode}"
        except (OSError, subprocess.SubprocessError) as exc:
            error = f"`{exe.name} --version` failed: {exc}"
    _discovery_cache[key] = (now, version, error)
    return version, error


def default_executable_for(default_name: str, env_override: str) -> tuple[str, tuple[str, ...]]:
    """(executable to use when a profile sets none, names trusted without allowlisting).

    ``env_override`` is operator configuration, so its path is trusted like the default name.
    """
    override = os.environ.get(env_override)
    if override:
        return override, (default_name, override)
    return default_name, (default_name,)


def discover_cli(
    adapter: str,
    *,
    env_override: str,
    default_name: str,
    setup_hint: str,
) -> tuple[AdapterAvailability, str | None, str | None, str, dict[str, Any]]:
    """Locate a CLI; returns (status, version, executable, message, details)."""
    requested = os.environ.get(env_override) or default_name
    path = resolve_executable(requested)
    details: dict[str, Any] = {"requested": requested, "env_override": env_override}
    if path is None:
        return (
            AdapterAvailability.UNAVAILABLE,
            None,
            None,
            f"'{requested}' was not found. {setup_hint}",
            details,
        )
    if not os.access(path, os.X_OK):
        return (
            AdapterAvailability.MISCONFIGURED,
            None,
            str(path),
            f"{path} is not executable",
            details,
        )
    version, error = detect_version(path, name_hint=adapter)
    if version is None:
        return (
            AdapterAvailability.MISCONFIGURED,
            None,
            str(path),
            f"Found {path}, but its version could not be read ({error})",
            details,
        )
    return (
        AdapterAvailability.INSTALLED,
        version,
        str(path),
        "Installed. Credentials are not checked during discovery; a run reports "
        "authentication problems as a config error.",
        details,
    )


# --------------------------------------------------------------------------- execution


class ExecutionSetupError(Exception):
    def __init__(self, category: FailureCategory, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message

    def result(self) -> AdapterResult:
        return AdapterResult.failure(self.category, self.message)


def resolve_launch(
    ctx: ExecutionContext, *, default_executable: str | None, default_names: tuple[str, ...] = ()
) -> Path:
    """Re-check the executable against the allowlist at execution time."""
    exe = ctx.profile.get("executable") or default_executable
    if not isinstance(exe, str) or not exe:
        raise ExecutionSetupError(FailureCategory.CONFIG_ERROR, "No executable is configured")
    check = check_executable(exe, ctx.data_dir, default_names=default_names)
    if check.path is None:
        raise ExecutionSetupError(
            FailureCategory.ADAPTER_UNAVAILABLE, check.error or "executable not found"
        )
    if not check.ok:
        raise ExecutionSetupError(FailureCategory.POLICY_VIOLATION, check.error or "not allowed")
    return check.path


def resolve_workdir(ctx: ExecutionContext) -> Path | None:
    base = ctx.workdir
    if ctx.profile.get("workdir_mode", "project") != "subdirectory":
        return base
    if base is None:
        raise ExecutionSetupError(
            FailureCategory.CONFIG_ERROR, "workdir_mode subdirectory needs a project"
        )
    try:
        target = safe_join(base, str(ctx.profile.get("working_dir", "")))
    except PolicyViolationError as exc:
        raise ExecutionSetupError(FailureCategory.POLICY_VIOLATION, exc.message) from exc
    if not target.is_dir():
        raise ExecutionSetupError(
            FailureCategory.CONFIG_ERROR, f"Working directory {target} does not exist"
        )
    return target


def child_env(ctx: ExecutionContext, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Safe base env + allowlisted names + resolved secret env (values never persisted)."""
    resolved: dict[str, str] = {}
    secret_env = ctx.profile.get("secret_env") or {}
    if isinstance(secret_env, dict):
        for env_name, ref in secret_env.items():
            if not forbidden_env_name(str(env_name)):
                resolved[str(env_name)] = ctx.resolve_secret(str(ref))
    allow = [
        n
        for n in ctx.profile.get("env_allowlist") or []
        if isinstance(n, str) and not forbidden_env_name(n)
    ]
    base = {
        "AGENTTAPE_RUN_ID": ctx.run.id,
        "AGENTTAPE_ATTEMPT": str(ctx.attempt.number),
    }
    env = ctx.build_env(allow, {**base, **(extra or {}), **resolved})
    return {k: v for k, v in env.items() if not forbidden_env_name(k)}


async def gate_commands(
    ctx: ExecutionContext, main_argv: list[str], cwd: Path | None
) -> AdapterResult | None:
    """Apply the V1 command policy to the agent command and every preflight/validation
    command before anything is launched. None means "go ahead"."""
    aux, error = plan_aux(ctx, cwd)
    if error is not None:
        await ctx.events.event(EventType.ERROR, error, {}, severity=Severity.ERROR)
        return AdapterResult.failure(FailureCategory.POLICY_VIOLATION, error)
    preflight = [c for c in aux if c.label == "preflight"]
    validation = [c for c in aux if c.label == "validation"]
    return await policy_gate(
        ctx,
        [
            *preflight,
            PlannedCommand("agent", main_argv, cwd, requested=ctx.profile.get("executable")),
            *validation,
        ],
    )


async def run_aux_commands(
    ctx: ExecutionContext, commands: Iterable[list[str]], *, label: str, cwd: Path | None
) -> AdapterResult | None:
    """Run preflight/validation commands; return a failure result or None when all pass.

    ``gate_commands`` must have approved these first; hard-disallowed commands are
    refused here again as a second line of defence.
    """
    for argv in commands:
        if not argv:
            continue
        check = check_executable(argv[0], ctx.data_dir)
        if not check.ok or check.path is None:
            return AdapterResult.failure(
                FailureCategory.POLICY_VIOLATION,
                f"{label} command refused: {check.error}",
            )
        decision = judge([str(check.path), *argv[1:]], cwd, elevated=False)
        if decision.verdict == Verdict.DENY:
            return AdapterResult.failure(
                FailureCategory.POLICY_VIOLATION, f"{label} command refused: {decision.reason}"
            )
        await ctx.events.event(EventType.LIFECYCLE, f"Running {label} command: {argv[0]}", {})
        spec = CommandSpec(argv=[str(check.path), *argv[1:]], cwd=cwd, env=child_env(ctx))
        result = await run_process(spec, ctx)
        if result.stopped_reason is not None:
            return AdapterResult.failure(result.stopped_reason, f"{label} command stopped")
        if result.exit_code != 0:
            await ctx.events.event(
                EventType.ERROR,
                f"{label.capitalize()} command failed with exit code {result.exit_code}",
                {"argv": argv, "exit_code": result.exit_code},
                severity=Severity.ERROR,
            )
            return AdapterResult.failure(
                FailureCategory.VALIDATION_FAILED,
                f"{label} command {argv[0]} exited with {result.exit_code}",
                exit_code=result.exit_code,
            )
    return None


def process_failure(result: ProcessResult, what: str) -> AdapterResult | None:
    """Map spawn errors, stops, and non-zero exits to a failure result (None = exit 0)."""
    if result.spawn_error is not None:
        return AdapterResult.failure(
            FailureCategory.ADAPTER_UNAVAILABLE, f"Could not start {what}: {result.spawn_error}"
        )
    if result.stopped_reason is not None:
        return AdapterResult.failure(result.stopped_reason, f"{what} was stopped")
    if result.exit_code != 0:
        return AdapterResult.failure(
            FailureCategory.ADAPTER_ERROR,
            f"{what} exited with code {result.exit_code}",
            exit_code=result.exit_code,
        )
    return None
