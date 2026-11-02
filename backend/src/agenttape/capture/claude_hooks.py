"""Claude Code ``http`` hook payload parsing.

Pure: no DB, no filesystem, no network. :func:`parse_hook` turns the JSON body Claude
Code's ``http`` hook posts into a :class:`HookEvent`; recording it is
``application/services/observed_sessions.py``'s job.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from agenttape.domain.enums import EventType
from agenttape.domain.events import EventDraft
from agenttape.findings.normalize import to_cat_n

#: Hook events AgentTape acts on. Others (e.g. ``UserPromptSubmit``, ``Notification``,
#: ``TeammateIdle``) are received (if ever registered) but ``parse_hook`` returns ``None``.
HOOK_EVENT_NAMES = frozenset(
    {"SessionStart", "SessionEnd", "PreToolUse", "PostToolUse", "PostToolUseFailure"}
)
#: Event source tag for every event this package builds.
HOOK_EVENT_SOURCE = "claude-code-hook"

MAX_SESSION_ID_CHARS = 200
MAX_CWD_CHARS = 4096
MAX_TOOL_NAME_CHARS = 200
MAX_TOOL_USE_ID_CHARS = 200

#: Fixed namespace for deterministic (uuid5) hook event ids: stable across a restart, so a
#: redelivered hook (or a backfilled PreToolUse) always resolves to the same event id.
_EVENT_ID_NAMESPACE = uuid.UUID("6f2a9b1e-6c8e-4c9b-9a8b-1f2a3c4d5e6f")

_TOOL_EVENT_NAMES = ("PreToolUse", "PostToolUse", "PostToolUseFailure")


class HookPayloadError(ValueError):
    """The hook body is not shaped like a Claude Code hook payload."""


@dataclass(frozen=True)
class HookEvent:
    name: str
    session_id: str
    cwd: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    tool_response: Any = None
    error: str | None = None
    is_interrupt: bool | None = None
    #: SessionStart's ``source`` (startup | resume | clear | compact | fork).
    start_source: str | None = None
    #: SessionEnd's ``reason`` (clear | resume | logout | prompt_input_exit | other).
    end_reason: str | None = None
    model: str | None = None
    #: A validated ``X-AgentTape-Run-Id`` header claim - only set when it is non-empty
    #: after stripping, contains no ``$`` (an unresolved Claude Code template variable),
    #: and parses as a UUID; anything else is treated as absent.
    claimed_run_id: str | None = None


def _str_field(body: dict[str, Any], key: str, *, max_len: int) -> str | None:
    value = body.get(key)
    if not isinstance(value, str) or not value or len(value) > max_len:
        return None
    return value


def is_absolute_path(value: str) -> bool:
    """POSIX absolute path or a Windows drive path (``C:\\...`` / ``C:/...``), with no
    control character (NUL included) anywhere in it: a NUL makes path resolution fail,
    and no real working directory contains one."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return False
    if value.startswith("/"):
        return True
    return len(value) >= 3 and value[1] == ":" and value[2] in "\\/"


def _claimed_run_id(header_value: str | None) -> str | None:
    """A ``X-AgentTape-Run-Id`` header value, validated - or ``None`` when it is empty,
    still carries an unresolved ``$...`` template variable (Claude Code's own behavior
    for a name not in ``allowedEnvVars``), or is not a canonical UUID."""
    if header_value is None:
        return None
    value = header_value.strip()
    if not value or "$" in value:
        return None
    try:
        uuid.UUID(value)
    except ValueError:
        return None
    return value


def parse_hook(body: object, *, claimed_run_id: str | None = None) -> HookEvent | None:
    """Parse a Claude Code hook POST body.

    Raises :class:`HookPayloadError` for a body that is not shaped like a hook payload at
    all (not a dict, missing/non-string ``hook_event_name``, or a bad/missing required
    field for the events AgentTape acts on). Returns ``None`` for a hook event name
    AgentTape does not act on: fail-open, because hooks never gate a tool call.

    ``claimed_run_id`` is the request's ``X-AgentTape-Run-Id`` header, validated by
    :func:`_claimed_run_id` (a token holder cannot attach to an arbitrary run merely by
    naming it: :class:`~agenttape.application.services.observed_sessions.ObservedSessionService`
    still requires that run to be active, agent-kind, and claude-code).
    """
    if not isinstance(body, dict):
        raise HookPayloadError("hook body must be a JSON object")
    name = body.get("hook_event_name")
    if not isinstance(name, str) or not name:
        raise HookPayloadError("hook body must carry a string 'hook_event_name'")
    if name not in HOOK_EVENT_NAMES:
        return None

    session_id = _str_field(body, "session_id", max_len=MAX_SESSION_ID_CHARS)
    if session_id is None:
        raise HookPayloadError("hook body must carry a string 'session_id' (1-200 chars)")

    cwd_raw = body.get("cwd")
    cwd: str | None = None
    if cwd_raw is not None:
        if (
            not isinstance(cwd_raw, str)
            or not cwd_raw
            or len(cwd_raw) > MAX_CWD_CHARS
            or not is_absolute_path(cwd_raw)
        ):
            raise HookPayloadError("'cwd' must be an absolute path string (at most 4096 chars)")
        cwd = cwd_raw

    tool_name: str | None = None
    tool_use_id: str | None = None
    tool_input: dict[str, Any] | None = None
    if name in _TOOL_EVENT_NAMES:
        tool_name = _str_field(body, "tool_name", max_len=MAX_TOOL_NAME_CHARS)
        tool_use_id = _str_field(body, "tool_use_id", max_len=MAX_TOOL_USE_ID_CHARS)
        if tool_name is None or tool_use_id is None:
            raise HookPayloadError(f"{name} needs string 'tool_name' and 'tool_use_id'")
        raw_input = body.get("tool_input")
        if raw_input is not None and not isinstance(raw_input, dict):
            raise HookPayloadError(f"{name} 'tool_input' must be a JSON object")
        tool_input = raw_input if isinstance(raw_input, dict) else {}

    error = body.get("error")
    is_interrupt = body.get("is_interrupt")
    start_source = body.get("source")
    end_reason = body.get("reason")
    model = body.get("model")
    return HookEvent(
        name=name,
        session_id=session_id,
        cwd=cwd,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_use_id=tool_use_id,
        tool_response=body.get("tool_response"),
        error=error if isinstance(error, str) else None,
        is_interrupt=is_interrupt if isinstance(is_interrupt, bool) else None,
        start_source=start_source if isinstance(start_source, str) else None,
        end_reason=end_reason if isinstance(end_reason, str) else None,
        model=model if isinstance(model, str) else None,
        claimed_run_id=_claimed_run_id(claimed_run_id),
    )


def session_hash(session_id: str) -> str:
    """``sha256:<hex>`` of the raw session id - never the raw id itself is stored."""
    return "sha256:" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def hook_event_ids(run_id: str, tool_use_id: str) -> tuple[str, str]:
    """Deterministic (uuid5) ``(call_id, result_id)`` for one ``tool_use_id`` on one run.

    A redelivered hook, or a PostToolUse whose PreToolUse was missed, always resolves to
    the same ids - so recording is naturally idempotent without a JSON query.
    """
    base = f"{run_id}:{tool_use_id}"
    call_id = str(uuid.uuid5(_EVENT_ID_NAMESPACE, base + ":call"))
    result_id = str(uuid.uuid5(_EVENT_ID_NAMESPACE, base + ":result"))
    return call_id, result_id


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def tool_output_text(
    tool_name: str, tool_input: dict[str, Any], tool_response: Any
) -> tuple[str, str, dict[str, Any]]:
    """``(text, response_shape, extra)`` for a hook's ``tool_response``.

    ``Read`` renders the same ``cat -n`` layout Claude Code's own Read tool result uses
    (shape ``"read_file"``) so hook-recorded reads are verifiable the same way as
    stream-json reads (``context_evidence.normalize`` parses the numbered layout back out
    of either). ``Bash`` surfaces ``stdout`` as the text (shape ``"bash"``), carrying
    non-empty ``stderr`` and a boolean ``interrupted`` in ``extra`` so they land in the
    TOOL_RESULT payload. A plain string is used as is (``"text"``); ``None`` becomes an
    empty ``"none"``-shaped result; anything else not covered above is sorted JSON
    (``"json"``) - the documented and undocumented shapes both degrade to readable text.
    """
    if isinstance(tool_response, dict):
        if tool_name == "Read":
            file_obj = tool_response.get("file")
            if isinstance(file_obj, dict) and isinstance(file_obj.get("content"), str):
                start = _positive_int(file_obj.get("startLine"))
                if start is None:
                    start = _positive_int(tool_input.get("offset")) or 1
                lines = file_obj["content"].splitlines()
                text = to_cat_n(lines, start=start)
                return text, "read_file", {}
        if tool_name == "Bash":
            stdout = tool_response.get("stdout")
            if isinstance(stdout, str):
                extra: dict[str, Any] = {}
                stderr = tool_response.get("stderr")
                if isinstance(stderr, str) and stderr:
                    extra["stderr"] = stderr
                interrupted = tool_response.get("interrupted")
                if isinstance(interrupted, bool):
                    extra["interrupted"] = interrupted
                return stdout, "bash", extra
        return json.dumps(tool_response, sort_keys=True, default=str), "json", {}
    if tool_response is None:
        return "", "none", {}
    if isinstance(tool_response, str):
        return tool_response, "text", {}
    return json.dumps(tool_response, sort_keys=True, default=str), "json", {}


def _tool_target(tool_input: dict[str, Any]) -> str:
    for key in ("file_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    command = tool_input.get("command")
    if isinstance(command, str) and command:
        return command[:80]
    return ""


def tool_drafts(
    hook: HookEvent, *, run_id: str, attempt_id: str, call_exists: bool
) -> list[EventDraft]:
    """``PreToolUse`` -> one ``tool_call``; ``PostToolUse``/``PostToolUseFailure`` -> the
    ``tool_call`` first when ``call_exists`` is False, then one ``tool_result`` (linked by
    ``parent_event_id``). Every draft carries ``source=HOOK_EVENT_SOURCE``."""
    if hook.tool_name is None or hook.tool_use_id is None:
        raise ValueError("tool_drafts needs a tool hook (tool_name and tool_use_id set)")
    call_id, result_id = hook_event_ids(run_id, hook.tool_use_id)
    target = _tool_target(hook.tool_input or {})
    summary = f"{hook.tool_name}({target})" if target else hook.tool_name
    call_payload = {
        "tool": hook.tool_name,
        "input": hook.tool_input or {},
        "tool_use_id": hook.tool_use_id,
        "cwd": hook.cwd,
    }
    drafts: list[EventDraft] = []
    if hook.name == "PreToolUse":
        drafts.append(
            EventDraft(
                type=EventType.TOOL_CALL,
                summary=summary,
                payload=call_payload,
                source=HOOK_EVENT_SOURCE,
                attempt_id=attempt_id,
                id=call_id,
            )
        )
        return drafts

    # PostToolUse / PostToolUseFailure
    if not call_exists:
        drafts.append(
            EventDraft(
                type=EventType.TOOL_CALL,
                summary=summary,
                payload=call_payload,
                source=HOOK_EVENT_SOURCE,
                attempt_id=attempt_id,
                id=call_id,
            )
        )
    is_error = hook.name == "PostToolUseFailure"
    extra: dict[str, Any] = {}
    if is_error:
        output = hook.error or ""
        response_shape = "error"
        if hook.is_interrupt is not None:
            extra["is_interrupt"] = hook.is_interrupt
    else:
        output, response_shape, extra = tool_output_text(
            hook.tool_name, hook.tool_input or {}, hook.tool_response
        )
    first_line = output.strip().splitlines()[0][:200] if output.strip() else "(empty)"
    drafts.append(
        EventDraft(
            type=EventType.TOOL_RESULT,
            summary=("Tool error: " if is_error else "Tool result: ") + first_line,
            payload={
                "tool_use_id": hook.tool_use_id,
                "output": output,
                "is_error": is_error,
                "response_shape": response_shape,
                **extra,
            },
            source=HOOK_EVENT_SOURCE,
            attempt_id=attempt_id,
            parent_event_id=call_id,
            id=result_id,
        )
    )
    return drafts
