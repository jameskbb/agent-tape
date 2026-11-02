"""``capture/claude_settings.py``: pure install/uninstall/inspect over a Claude Code
``settings.json`` document. No file or network I/O anywhere in this test file's
subject module - see the module's own purity acceptance check.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from agenttape.api.routers.hooks import HOOK_URL_PATH as ROUTER_HOOK_URL_PATH
from agenttape.capture.claude_settings import (
    HOOK_TIMEOUT_SECONDS,
    HOOK_URL_PATH,
    RELAY_EVENTS,
    RELAY_HOOK_TIMEOUT_SECONDS,
    SESSION_EVENTS,
    TOOL_EVENTS,
    RelayCommand,
    SettingsShapeError,
    hook_entry,
    inspect,
    install,
    is_agenttape_hook,
    relay_entry,
    uninstall,
)
from agenttape.cli.hook_relay import RELAY_DEADLINE_SECONDS

URL = "http://127.0.0.1:8787" + HOOK_URL_PATH
TOKEN = "atc_test-token"
ALL_EVENTS = (*TOOL_EVENTS, *SESSION_EVENTS)
HTTP_EVENTS = tuple(e for e in ALL_EVENTS if e not in RELAY_EVENTS)
RELAY = RelayCommand(interpreter="/opt/agenttape/.venv/bin/python", data_dir="/home/u/.agenttape")
OLD_RELAY = RelayCommand(interpreter="/old/venv/bin/python3", data_dir="/old/data")


def _legacy_http_only_install(url: str, token: str) -> dict[str, Any]:
    """What ``agenttape hooks setup`` wrote before the SessionStart relay: one ``http``
    hook on every event, SessionStart included (which Claude Code never runs)."""
    entry = hook_entry(url, token)
    hooks: dict[str, Any] = {}
    for event in TOOL_EVENTS:
        hooks[event] = [{"matcher": "*", "hooks": [copy.deepcopy(entry)]}]
    for event in SESSION_EVENTS:
        hooks[event] = [{"hooks": [copy.deepcopy(entry)]}]
    return {"hooks": hooks}


def _agenttape_hooks_of(doc: dict[str, Any], event: str) -> list[dict[str, Any]]:
    groups = doc["hooks"][event]
    out = []
    for group in groups:
        out.extend(h for h in group.get("hooks", []) if is_agenttape_hook(h))
    return out


def test_hook_url_path_matches_router_constant() -> None:
    assert HOOK_URL_PATH == ROUTER_HOOK_URL_PATH


def test_install_creates_exactly_five_events_with_one_hook_each() -> None:
    result = install({}, url=URL, token=TOKEN, relay=RELAY)
    assert set(result["hooks"].keys()) == set(ALL_EVENTS)
    for event in HTTP_EVENTS:
        hooks = _agenttape_hooks_of(result, event)
        assert len(hooks) == 1
        entry = hooks[0]
        assert entry["type"] == "http"
        assert entry["timeout"] == HOOK_TIMEOUT_SECONDS == 3
        assert entry["url"] == URL
    for event in RELAY_EVENTS:
        assert _agenttape_hooks_of(result, event) == [relay_entry(RELAY, url=URL)]


def test_session_start_is_the_only_command_hook_and_every_other_event_stays_http() -> None:
    """Claude Code skips every ``http`` hook on SessionStart ("HTTP hooks are not
    supported for SessionStart"), so SessionStart alone is a ``command`` hook."""
    assert RELAY_EVENTS == ("SessionStart",)
    result = install({}, url=URL, token=TOKEN, relay=RELAY)
    types = {event: _agenttape_hooks_of(result, event)[0]["type"] for event in ALL_EVENTS}
    assert types == {
        "PreToolUse": "http",
        "PostToolUse": "http",
        "PostToolUseFailure": "http",
        "SessionStart": "command",
        "SessionEnd": "http",
    }


def test_relay_entry_is_exec_form_with_a_fixed_argv_and_no_token() -> None:
    entry = relay_entry(RELAY, url=URL)
    assert entry == {
        "type": "command",
        "command": "/opt/agenttape/.venv/bin/python",
        "args": [
            "-I",
            "-m",
            "agenttape.cli.hook_relay",
            "--data-dir",
            "/home/u/.agenttape",
            "--url",
            URL,
        ],
        "timeout": RELAY_HOOK_TIMEOUT_SECONDS,
    }
    # ``args`` present means Claude Code spawns ``command`` directly: no shell ever
    # parses it, and the token is never written into settings.json for this hook.
    assert TOKEN not in repr(entry)
    assert "shell" not in entry
    # The relay's own hard deadline finishes well inside Claude Code's hook timeout.
    assert RELAY_DEADLINE_SECONDS < RELAY_HOOK_TIMEOUT_SECONDS


def test_relay_entry_runs_the_interpreter_in_isolated_mode() -> None:
    """``-I``: the session's working directory is not on ``sys.path`` and ``PYTHON*``
    variables are ignored, so a repository's own ``json.py`` or ``typer.py`` (or a
    direnv-set ``PYTHONPATH``) can never run in place of AgentTape."""
    args = relay_entry(RELAY, url=URL)["args"]
    assert args[0] == "-I"
    assert args.index("-I") < args.index("-m")


def _legacy_relay(data_dir: str = RELAY.data_dir) -> dict[str, Any]:
    """The relay entry the first SessionStart relay wrote: ``-m`` with no ``-I``."""
    return {
        "type": "command",
        "command": RELAY.interpreter,
        "args": [
            "-m",
            "agenttape.cli.main",
            "--data-dir",
            data_dir,
            "hooks",
            "relay",
            "--url",
            URL,
        ],
        "timeout": RELAY_HOOK_TIMEOUT_SECONDS,
    }


def _isolated_cli_relay(data_dir: str = RELAY.data_dir) -> dict[str, Any]:
    """An older relay entry: ``-I`` through the whole CLI."""
    entry = _legacy_relay(data_dir)
    entry["args"] = ["-I", *entry["args"]]
    return entry


@pytest.mark.parametrize("older", [_legacy_relay, _isolated_cli_relay], ids=["m", "I-m-cli"])
def test_an_older_relay_entry_is_recognized_reported_and_replaced(
    older: Any,
) -> None:
    doc = install({}, url=URL, token=TOKEN, relay=RELAY)
    doc["hooks"]["SessionStart"] = [{"hooks": [older()]}]
    assert is_agenttape_hook(older())
    state = inspect(doc, url=URL, data_dir=RELAY.data_dir)
    assert state.outdated_relay_events == ("SessionStart",)
    assert state.relay_events == ("SessionStart",)

    upgraded = install(doc, url=URL, token=TOKEN, relay=RELAY)
    assert _agenttape_hooks_of(upgraded, "SessionStart") == [relay_entry(RELAY, url=URL)]
    assert inspect(upgraded, url=URL, data_dir=RELAY.data_dir).outdated_relay_events == ()


@pytest.mark.parametrize(
    "interpreter",
    [
        "/Users/Jane Doe/agenttape/.venv/bin/python",
        "C:\\Program Files\\Python312\\python.exe",
        "/opt/$(touch x)/bin/python",
        "/opt/a`id`/bin/python",
        "/opt/a;b/bin/python",
        "/opt/a&b/bin/python",
        "/opt/a|b/bin/python",
        "/opt/a'b/bin/python",
        '/opt/a"b/bin/python',
        "/opt/a*b/bin/python",
        "/opt/a\nb/bin/python",
        "/opt/a\tb/bin/python",
        "/opt/a(b)/bin/python",
        "/opt/a,b/bin/python",
        "/opt/a@b/bin/python",
        "/opt/a%b%/bin/python",
        "/opt/a\u201cb/bin/python",  # PowerShell treats typographic quotes as quotes
        "/opt/a\u00a0b/bin/python",  # a no-break space
        "/opt/a\u200bb/bin/python",  # a zero-width space
    ],
)
def test_relay_entry_refuses_an_interpreter_a_shell_would_split_or_interpret(
    interpreter: str,
) -> None:
    """If a Claude Code build ever ignored ``args`` and ran ``command`` through a shell, a
    space or shell character in it would run something else."""
    with pytest.raises(ValueError, match="interpreter") as raised:
        relay_entry(RelayCommand(interpreter=interpreter, data_dir="/d"), url=URL)
    assert "virtualenv" in str(raised.value)


@pytest.mark.parametrize(
    "data_dir",
    ["/home/Jane Doe/.agenttape", "/home/jos\u00e9/.agenttape", "C:\\Users\\Zo\u00eb\\.agenttape"],
)
def test_relay_entry_accepts_any_absolute_data_dir(data_dir: str) -> None:
    """The data dir only ever appears in ``args``, which a shell never sees even in the
    shell fallback the path check guards against."""
    entry = relay_entry(RelayCommand(interpreter="/usr/bin/python3", data_dir=data_dir), url=URL)
    assert data_dir in entry["args"]


@pytest.mark.parametrize(
    "interpreter",
    [
        "/home/u/agent-tape/backend/.venv/bin/python3.12",
        "C:\\Users\\jane\\agenttape\\.venv\\Scripts\\python.exe",
        "C:\\PROGRA~1\\Python312\\python.exe",
        "/opt/py+debug/bin/python",
        "/home/jos\u00e9/agenttape/.venv/bin/python",
        "C:\\Users\\Zo\u00eb\\agenttape\\.venv\\Scripts\\python.exe",
        "/home/\u7530\u4e2d/.venv/bin/python3",
    ],
)
def test_relay_entry_accepts_ordinary_interpreter_paths(interpreter: str) -> None:
    assert (
        relay_entry(RelayCommand(interpreter=interpreter, data_dir="/d"), url=URL)["command"]
        == interpreter
    )


def test_install_without_the_relay_keeps_the_four_http_hooks() -> None:
    """When the interpreter path is one a shell would split, setup installs everything but
    the SessionStart relay; an existing relay for this data dir still goes."""
    before = install({}, url=URL, token=TOKEN, relay=RELAY)
    result = install(before, url=URL, token=TOKEN, relay=RELAY, skip_relay=True)
    assert "SessionStart" not in result["hooks"]
    for event in HTTP_EVENTS:
        assert _agenttape_hooks_of(result, event) == [hook_entry(URL, TOKEN)]
    state = inspect(result, url=URL, data_dir=RELAY.data_dir)
    assert state.relay_events == ()
    assert set(state.events) == set(HTTP_EVENTS)
    spaced = RelayCommand(interpreter="/opt/Jane Doe/python", data_dir=RELAY.data_dir)
    assert install({}, url=URL, token=TOKEN, relay=spaced, skip_relay=True) == install(
        {}, url=URL, token=TOKEN, relay=RELAY, skip_relay=True
    )


def test_relay_entry_refuses_a_relative_interpreter() -> None:
    with pytest.raises(ValueError, match="absolute"):
        relay_entry(RelayCommand(interpreter="python3", data_dir="/d"), url=URL)
    with pytest.raises(ValueError, match="absolute"):
        relay_entry(RelayCommand(interpreter="/usr/bin/python3", data_dir="rel/d"), url=URL)


def test_is_agenttape_hook_recognizes_a_relay_from_any_interpreter_or_data_dir() -> None:
    assert is_agenttape_hook(relay_entry(RELAY, url=URL))
    assert is_agenttape_hook(relay_entry(OLD_RELAY, url="http://127.0.0.1:1/x"))
    assert not is_agenttape_hook({"type": "command", "command": "echo hi"})
    assert not is_agenttape_hook(
        {"type": "command", "command": "/usr/bin/python3", "args": ["-m", "other", "hooks"]}
    )
    assert not is_agenttape_hook(
        {"type": "command", "command": "/usr/bin/python3", "args": "-m agenttape.cli.main"}
    )


def test_install_upgrades_an_http_only_install_to_a_session_start_relay() -> None:
    legacy = _legacy_http_only_install("http://127.0.0.1:8787" + HOOK_URL_PATH, TOKEN)
    before = inspect(legacy, url=URL, data_dir=RELAY.data_dir)
    assert before.legacy_http_events == ("SessionStart",)
    assert before.relay_interpreter is None

    result = install(legacy, url=URL, token=TOKEN, relay=RELAY)
    [start] = _agenttape_hooks_of(result, "SessionStart")
    assert start == relay_entry(RELAY, url=URL)
    assert len(result["hooks"]["SessionStart"]) == 1  # the http group is gone, not kept
    for event in HTTP_EVENTS:
        [entry] = _agenttape_hooks_of(result, event)
        assert entry == hook_entry(URL, TOKEN)
    after = inspect(result, url=URL, data_dir=RELAY.data_dir)
    assert after.legacy_http_events == ()
    assert after.relay_interpreter == RELAY.interpreter
    assert after.token == TOKEN
    assert install(result, url=URL, token=TOKEN, relay=RELAY) == result

    restored, removed = uninstall(result, data_dir=RELAY.data_dir)
    assert removed == len(ALL_EVENTS)
    assert restored == {}


def test_install_uses_matcher_star_for_tool_events_and_no_matcher_for_session_events() -> None:
    result = install({}, url=URL, token=TOKEN, relay=RELAY)
    for event in TOOL_EVENTS:
        for group in result["hooks"][event]:
            assert group.get("matcher") == "*"
    for event in SESSION_EVENTS:
        for group in result["hooks"][event]:
            assert "matcher" not in group


def test_install_is_idempotent() -> None:
    once = install({}, url=URL, token=TOKEN, relay=RELAY)
    twice = install(once, url=URL, token=TOKEN, relay=RELAY)
    assert once == twice


def test_install_keeps_user_content_and_key_order() -> None:
    doc = {
        "permissions": {"allow": ["Bash"]},
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]},
                {"matcher": "*", "hooks": [{"type": "http", "url": "http://example.com/other"}]},
            ]
        },
        "env": {"FOO": "bar"},
        "model": "sonnet",
    }
    original = copy.deepcopy(doc)
    result = install(doc, url=URL, token=TOKEN, relay=RELAY)
    assert list(result.keys())[: len(original)] == list(original.keys())
    assert result["permissions"] == original["permissions"]
    assert result["env"] == original["env"]
    assert result["model"] == original["model"]
    pre_groups = result["hooks"]["PreToolUse"]
    assert {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]} in pre_groups
    assert any(
        g.get("matcher") == "*"
        and any(h.get("url") == "http://example.com/other" for h in g.get("hooks", []))
        for g in pre_groups
    )
    assert len(_agenttape_hooks_of(result, "PreToolUse")) == 1
    # install must not mutate the caller's own document
    assert doc == original


def test_install_replaces_an_older_agenttape_hook() -> None:
    moved_venv = RelayCommand(interpreter=OLD_RELAY.interpreter, data_dir=RELAY.data_dir)
    stale = install(
        {}, url="http://127.0.0.1:9999" + HOOK_URL_PATH, token="atc_old", relay=moved_venv
    )
    result = install(stale, url=URL, token=TOKEN, relay=RELAY)
    for event in HTTP_EVENTS:
        hooks = _agenttape_hooks_of(result, event)
        assert len(hooks) == 1
        assert hooks[0]["url"] == URL
        assert hooks[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    for event in RELAY_EVENTS:
        assert _agenttape_hooks_of(result, event) == [relay_entry(RELAY, url=URL)]


def test_a_relay_for_another_data_dir_is_left_alone() -> None:
    """A second AgentTape (a dev instance with its own data dir) keeps its own relay:
    setup and remove strip only the relay that reads this install's token."""
    other = relay_entry(OLD_RELAY, url="http://127.0.0.1:9999" + HOOK_URL_PATH)
    doc = {"hooks": {"SessionStart": [{"hooks": [other]}]}}
    original = copy.deepcopy(doc)
    assert is_agenttape_hook(other)  # still recognizably AgentTape's

    installed = install(doc, url=URL, token=TOKEN, relay=RELAY)
    start = [h for g in installed["hooks"]["SessionStart"] for h in g["hooks"]]
    assert start == [other, relay_entry(RELAY, url=URL)]
    state = inspect(installed, url=URL, data_dir=RELAY.data_dir)
    assert state.relay_interpreter == RELAY.interpreter
    assert state.url == URL
    assert install(installed, url=URL, token=TOKEN, relay=RELAY) == installed

    assert state.other_relays == ((OLD_RELAY.interpreter, OLD_RELAY.data_dir),)

    restored, removed = uninstall(installed, data_dir=RELAY.data_dir)
    assert removed == len(ALL_EVENTS)
    assert restored == original
    assert inspect(original, url=URL, data_dir=RELAY.data_dir).events == ()


@pytest.mark.parametrize("older", [_legacy_relay, _isolated_cli_relay], ids=["m", "I-m-cli"])
def test_an_older_relay_for_any_data_dir_is_this_installs_to_replace(older: Any) -> None:
    """No current AgentTape writes the CLI-form relay, and without ``-I`` it runs modules
    from the session's directory: whatever data dir it names, setup replaces it, remove
    takes it out, and status flags it."""
    stale = older("/old/data")
    doc = {"hooks": {"SessionStart": [{"hooks": [stale]}]}}
    state = inspect(doc, url=URL, data_dir=RELAY.data_dir)
    assert state.outdated_relay_events == ("SessionStart",)
    assert state.other_relays == ()

    installed = install(doc, url=URL, token=TOKEN, relay=RELAY)
    assert _agenttape_hooks_of(installed, "SessionStart") == [relay_entry(RELAY, url=URL)]
    restored, removed = uninstall(doc, data_dir=RELAY.data_dir)
    assert removed == 1
    assert restored == {}


@pytest.mark.parametrize(
    "doc",
    [{}, {"env": {"FOO": "bar"}, "permissions": {"allow": []}}],
    ids=["empty", "user-keys-no-hooks"],
)
def test_uninstall_round_trip_no_prior_hooks(doc: dict[str, Any]) -> None:
    original = copy.deepcopy(doc)
    installed = install(doc, url=URL, token=TOKEN, relay=RELAY)
    restored, removed = uninstall(installed, data_dir=RELAY.data_dir)
    assert restored == original
    assert removed == len(ALL_EVENTS)


def test_uninstall_round_trip_with_user_hooks_and_other_events() -> None:
    doc = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
            ],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo prompt"}]}],
        }
    }
    original = copy.deepcopy(doc)
    installed = install(doc, url=URL, token=TOKEN, relay=RELAY)
    restored, removed = uninstall(installed, data_dir=RELAY.data_dir)
    assert restored == original
    assert removed == len(ALL_EVENTS)


def test_uninstall_on_a_never_installed_empty_hooks_object_is_a_noop() -> None:
    doc = {"hooks": {}}
    restored, removed = uninstall(copy.deepcopy(doc), data_dir=RELAY.data_dir)
    assert restored == doc
    assert removed == 0


def test_uninstall_drops_only_groups_and_events_it_emptied() -> None:
    doc = install({}, url=URL, token=TOKEN, relay=RELAY)
    doc["hooks"]["PreToolUse"].append(
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
    )
    restored, removed = uninstall(doc, data_dir=RELAY.data_dir)
    assert removed == len(ALL_EVENTS)
    # PreToolUse keeps the user's own group (not emptied by the strip) ...
    assert restored["hooks"]["PreToolUse"] == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
    ]
    # ... but every other, now-fully-emptied event is dropped entirely.
    assert set(restored["hooks"].keys()) == {"PreToolUse"}


def test_settings_shape_error_when_hooks_is_not_an_object() -> None:
    with pytest.raises(SettingsShapeError, match="hooks"):
        install({"hooks": []}, url=URL, token=TOKEN, relay=RELAY)


def test_settings_shape_error_when_an_event_value_is_not_a_list() -> None:
    with pytest.raises(SettingsShapeError, match="PreToolUse"):
        install({"hooks": {"PreToolUse": {}}}, url=URL, token=TOKEN, relay=RELAY)


def test_settings_shape_error_when_a_group_hooks_is_not_a_list() -> None:
    with pytest.raises(SettingsShapeError, match="hooks"):
        install(
            {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": "nope"}]}},
            url=URL,
            token=TOKEN,
            relay=RELAY,
        )


def test_inspect_reports_installed_events_url_timeouts_and_token() -> None:
    doc = install({}, url=URL, token=TOKEN, relay=RELAY)
    state = inspect(doc, url=URL, data_dir=RELAY.data_dir)
    assert set(state.events) == set(ALL_EVENTS)
    assert state.url == URL
    assert state.timeouts == (HOOK_TIMEOUT_SECONDS, RELAY_HOOK_TIMEOUT_SECONDS)
    assert state.token == TOKEN
    assert state.relay_interpreter == RELAY.interpreter
    assert state.legacy_http_events == ()
    assert state.disable_all_hooks is False
    assert state.url_blocked_by_allowlist is False


def test_inspect_on_an_uninstalled_doc_reports_nothing_installed() -> None:
    state = inspect({}, url=URL, data_dir=RELAY.data_dir)
    assert state.events == ()
    assert state.url is None
    assert state.token is None


def test_inspect_reports_disable_all_hooks() -> None:
    doc = install({"disableAllHooks": True}, url=URL, token=TOKEN, relay=RELAY)
    state = inspect(doc, url=URL, data_dir=RELAY.data_dir)
    assert state.disable_all_hooks is True


def test_inspect_reports_allowlist_blocking_with_fnmatch_wildcards() -> None:
    doc = install(
        {"allowedHttpHookUrls": ["http://127.0.0.1:9999/*"]}, url=URL, token=TOKEN, relay=RELAY
    )
    state = inspect(doc, url=URL, data_dir=RELAY.data_dir)
    assert state.url_blocked_by_allowlist is True

    doc2 = install(
        {"allowedHttpHookUrls": ["http://127.0.0.1:*/*"]}, url=URL, token=TOKEN, relay=RELAY
    )
    state2 = inspect(doc2, url=URL, data_dir=RELAY.data_dir)
    assert state2.url_blocked_by_allowlist is False


def test_hook_entry_shape() -> None:
    entry = hook_entry(URL, TOKEN)
    assert entry == {
        "type": "http",
        "url": URL,
        "timeout": HOOK_TIMEOUT_SECONDS,
        "headers": {
            "Authorization": f"Bearer {TOKEN}",
            "X-AgentTape-Run-Id": "${AGENTTAPE_RUN_ID}",
        },
        "allowedEnvVars": ["AGENTTAPE_RUN_ID"],
    }
