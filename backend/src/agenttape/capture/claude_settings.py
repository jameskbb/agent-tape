"""Merges AgentTape's observational hooks into a Claude Code ``settings.json`` document,
without touching anything else.

Every event is a built-in ``type: "http"`` hook except ``SessionStart``: Claude Code skips
``http`` hooks for SessionStart (and Setup), logging "HTTP hooks are not supported for
SessionStart", so that one event is an exec-form ``type: "command"`` hook running
``agenttape hooks relay`` (``cli/hook_relay.py``), which posts the hook's stdin to the
same endpoint. Exec form (``command`` plus an ``args`` array) means Claude Code spawns the
interpreter directly: no shell ever parses it, and no token is written for it.

Pure: no file or network access anywhere in this module - the CLI (``cli/commands/hooks.py``)
owns every read/write of the actual settings file; this module only transforms a parsed
JSON document (a ``dict``) in memory and hands the result back.
"""

from __future__ import annotations

import copy
import fnmatch
import ntpath
import posixpath
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agenttape.config import is_loopback_host

#: Must equal ``api.routers.hooks.HOOK_URL_PATH`` (asserted by a unit test) - this is how
#: an AgentTape-owned hook entry is recognized, without adding any custom key to Claude
#: Code's own hook schema.
HOOK_URL_PATH = "/api/v1/hooks/claude-code"
#: Claude Code's own default ``http`` hook timeout is 600 seconds; every AgentTape hook
#: entry sets this explicit, short timeout instead.
HOOK_TIMEOUT_SECONDS = 3
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
SESSION_EVENTS = ("SessionStart", "SessionEnd")
RUN_ID_HEADER = "X-AgentTape-Run-Id"
#: Events Claude Code never runs an ``http`` hook for; installed as the relay ``command``
#: hook instead. Only SessionStart of these is one AgentTape records.
EVENTS_WITHOUT_HTTP_HOOKS = ("SessionStart", "Setup")
RELAY_EVENTS = ("SessionStart",)
#: Claude Code's timeout for the relay hook. The relay's own hard deadline
#: (``hook_relay.RELAY_DEADLINE_SECONDS``) plus interpreter start-up fits well inside it.
RELAY_HOOK_TIMEOUT_SECONDS = 10
#: Isolated mode (``-I``, which implies ``-E -s -P``): the hook's working directory (the
#: repository the session was opened in) is never on ``sys.path``, and ``PYTHON*``
#: variables and the user site directory are ignored, so nothing in the session's
#: directory or environment can run in place of AgentTape or print into the model's
#: context. The virtualenv's own site-packages (and an editable install's ``.pth``) still
#: load.
RELAY_INTERPRETER_FLAGS = ("-I",)
#: ``<interpreter> -I -m agenttape.cli.hook_relay --data-dir <dir> --url <url>``: the relay's
#: own minimal entry point (standard library plus the token reader, never the whole CLI).
RELAY_MODULE = "agenttape.cli.hook_relay"
_CURRENT_RELAY_PREFIX = (*RELAY_INTERPRETER_FLAGS, "-m", RELAY_MODULE)
#: ``<interpreter> -I -m agenttape.cli.hook_relay --probe``: proves an interpreter can run
#: the relay (``hooks setup`` and ``hooks status`` run it; Claude Code never does).
RELAY_PROBE_ARGS = (*_CURRENT_RELAY_PREFIX, "--probe")
#: What an older setup wrote: the whole CLI, ``-m agenttape.cli.main ... hooks relay ...``,
#: with or without ``-I``. Still recognized, so ``hooks setup`` replaces it and ``hooks
#: status`` flags it.
LEGACY_RELAY_MODULE_ARGS = ("-m", "agenttape.cli.main")
LEGACY_RELAY_SUBCOMMAND = ("hooks", "relay")
_LEGACY_RELAY_PREFIXES = (
    (*RELAY_INTERPRETER_FLAGS, *LEGACY_RELAY_MODULE_ARGS),
    LEGACY_RELAY_MODULE_ARGS,
)


#: The oldest Claude Code verified to spawn a ``command`` hook's ``command`` directly with
#: its ``args`` and no shell (the hook schema's ``args`` field: builds 2.1.281 to 2.1.284
#: all have it; older builds were not available to check). A build without it drops
#: ``args`` and runs ``command`` alone through a shell, so the relay would never run.
EXEC_FORM_MIN_CLAUDE_CODE_VERSION = "2.1.281"
#: Characters a shell treats specially in a word (bash; PowerShell, which also reads the
#: typographic quotes as quotes; and cmd.exe's ``%`` and ``^``). A relay interpreter path
#: holding one, or any whitespace or control or format character, is refused: if a Claude Code ever ignored ``args`` and ran ``command`` alone through a
#: shell, such a path could split into another command or run something. Letters in any
#: script, digits, and ``_ . / : \ + ~ - =`` are fine. Only the interpreter is checked: the
#: data dir only ever appears in ``args``, which no shell sees.
SHELL_SPECIAL_CHARACTERS = frozenset(
    "\"'`$;&|<>(){}[]*?!#%^,@\u2018\u2019\u201a\u201b\u201c\u201d\u201e"
)
SHELL_SPECIAL_DESCRIPTION = (
    "whitespace, a control character, a quote, or one of ` $ ; & | < > ( ) { } [ ] * ? ! # % ^ , @"
)


def version_tuple(version: str) -> tuple[int, ...] | None:
    """``"2.1.284"`` (or ``"2.1.284 (Claude Code)"``, ``"2.1.284-beta"``) as ``(2, 1, 284)``,
    else ``None``."""
    match = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", version)
    return tuple(int(part) for part in match.groups()) if match else None


def shell_special_character(path: str) -> str | None:
    """The first character in ``path`` a shell treats specially (see
    :data:`SHELL_SPECIAL_CHARACTERS`), else ``None``."""
    for char in path:
        if char.isspace() or unicodedata.category(char)[0] in "CZ":
            return char
        if char in SHELL_SPECIAL_CHARACTERS:
            return char
    return None


def relay_interpreter_problem(interpreter: str) -> str | None:
    """Why ``interpreter`` cannot be a relay hook's ``command``, with how to fix it; else
    ``None``."""
    if not _is_absolute(interpreter):
        return f"the Python interpreter path {interpreter!r} is not absolute"
    char = shell_special_character(interpreter)
    if char is None:
        return None
    return (
        f"the Python interpreter path {interpreter!r} contains {char!r}, and a shell treats "
        f"{SHELL_SPECIAL_DESCRIPTION} specially; install AgentTape in a virtualenv at a path "
        "without such characters and run `agenttape hooks setup` with that virtualenv's "
        "Python"
    )


@dataclass(frozen=True)
class RelayCommand:
    """The two machine-local values a relay hook needs: the absolute Python interpreter
    that has AgentTape installed, and AgentTape's data dir (where the relay reads the
    hooks token from the 0600 token store). Neither is user text."""

    interpreter: str
    data_dir: str


class SettingsShapeError(ValueError):
    """The settings document has an unexpected shape at the named JSON path."""


def _host_and_path(url: str) -> tuple[str, str]:
    """A minimal ``(host, path)`` split, kept dependency-free on purpose - this module's
    purity is asserted by an acceptance check that forbids importing a URL-parsing or I/O
    library here."""
    split = url.split("://", 1)
    rest = split[1] if len(split) == 2 else split[0]
    authority, sep, path_part = rest.partition("/")
    path = ("/" + path_part) if sep else "/"
    path = path.split("?", 1)[0].split("#", 1)[0]
    authority = authority.rsplit("@", 1)[-1]
    if authority.startswith("["):
        host = authority[1 : authority.find("]")]
    else:
        host = authority.split(":", 1)[0]
    return host, path


def hook_entry(url: str, token: str) -> dict[str, Any]:
    """The single ``http`` hook object AgentTape writes for every event."""
    return {
        "type": "http",
        "url": url,
        "timeout": HOOK_TIMEOUT_SECONDS,
        "headers": {
            "Authorization": f"Bearer {token}",
            RUN_ID_HEADER: "${AGENTTAPE_RUN_ID}",
        },
        "allowedEnvVars": ["AGENTTAPE_RUN_ID"],
    }


def _is_absolute(path: str) -> bool:
    return posixpath.isabs(path) or ntpath.isabs(path)


def relay_entry(relay: RelayCommand, *, url: str) -> dict[str, Any]:
    """The exec-form ``command`` hook AgentTape writes for each of :data:`RELAY_EVENTS`:
    a fixed argv, the absolute interpreter, and no token (the relay reads it from the
    token store at run time)."""
    if not _is_absolute(relay.interpreter) or not _is_absolute(relay.data_dir):
        raise ValueError("the relay needs an absolute interpreter path and data dir")
    problem = relay_interpreter_problem(relay.interpreter)
    if problem is not None:
        raise ValueError(problem)
    return {
        "type": "command",
        "command": relay.interpreter,
        "args": [*_CURRENT_RELAY_PREFIX, "--data-dir", relay.data_dir, "--url", url],
        "timeout": RELAY_HOOK_TIMEOUT_SECONDS,
    }


def _relay_args(obj: Any) -> list[str] | None:
    """A relay entry's ``args`` when ``obj`` is one AgentTape wrote, else ``None``."""
    if not isinstance(obj, dict) or obj.get("type") != "command":
        return None
    args = obj.get("args")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return None
    if _is_current_relay(args):
        return args
    if not any(tuple(args[: len(prefix)]) == prefix for prefix in _LEGACY_RELAY_PREFIXES):
        return None
    width = len(LEGACY_RELAY_SUBCOMMAND)
    if not any(tuple(args[i : i + width]) == LEGACY_RELAY_SUBCOMMAND for i in range(len(args))):
        return None
    return args


def _is_current_relay(args: list[str]) -> bool:
    """Whether a relay entry's ``args`` start the way ``hooks setup`` writes them today."""
    return tuple(args[: len(_CURRENT_RELAY_PREFIX)]) == _CURRENT_RELAY_PREFIX


def _option(args: list[str], name: str) -> str | None:
    for i, arg in enumerate(args[:-1]):
        if arg == name:
            return args[i + 1]
    return None


def _relay_url(args: list[str]) -> str | None:
    return _option(args, "--url")


def _is_own_hook(obj: Any, data_dir: str) -> bool:
    """Whether ``obj`` is this install's to replace or remove: any AgentTape ``http`` hook;
    any relay in an older form (``-m agenttape.cli.main ... hooks relay``), whatever data
    dir it names, since no current AgentTape writes one and without ``-I`` it runs modules
    from the session's directory; and a current relay only when its
    ``--data-dir`` is ``data_dir`` (a relay naming another one belongs to another
    AgentTape)."""
    if _is_agenttape_http_hook(obj):
        return True
    args = _relay_args(obj)
    if args is None:
        return False
    return not _is_current_relay(args) or _option(args, "--data-dir") == data_dir


def _is_agenttape_http_hook(obj: Any) -> bool:
    if not isinstance(obj, dict) or obj.get("type") != "http":
        return False
    url = obj.get("url")
    if not isinstance(url, str) or not url:
        return False
    host, path = _host_and_path(url)
    return path == HOOK_URL_PATH and is_loopback_host(host)


def is_agenttape_hook(obj: Any) -> bool:
    """Whether ``obj`` is a hook entry AgentTape itself would have written: a ``http``
    hook whose URL path is :data:`HOOK_URL_PATH` on a loopback host, or a relay
    ``command`` hook (``-I -m agenttape.cli.hook_relay ...``, or an older setup's
    ``-m agenttape.cli.main ... hooks relay``), independent of the exact
    host/port/token/interpreter, so a stale registration (an old port, a rotated token, a
    moved virtualenv) is still recognized and replaced."""
    return _is_agenttape_http_hook(obj) or _relay_args(obj) is not None


def _validate_shape(settings: Mapping[str, Any]) -> None:
    hooks = settings.get("hooks")
    if hooks is None:
        return
    if not isinstance(hooks, dict):
        raise SettingsShapeError("hooks must be an object")
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise SettingsShapeError(f"hooks.{event} must be an array")
        for i, group in enumerate(groups):
            if not isinstance(group, dict):
                raise SettingsShapeError(f"hooks.{event}[{i}] must be an object")
            group_hooks = group.get("hooks")
            if group_hooks is not None and not isinstance(group_hooks, list):
                raise SettingsShapeError(f"hooks.{event}[{i}].hooks must be an array")


def _strip_agenttape_hooks(hooks: dict[str, Any], data_dir: str) -> int:
    """Mutates ``hooks`` in place: removes every hook entry this install owns (see
    :func:`_is_own_hook`), dropping only the matcher groups and event lists that removal
    left empty. Returns the count removed."""
    removed = 0
    empty_events: list[str] = []
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        new_groups: list[Any] = []
        for group in groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            group_hooks = group.get("hooks")
            if not isinstance(group_hooks, list):
                new_groups.append(group)
                continue
            kept = [h for h in group_hooks if not _is_own_hook(h, data_dir)]
            removed += len(group_hooks) - len(kept)
            if len(kept) == len(group_hooks):
                new_groups.append(group)
            elif kept:
                new_group = dict(group)
                new_group["hooks"] = kept
                new_groups.append(new_group)
            # else: this group's hooks list was emptied entirely by the strip - drop it.
        hooks[event] = new_groups
        if not new_groups and groups:
            empty_events.append(event)
    for event in empty_events:
        del hooks[event]
    return removed


def install(
    settings: Mapping[str, Any],
    *,
    url: str,
    token: str,
    relay: RelayCommand,
    skip_relay: bool = False,
) -> dict[str, Any]:
    """Every AgentTape hook entry this install owns replaced by a fresh one at
    ``url``/``token`` (``http``) or ``relay`` (the SessionStart ``command`` hook);
    everything else in ``settings`` is left exactly as it was (deep-copied, never mutated
    in place). An older install's ``http`` SessionStart hook, or its relay for the same
    data dir, is one of the entries replaced; a relay for another data dir is kept.
    ``skip_relay`` installs the ``http`` hooks only, leaving SessionStart uncaptured (for an
    interpreter :func:`relay_interpreter_problem` refuses)."""
    _validate_shape(settings)
    result: dict[str, Any] = copy.deepcopy(dict(settings))
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        result["hooks"] = hooks
    _strip_agenttape_hooks(hooks, relay.data_dir)
    entry = hook_entry(url, token)
    relayed = None if skip_relay else relay_entry(relay, url=url)
    for event in TOOL_EVENTS:
        groups = hooks.setdefault(event, [])
        groups.append({"matcher": "*", "hooks": [entry]})
    for event in SESSION_EVENTS:
        if event in RELAY_EVENTS and relayed is None:
            continue
        groups = hooks.setdefault(event, [])
        groups.append({"hooks": [relayed if event in RELAY_EVENTS else entry]})
    return result


def uninstall(settings: Mapping[str, Any], *, data_dir: str) -> tuple[dict[str, Any], int]:
    """Every AgentTape hook entry this install owns removed (every ``http`` hook, and the
    relay for ``data_dir``; a relay for another data dir is kept); only the matcher groups and event lists the
    removal itself emptied are dropped, and the top-level ``hooks`` key only when the
    removal left it with nothing in it at all. Returns ``(document, count_removed)``."""
    _validate_shape(settings)
    result: dict[str, Any] = copy.deepcopy(dict(settings))
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result, 0
    removed = _strip_agenttape_hooks(hooks, data_dir)
    if removed and not hooks:
        del result["hooks"]
    return result, removed


@dataclass(frozen=True)
class InstallState:
    """What :func:`inspect` finds already installed in a settings document."""

    events: tuple[str, ...]
    url: str | None
    timeouts: tuple[int, ...]
    token: str | None
    disable_all_hooks: bool
    url_blocked_by_allowlist: bool
    #: The interpreter a relay ``command`` hook runs (only when every relay entry agrees).
    relay_interpreter: str | None = None
    #: Events with an AgentTape relay ``command`` hook.
    relay_events: tuple[str, ...] = ()
    #: Events with an AgentTape ``http`` hook Claude Code never runs (an install from
    #: before the SessionStart relay); ``hooks setup`` replaces them.
    legacy_http_events: tuple[str, ...] = ()
    #: Events with an AgentTape relay ``command`` hook written by an older setup (the whole
    #: CLI, and before ``-I`` open to modules in the session's directory); ``hooks setup``
    #: replaces them.
    outdated_relay_events: tuple[str, ...] = ()
    #: ``(interpreter, data_dir)`` of every current-form relay for another data dir:
    #: another AgentTape's, left alone and reported for information only.
    other_relays: tuple[tuple[str | None, str | None], ...] = ()


def _bearer_token(entry: dict[str, Any]) -> str | None:
    headers = entry.get("headers")
    if not isinstance(headers, dict):
        return None
    auth = headers.get("Authorization")
    if not isinstance(auth, str) or not auth.lower().startswith("bearer "):
        return None
    return auth[len("bearer ") :].strip()


def inspect(settings: Mapping[str, Any], *, url: str, data_dir: str) -> InstallState:
    """Report what is currently installed in ``settings``: which events have a hook this
    install owns (a relay for another data dir is not counted), the url(s)/timeout(s)/token seen on them (only when every entry
    agrees), whether ``disableAllHooks`` is set, and whether ``allowedHttpHookUrls`` (when
    present) excludes ``url`` (fnmatch-style wildcards)."""
    hooks = settings.get("hooks")
    events: list[str] = []
    urls_seen: set[str] = set()
    timeouts: set[int] = set()
    tokens: set[str] = set()
    interpreters: set[str] = set()
    relay_events: list[str] = []
    legacy_http_events: list[str] = []
    outdated_relay_events: list[str] = []
    other_relays: list[tuple[str | None, str | None]] = []
    if isinstance(hooks, dict):
        for event in (*TOOL_EVENTS, *SESSION_EVENTS):
            groups = hooks.get(event)
            if not isinstance(groups, list):
                continue
            found = False
            for group in groups:
                if not isinstance(group, dict):
                    continue
                group_hooks = group.get("hooks")
                if not isinstance(group_hooks, list):
                    continue
                for entry in group_hooks:
                    if not _is_own_hook(entry, data_dir):
                        other_args = _relay_args(entry)
                        if other_args is not None:
                            command = entry.get("command")
                            other = (
                                command if isinstance(command, str) else None,
                                _option(other_args, "--data-dir"),
                            )
                            if other not in other_relays:
                                other_relays.append(other)
                        continue
                    found = True
                    timeout = entry.get("timeout")
                    if isinstance(timeout, int):
                        timeouts.add(timeout)
                    args = _relay_args(entry)
                    if args is not None:
                        relay_url = _relay_url(args)
                        if relay_url:
                            urls_seen.add(relay_url)
                        command = entry.get("command")
                        if isinstance(command, str):
                            interpreters.add(command)
                        if event not in relay_events:
                            relay_events.append(event)
                        if not _is_current_relay(args) and event not in outdated_relay_events:
                            outdated_relay_events.append(event)
                        continue
                    if event in EVENTS_WITHOUT_HTTP_HOOKS and event not in legacy_http_events:
                        legacy_http_events.append(event)
                    entry_url = entry.get("url")
                    if isinstance(entry_url, str):
                        urls_seen.add(entry_url)
                    token = _bearer_token(entry)
                    if token:
                        tokens.add(token)
            if found:
                events.append(event)
    disable_all_hooks = bool(settings.get("disableAllHooks"))
    allowlist = settings.get("allowedHttpHookUrls")
    blocked = False
    if isinstance(allowlist, list) and allowlist:
        blocked = not any(
            isinstance(pattern, str) and fnmatch.fnmatch(url, pattern) for pattern in allowlist
        )
    return InstallState(
        events=tuple(events),
        url=next(iter(urls_seen)) if len(urls_seen) == 1 else None,
        timeouts=tuple(sorted(timeouts)),
        token=next(iter(tokens)) if len(tokens) == 1 else None,
        disable_all_hooks=disable_all_hooks,
        url_blocked_by_allowlist=blocked,
        relay_interpreter=next(iter(interpreters)) if len(interpreters) == 1 else None,
        relay_events=tuple(relay_events),
        legacy_http_events=tuple(legacy_http_events),
        outdated_relay_events=tuple(outdated_relay_events),
        other_relays=tuple(other_relays),
    )
