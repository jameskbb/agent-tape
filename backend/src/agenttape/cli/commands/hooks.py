"""``agenttape hooks setup|remove|status|relay``: Claude Code hooks that send tool-level
telemetry to AgentTape (observational only). Every tool and SessionEnd hook is a built-in
``type: "http"`` entry with an explicit 3s timeout, so a merely slow AgentTape can never
stall a user's tool call for more than that.

SessionStart is the one exception: Claude Code does not run ``http`` hooks for it
("HTTP hooks are not supported for SessionStart"), so it is an exec-form
``type: "command"`` hook (absolute interpreter, fixed argv, no shell, isolated mode)
running the relay's own entry point, which posts the hook's stdin to the same endpoint with the token it reads
from AgentTape's 0600 token store, and fails open inside a 3s deadline
(``cli/hook_relay.py``).

This module owns every read/write of the user's actual ``settings.json``; the merge
itself is delegated entirely to ``capture/claude_settings.py`` (pure, no I/O).
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from agenttape.adapters.builtin._process_cli import detect_version
from agenttape.adapters.builtin.claude_code import DEFAULT_NAME as CLAUDE_DEFAULT_NAME
from agenttape.adapters.builtin.claude_code import ENV_OVERRIDE as CLAUDE_ENV_OVERRIDE
from agenttape.capture.claude_settings import (
    EXEC_FORM_MIN_CLAUDE_CODE_VERSION,
    HOOK_TIMEOUT_SECONDS,
    HOOK_URL_PATH,
    RELAY_EVENTS,
    RELAY_PROBE_ARGS,
    SHELL_SPECIAL_DESCRIPTION,
    InstallState,
    RelayCommand,
    SettingsShapeError,
    inspect,
    install,
    relay_interpreter_problem,
    shell_special_character,
    uninstall,
    version_tuple,
)
from agenttape.cli import hook_relay
from agenttape.cli.common import echo_json
from agenttape.config import load_settings
from agenttape.security.capture import CaptureTokenStore, capture_api_base
from agenttape.security.executables import resolve_executable

BACKUP_SUFFIX = ".agenttape.bak"
#: Never a real token; only used to validate the merge shape before a token is minted
#: (a mint must only ever happen once the in-memory merge has already succeeded).
_PROBE_TOKEN = "atc_probe-not-a-real-token"  # noqa: S105 (not a secret, never persisted)


SettingsOption = Annotated[
    Path | None,
    typer.Option(
        "--settings",
        help=(
            "Claude Code settings.json path (default: $CLAUDE_CONFIG_DIR/settings.json, "
            "else ~/.claude/settings.json)"
        ),
    ),
]


def _settings_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser()
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser() / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def _fail(path: Path, reason: str) -> NoReturn:
    typer.secho(f"error: {reason}; {path} was not changed.", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _load_document(path: Path) -> dict[str, Any]:
    """``{}`` for a missing file. Exits 1 (file left untouched, no token minted) for
    invalid JSON or a document that is not a JSON object."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail(path, f"{path} is not valid JSON ({exc})")
    if not isinstance(data, dict):
        _fail(path, f"{path} does not contain a JSON object")
    return data


def _relay_command(data_dir: Path) -> RelayCommand:
    """This interpreter (the one AgentTape is installed in) and the data dir, both
    absolute: the only two values the SessionStart relay hook's argv carries."""
    return RelayCommand(
        interpreter=os.path.abspath(sys.executable),  # noqa: PTH100 (keep venv symlinks)
        data_dir=str(data_dir.expanduser().absolute()),
    )


_UNCAPTURED_CONSEQUENCE = (
    "Without it AgentTape cannot show that its hooks covered a session from the start, so "
    "a missing read can only ever be `read_not_observed`, never `never_read`."
)
#: How long the relay probe may take in ``hooks setup``; the same bound Claude Code gives
#: the hook itself.
RELAY_PROBE_TIMEOUT_SECONDS = 10
#: How long each of ``hooks status``'s two checks (the relay probe, ``claude --version``)
#: may take, so a scripted status call stays quick.
STATUS_CHECK_TIMEOUT_SECONDS = 3


def _relay_probe_failure(
    interpreter: str, *, timeout: float = RELAY_PROBE_TIMEOUT_SECONDS
) -> str | None:
    """``None`` when ``interpreter`` can run the SessionStart relay, else why not. Runs the
    relay's own ``--probe`` exactly as the hook runs (``-I``, no shell) from an empty
    temporary directory with no stdin; it posts and reads nothing."""
    try:
        with tempfile.TemporaryDirectory(prefix="agenttape-relay-probe-") as cwd:
            proc = subprocess.run(
                [interpreter, *RELAY_PROBE_ARGS],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return f"it did not answer within {timeout:g}s"
    except OSError as exc:
        return f"it could not be started ({exc.strerror or exc})"
    if proc.returncode == 0 and proc.stdout == hook_relay.PROBE_OUTPUT and not proc.stderr:
        return None
    lines = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip().splitlines()
    detail = lines[-1][:300] if lines else "no output"
    return f"`-I -m {RELAY_PROBE_ARGS[2]} --probe` exited {proc.returncode}: {detail}"


def _is_own_interpreter(interpreter: str) -> bool:
    """Whether ``interpreter`` is an absolute path to the very Python running AgentTape now
    (the only one ``hooks status`` will execute to probe)."""
    try:
        path = Path(interpreter)
        return path.is_absolute() and path.resolve() == Path(sys.executable).resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _merge_or_exit(
    document: dict[str, Any],
    *,
    url: str,
    token: str,
    relay: RelayCommand,
    skip_relay: bool,
    path: Path,
) -> dict[str, Any]:
    try:
        return install(document, url=url, token=token, relay=relay, skip_relay=skip_relay)
    except (SettingsShapeError, ValueError) as exc:
        _fail(path, str(exc))


def _write_document(path: Path, document: dict[str, Any], *, private: bool = False) -> None:
    """Back up the previous file (if any) to ``<name>.agenttape.bak`` (0600), then write
    atomically (temp file in the same directory + ``os.replace``). An existing file's mode
    is kept unless ``private`` is set, in which case it is always written 0600 (used when
    the file now carries a live bearer token). A brand-new file is created 0600 in
    a parent created 0700 either way."""
    existing_mode: int | None = None
    if path.exists():
        existing_mode = stat.S_IMODE(path.stat().st_mode)
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)  # noqa: PTH101 (matches security/capture.py's own convention)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)  # noqa: PTH101
    mode = 0o600 if private else (existing_mode if existing_mode is not None else 0o600)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, mode)  # noqa: PTH101
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(document, indent=2) + "\n")
        os.replace(tmp_path, path)  # noqa: PTH105 (atomic rename, same-directory temp file)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _mask_tokens(document: dict[str, Any]) -> dict[str, Any]:
    """A deep copy of ``document`` with every AgentTape bearer token replaced by a
    non-secret placeholder; never print a real token, even in ``--dry-run``."""
    masked = copy.deepcopy(document)
    hooks = masked.get("hooks")
    if not isinstance(hooks, dict):
        return masked
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for entry in group.get("hooks", []):
                if not isinstance(entry, dict):
                    continue
                headers = entry.get("headers")
                if isinstance(headers, dict) and isinstance(headers.get("Authorization"), str):
                    headers["Authorization"] = "Bearer atc_…"
    return masked


def _permission_warning(path: Path) -> str | None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"{path} is readable by group or others (mode {oct(mode)}); it carries a live hooks token"
    return None


def _relay_token_copy_valid(store: CaptureTokenStore) -> bool:
    copy_token = store.read_client_token("hooks")
    return copy_token is not None and store.verify("hooks", copy_token)


def _warnings_for(
    state: InstallState, path: Path, *, store: CaptureTokenStore | None = None
) -> list[str]:
    warnings: list[str] = []
    if state.disable_all_hooks:
        warnings.append(
            "disableAllHooks is set in this settings file, so all hooks, including "
            "AgentTape's, are turned off"
        )
    if state.url_blocked_by_allowlist:
        warnings.append(
            "allowedHttpHookUrls is set and does not include AgentTape's hook url, so "
            "AgentTape's hooks will never run"
        )
    for event in state.legacy_http_events:
        warnings.append(
            f"{event} is installed as an http hook, which Claude Code does not run; run "
            "`agenttape hooks setup` again to install it as a command hook"
        )
    for event in state.outdated_relay_events:
        warnings.append(
            f"the {event} command hook was installed by an older AgentTape (it loads the "
            "whole CLI at every session start and, without -I, can run Python modules from "
            "the session's working directory); run `agenttape hooks setup` again to "
            "replace it"
        )
    if (
        state.relay_events
        and state.relay_interpreter is not None
        and not Path(state.relay_interpreter).is_file()
    ):
        warnings.append(
            f"the {', '.join(state.relay_events)} hook runs {state.relay_interpreter}, "
            "which no longer exists; run `agenttape hooks setup` again"
        )
    if (
        state.relay_interpreter is not None
        and shell_special_character(state.relay_interpreter) is not None
    ):
        warnings.append(
            f"the {', '.join(state.relay_events)} hook runs {state.relay_interpreter}, whose "
            f"path contains {SHELL_SPECIAL_DESCRIPTION}, which a shell treats specially; run "
            "`agenttape hooks setup` again, which says how to fix it"
        )
    uncaptured = [
        event
        for event in RELAY_EVENTS
        if state.events
        and event not in state.relay_events
        and event not in state.legacy_http_events
    ]
    for event in uncaptured:
        warnings.append(
            f"{event} is not captured: no AgentTape command hook is installed for it. "
            f"{_UNCAPTURED_CONSEQUENCE} Run `agenttape hooks setup` again; if it says the "
            "interpreter path contains a character a shell treats specially, install "
            "AgentTape in a virtualenv at a path without one and run setup with that Python"
        )
    if state.relay_events and store is not None and not _relay_token_copy_valid(store):
        warnings.append(
            f"the {', '.join(state.relay_events)} hook has no valid hooks token to send; run "
            "`agenttape hooks setup` again"
        )
    permission_warning = _permission_warning(path)
    if permission_warning:
        warnings.append(permission_warning)
    return warnings


def _claude_code_report(*, run: bool = True) -> dict[str, Any]:
    """The installed Claude Code's version (found as ``agents discover`` finds it: the
    ``AGENTTAPE_CLAUDE_EXECUTABLE`` override or ``claude`` on PATH, then its install path,
    else ``claude --version`` with an argument array and a
    :data:`STATUS_CHECK_TIMEOUT_SECONDS` timeout), and whether it is at least the oldest
    version verified to run a command hook's ``args`` without a shell; ``None`` when that
    cannot be determined, or when ``run`` is false (nothing is executed)."""
    version: str | None = None
    if run:
        with contextlib.suppress(Exception):
            exe = resolve_executable(os.environ.get(CLAUDE_ENV_OVERRIDE) or CLAUDE_DEFAULT_NAME)
            if exe is not None and os.access(exe, os.X_OK):
                version, _error = detect_version(
                    exe, name_hint="claude-code", timeout=STATUS_CHECK_TIMEOUT_SECONDS
                )
    found = version_tuple(version) if version else None
    minimum = version_tuple(EXEC_FORM_MIN_CLAUDE_CODE_VERSION)
    return {
        "version": version,
        "exec_form_min_version": EXEC_FORM_MIN_CLAUDE_CODE_VERSION,
        "exec_form_supported": None if found is None or minimum is None else found >= minimum,
    }


def register(app: typer.Typer) -> None:
    hooks_app = typer.Typer(
        help=("Claude Code hooks that send tool-level telemetry to AgentTape (observational only)"),
        no_args_is_help=True,
    )
    app.add_typer(hooks_app, name="hooks")

    @hooks_app.command()
    def setup(
        settings: SettingsOption = None,
        rotate: Annotated[
            bool,
            typer.Option(
                "--rotate", help="Replace the hooks token even if the existing one is still valid"
            ),
        ] = False,
        dry_run: Annotated[
            bool,
            typer.Option(
                "--dry-run", help="Print the merged settings document; write and mint nothing"
            ),
        ] = False,
    ) -> None:
        """Install AgentTape's hooks: PreToolUse, PostToolUse, PostToolUseFailure, and
        SessionEnd as http hooks, and SessionStart as a command hook relaying to the same
        endpoint (Claude Code does not run http hooks for SessionStart). All are
        observational-only and bounded by a short timeout."""
        path = _settings_path(settings)
        document = _load_document(path)
        app_settings = load_settings()
        url = capture_api_base(app_settings) + HOOK_URL_PATH
        store = CaptureTokenStore(app_settings.data_dir)
        relay_command = _relay_command(app_settings.data_dir)

        current = inspect(document, url=url, data_dir=relay_command.data_dir)
        reusable = not rotate and current.token is not None and store.verify("hooks", current.token)
        reuse_token = current.token if reusable else None

        # An interpreter path a shell would split costs SessionStart only: the four http
        # hooks still go in.
        relay_problem = relay_interpreter_problem(relay_command.interpreter)
        skip_relay = relay_problem is not None

        if dry_run:
            preview = _merge_or_exit(
                document,
                url=url,
                token=reuse_token or _PROBE_TOKEN,
                relay=relay_command,
                skip_relay=skip_relay,
                path=path,
            )
            typer.echo(json.dumps(_mask_tokens(preview), indent=2, ensure_ascii=False))
            return

        probe = _merge_or_exit(
            document,
            url=url,
            token=reuse_token or _PROBE_TOKEN,
            relay=relay_command,
            skip_relay=skip_relay,
            path=path,
        )
        relay_failure = None if skip_relay else _relay_probe_failure(relay_command.interpreter)
        if relay_failure is not None:
            _fail(
                path,
                f"{relay_command.interpreter} cannot run AgentTape's SessionStart relay "
                f"({relay_failure}); run `agenttape hooks setup` with a Python that has "
                "AgentTape installed in it, such as `uv run agenttape hooks setup` in "
                "AgentTape's backend directory",
            )
        if reuse_token is not None:
            token = reuse_token
            merged = probe
            # An install from before the SessionStart relay kept no copy for it to read.
            store.ensure_client_copy("hooks", token)
        else:
            token = store.mint("hooks", client_copy=True)
            merged = install(
                document, url=url, token=token, relay=relay_command, skip_relay=skip_relay
            )
        if relay_problem is not None:
            typer.secho(
                f"warning: {', '.join(RELAY_EVENTS)} is not captured: {relay_problem}. "
                f"{_UNCAPTURED_CONSEQUENCE}",
                fg=typer.colors.YELLOW,
                err=True,
            )

        if merged == document:
            typer.echo(f"AgentTape hooks are already installed in {path} (token still valid).")
            current_mode = stat.S_IMODE(path.stat().st_mode)
            if current_mode != 0o600:
                os.chmod(path, 0o600)  # noqa: PTH101 (the file already carries a live token)
                typer.echo(
                    f"Set {path} to owner-only (0600) because it now holds the AgentTape "
                    "hooks token."
                )
            return

        previous_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
        _write_document(path, merged, private=True)
        if previous_mode is not None and previous_mode != 0o600:
            typer.echo(
                f"Set {path} to owner-only (0600) because it now holds the AgentTape hooks token."
            )
        final_state = inspect(merged, url=url, data_dir=relay_command.data_dir)
        typer.echo(f"Installed AgentTape hooks in {path}")
        typer.echo(f"Events: {', '.join(final_state.events)}")
        typer.echo(f"URL: {url}")
        typer.echo(
            f"Timeout: {HOOK_TIMEOUT_SECONDS}s. Hooks are observational and never block a "
            f"tool call; if AgentTape is down or slow, Claude Code continues after at most "
            f"{HOOK_TIMEOUT_SECONDS}s."
        )
        if not skip_relay:
            typer.echo(
                f"{', '.join(RELAY_EVENTS)} is a command hook (Claude Code does not run http "
                f"hooks for it): {relay_command.interpreter} runs AgentTape's hook relay, "
                "which reads the hooks token from AgentTape's token store, posts to the same "
                f"URL, and gives up silently after {int(hook_relay.RELAY_DEADLINE_SECONDS)}s."
            )
        typer.echo("Only sessions inside a registered AgentTape project are recorded.")
        for warning in _warnings_for(final_state, path, store=store):
            typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW, err=True)

    @hooks_app.command()
    def relay(
        url: Annotated[str, typer.Option("--url", help="AgentTape's loopback hooks endpoint")] = "",
    ) -> None:
        """Relay one Claude Code hook's stdin JSON to AgentTape, as the SessionStart command
        hook does (``python -I -m agenttape.cli.hook_relay``); an older setup's hook runs
        this. Always exits 0 and prints nothing."""
        finished = True
        with contextlib.suppress(BaseException):
            data_dir_env = os.environ.get("AGENTTAPE_DATA_DIR")
            data_dir = Path(data_dir_env) if data_dir_env else load_settings().data_dir
            outcome = hook_relay.relay(url=url, data_dir=data_dir, stdin_fd=0, environ=os.environ)
            hook_relay.record_outcome(data_dir, outcome)
            finished = outcome[0] != "timeout"
        if not finished:
            # The attempt is still running on a daemon thread (AgentTape or stdin hung):
            # leave now, with nothing flushed and no interpreter shutdown to wait through.
            os._exit(0)

    @hooks_app.command()
    def remove(settings: SettingsOption = None) -> None:
        """Remove AgentTape's hooks and revoke the hooks token."""
        path = _settings_path(settings)
        document = _load_document(path)
        app_settings = load_settings()
        store = CaptureTokenStore(app_settings.data_dir)
        try:
            restored, removed = uninstall(
                document, data_dir=_relay_command(app_settings.data_dir).data_dir
            )
        except SettingsShapeError as exc:
            _fail(path, str(exc))
        if removed == 0:
            typer.echo(f"No AgentTape hooks were installed in {path}; it was not changed.")
            if store.read_client_token("hooks") is not None:
                # Another settings file may hold the relay that reads this copy, and which
                # one is not knowable from here, so it stays.
                typer.echo(
                    "The SessionStart relay's copy of the hooks token was kept, because another "
                    "settings file may still use it. If no settings file has AgentTape's hooks "
                    "any more, run `agenttape hooks setup` and then `agenttape hooks remove` to "
                    "revoke the token and delete the copy; `agenttape hooks setup --rotate` "
                    "replaces the token instead."
                )
            return
        _write_document(path, restored, private=False)
        store.revoke("hooks")
        typer.echo(f"Removed {removed} AgentTape hook(s) from {path} and revoked the hooks token.")

    @hooks_app.command()
    def status(
        settings: SettingsOption = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
        no_probe: Annotated[
            bool,
            typer.Option(
                "--no-probe",
                help="Run nothing: skip the relay probe and the Claude Code version check",
            ),
        ] = False,
    ) -> None:
        """Report what AgentTape's Claude Code hooks setup currently looks like. Unless
        --no-probe is given, runs two checks of at most 3s each: the relay probe (only
        with AgentTape's own interpreter) and `claude --version`."""
        path = _settings_path(settings)
        document = _load_document(path)
        app_settings = load_settings()
        url = capture_api_base(app_settings) + HOOK_URL_PATH
        store = CaptureTokenStore(app_settings.data_dir)
        state = inspect(document, url=url, data_dir=_relay_command(app_settings.data_dir).data_dir)
        token_valid = state.token is not None and store.verify("hooks", state.token)
        warnings = _warnings_for(state, path, store=store)
        relay_interpreter_exists = state.relay_interpreter is not None and (
            Path(state.relay_interpreter).is_file()
        )
        # Status is a read-only look, also at a settings file someone else wrote: it only
        # ever runs AgentTape's own interpreter, never one the document names.
        probe_it = (
            not no_probe
            and bool(state.relay_events)
            and relay_interpreter_exists
            and state.relay_interpreter is not None
            and _is_own_interpreter(state.relay_interpreter)
        )
        relay_failure = (
            _relay_probe_failure(state.relay_interpreter, timeout=STATUS_CHECK_TIMEOUT_SECONDS)
            if probe_it and state.relay_interpreter
            else None
        )
        interpreter_runs_relay = relay_failure is None if probe_it else None
        if not no_probe and state.relay_events and relay_interpreter_exists and not probe_it:
            warnings.append(
                f"the {', '.join(state.relay_events)} hook runs {state.relay_interpreter}, "
                "which is not the Python running this command, so status did not run it to "
                "check it; `agenttape hooks setup` would replace it with "
                f"{_relay_command(app_settings.data_dir).interpreter}"
            )
        if relay_failure is not None:
            warnings.append(
                f"the {', '.join(state.relay_events)} hook runs {state.relay_interpreter}, "
                f"which cannot run AgentTape's relay ({relay_failure}); run `agenttape hooks "
                "setup` again with a Python that has AgentTape installed in it"
            )
        claude_code = _claude_code_report(run=not no_probe)
        last_outcome = hook_relay.read_last_outcome(app_settings.data_dir)
        if state.relay_events and claude_code["exec_form_supported"] is False:
            warnings.append(
                f"Claude Code {claude_code['version']} is older than "
                f"{EXEC_FORM_MIN_CLAUDE_CODE_VERSION}, the oldest version verified to run a "
                "command hook's args without a shell, so the "
                f"{', '.join(state.relay_events)} hook may never run; update Claude Code"
            )

        if as_json:
            echo_json(
                {
                    "settings_path": str(path),
                    "checks_run": not no_probe,
                    "events": list(state.events),
                    "url": state.url,
                    "timeouts": list(state.timeouts),
                    "token_valid": token_valid,
                    "relay": {
                        "events": list(state.relay_events),
                        "interpreter": state.relay_interpreter,
                        "interpreter_exists": relay_interpreter_exists,
                        "interpreter_runs_relay": interpreter_runs_relay,
                        "token_copy_valid": _relay_token_copy_valid(store),
                        "last_outcome": last_outcome,
                    },
                    "session_start_captured": "SessionStart" in state.relay_events,
                    "other_relays": [
                        {"interpreter": interpreter, "data_dir": other_dir}
                        for interpreter, other_dir in state.other_relays
                    ],
                    "http_events_claude_code_skips": list(state.legacy_http_events),
                    "claude_code": claude_code,
                    "disable_all_hooks": state.disable_all_hooks,
                    "url_blocked_by_allowlist": state.url_blocked_by_allowlist,
                    "warnings": warnings,
                }
            )
            return

        if state.events:
            typer.echo(f"Installed events: {', '.join(state.events)}")
            if state.relay_events:
                typer.echo(
                    f"Command hook (relay): {', '.join(state.relay_events)} via "
                    f"{state.relay_interpreter}"
                )
                if last_outcome is None:
                    typer.echo("Relay last run: not recorded yet")
                else:
                    status_text = (
                        f" (HTTP {last_outcome['http_status']})"
                        if last_outcome["http_status"] is not None
                        else ""
                    )
                    typer.echo(
                        f"Relay last run: {last_outcome['at']} {last_outcome['outcome']}"
                        f"{status_text}"
                    )
            typer.echo(f"URL: {state.url}")
            typer.echo(f"Timeouts: {', '.join(str(t) for t in state.timeouts)}s")
            typer.echo(f"Token valid: {token_valid}")
        else:
            typer.echo(f"No AgentTape hooks installed in {path}.")
        for interpreter, other_dir in state.other_relays:
            typer.echo(
                f"Another AgentTape's relay (data dir {other_dir}) via {interpreter}: not "
                "this install's, left alone"
            )
        typer.echo(f"Claude Code: {claude_code['version'] or 'not found'}")
        for warning in warnings:
            typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW, err=True)
