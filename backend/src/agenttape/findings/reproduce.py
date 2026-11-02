"""What may be replayed, and how a re-run compares to the recording.

Pure module: no ``Session``, no I/O, no subprocess. ``ReproductionService``
(``application/services/reproductions.py``) is the only caller that touches a database or
starts a process; this module only decides:

* :func:`recorded_command` - the argv/cwd/exit_code/output a citation's resolved event
  actually recorded, from a ``command`` event's payload only - never the finding's own
  prose about what it ran.
* :func:`classify` - ``"search"`` (grep/egrep/fgrep/rg, or ``git grep`` - read-only, whose
  output is compared line-for-line) or ``"command"`` (everything else - compared by exit
  status only, since a build or test command's output is not a stable thing to diff).
* :func:`git_refusal` - a named refusal (and nothing runs) for a git invocation this
  module never replays, whatever the command policy says: a git subcommand outside the
  small set that is read-only regardless of its flags (:data:`GIT_REPLAYABLE_SUBCOMMANDS`),
  a ``git-<sub>`` helper binary, or git run through another program's arguments.
* :func:`unsafe_argument` - a reason (and nothing runs) for an argv this module refuses to
  replay even when the command policy would allow it: any :func:`git_refusal`, a program
  named by a path rather than a bare name, a redacted value it cannot compare honestly, a
  path outside the repository, or an ``rg``/``git grep`` option that can make the replayed
  program run another program of its own.
* :func:`compare` - ``reproduced`` / ``not_reproduced`` (with a one-sentence, honest
  reason) from a re-run's exit code and output against what was recorded.

Reason strings here never contain the words token, secret, password, credential,
authorization, api_key, access_key, or private_key: the before_flush redaction guard masks
values under such keys, and a masked reason would misrepresent what was actually checked.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from agenttape.domain.policy import parse_git
from agenttape.findings.normalize import has_redaction, normalize

#: The queue payload ``run_kind`` the worker dispatches to
#: :class:`agenttape.worker.executors.citation_reproduction.CitationReproductionExecutor`.
REPRODUCTION_KIND = "citation_reproduction"

#: A ``tool_call``'s ``tool`` name :func:`recorded_command` extracts a command from - a
#: TOOL_CALL/TOOL_RESULT pair for any other tool (``Read``, an MCP tool, ``web_search``, ...)
#: is never a recorded command to replay.
COMMAND_TOOLS = ("Bash", "shell")

#: Refused outright, before :func:`shlex.split` ever runs: any of these means the recorded
#: string relies on a shell to mean what it meant when it ran, and AgentTape never starts
#: one to replay it.
_SHELL_SYNTAX_CHARS = "|&;<>(){}$`\\*?[]~!#"

#: A recorded command longer than this is refused outright, unread by the tokenizer.
_MAX_COMMAND_STRING_CHARS = 2000

#: A leading ``NAME=value`` (a shell would set the variable for the command that follows;
#: AgentTape never would).
_LEADING_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: A read-only search argv is bounded tightly; a test/build command may legitimately run
#: much longer.
SEARCH_TIMEOUT_SECONDS = 120
COMMAND_TIMEOUT_SECONDS = 600

#: How much of a re-run's redacted, combined output is kept in ``output_excerpt``.
OUTPUT_EXCERPT_CHARS = 4000

_SEARCH_EXECUTABLES = frozenset({"grep", "egrep", "fgrep", "rg"})

#: ``rg`` options that can make ``rg`` run another program (a decompressor, a pager, a
#: hostname resolver command) - never replayed, even when the command policy allows ``rg``.
RG_PROGRAM_OPTIONS: tuple[str, ...] = (
    "--pre",
    "--pre-glob",
    "-z",
    "--search-zip",
    "--hostname-bin",
)
#: The single short letter among the above that can also appear inside a combined short
#: option cluster (``-z``, e.g. ``-vz``).
_RG_PROGRAM_CLUSTER_LETTER = "z"

#: ``git grep`` options that can make git run another program (a pager, a diff filter).
GIT_GREP_PROGRAM_OPTIONS: tuple[str, ...] = ("-O", "--open-files-in-pager", "--textconv")
_GIT_GREP_PROGRAM_CLUSTER_LETTER = "O"

#: The only git subcommands a re-run ever replays: each is read-only whatever flags it is
#: given (``git grep`` is also the search family's own git member). Every other subcommand -
#: ``worktree``, ``branch``, ``tag``, ``stash``, ``remote``, ``config``, ``add``,
#: ``checkout``, ``reset``, ``commit``, an alias - is refused before the command policy is
#: consulted, so a re-run never depends on the policy's own git classification to stay
#: read-only. The snapshot a re-run executes in is a worktree of the run's *real*
#: repository, sharing its refs and config, so a replayed ``git branch <name>`` or ``git
#: worktree add`` would change that repository.
GIT_REPLAYABLE_SUBCOMMANDS = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "rev-parse",
        "ls-files",
        "cat-file",
        "blame",
        "describe",
        "shortlog",
        "grep",
    }
)

#: :attr:`Refusal.code` for a git subcommand outside :data:`GIT_REPLAYABLE_SUBCOMMANDS`.
GIT_SUBCOMMAND_NOT_REPLAYABLE = "git_subcommand_not_replayable"
#: :attr:`Refusal.code` for any git global option before the subcommand (``-C <dir>``,
#: ``--git-dir``, ``--work-tree``, ``-c key=value``, ``--exec-path``, ``--namespace``...):
#: each can change which repository, configuration, or program the command uses.
GIT_GLOBAL_OPTION_NOT_REPLAYABLE = "git_global_option_not_replayable"
#: :attr:`Refusal.code` for a ``git-<sub>`` helper binary, or git named inside another
#: program's arguments (``npm exec git tag v1``, ``env git branch x``).
GIT_INDIRECT_NOT_REPLAYABLE = "git_indirect_not_replayable"


@dataclass(frozen=True)
class Refusal:
    """A named could-not-run reason: ``code`` is stable, ``reason`` is the stored sentence."""

    code: str
    reason: str


def _is_git_name(name: str) -> bool:
    return name == "git" or (name.startswith("git-") and len(name) > 4)


def _names_git_program(token: str) -> bool:
    """``git``, ``git-<sub>``, or an absolute path to either - a program another program
    could run. A relative path such as ``tests/git`` is a directory argument, not a program."""
    if "/" not in token:
        return _is_git_name(token)
    return token.startswith("/") and _is_git_name(PurePosixPath(token).name)


@dataclass(frozen=True)
class RecordedCommand:
    """What a ``command`` event recorded about the argv AgentTape ran."""

    argv: tuple[str, ...]
    cwd: str | None
    cwd_known: bool
    exit_code: int | None
    failed: bool | None
    output: str | None
    output_truncated: bool
    source: str


def argv_from_command_string(text: str) -> list[str] | str:
    """The argv a recorded command *string* tokenizes to, or a could-not-run reason.

    Never starts a shell: every character a shell would treat specially is refused before
    :func:`shlex.split` (``posix=True``) ever runs, so the tokenizer only ever sees a string
    already known to contain none of them - this function itself never raises on shell
    syntax, only on an unbalanced quote inside an otherwise plain argument.
    """
    if not text:
        return "The recorded command is empty, so there is nothing to replay."
    if len(text) > _MAX_COMMAND_STRING_CHARS:
        return (
            f"The recorded command is longer than {_MAX_COMMAND_STRING_CHARS} characters, "
            "so AgentTape does not replay it."
        )
    if "\n" in text or "\r" in text:
        return (
            "The recorded command contains a newline, which AgentTape does not replay "
            "without a shell."
        )
    for ch in _SHELL_SYNTAX_CHARS:
        if ch in text:
            return (
                f"The recorded command uses shell syntax ({ch!r}) that AgentTape does not "
                "replay without a shell."
            )
    if _LEADING_ASSIGNMENT_RE.match(text.lstrip()):
        return (
            "The recorded command begins with a variable assignment, which AgentTape does "
            "not replay without a shell."
        )
    try:
        argv = shlex.split(text, posix=True)
    except ValueError:
        return "The recorded command has an unbalanced quote, so AgentTape does not replay it."
    if not argv:
        return "The recorded command is empty, so there is nothing to replay."
    return argv


def _outcome_from_result(
    result: Mapping[str, Any] | None,
) -> tuple[int | None, bool | None, str | None, bool]:
    """``(exit_code, failed, output, output_truncated)`` from a TOOL_RESULT payload, or the
    all-unknown tuple when there is none."""
    if result is None:
        return None, None, None, False
    raw_exit_code = result.get("exit_code")
    exit_code = (
        raw_exit_code
        if isinstance(raw_exit_code, int) and not isinstance(raw_exit_code, bool)
        else None
    )
    raw_is_error = result.get("is_error")
    failed = raw_is_error if isinstance(raw_is_error, bool) else None
    raw_output = result.get("output")
    output = raw_output if isinstance(raw_output, str) else None
    output_truncated = bool(result.get("output_truncated")) or bool(result.get("truncated"))
    return exit_code, failed, output, output_truncated


def recorded_command(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    call_payload: Mapping[str, Any] | None = None,
    result_payload: Mapping[str, Any] | None = None,
) -> RecordedCommand | str:
    """The :class:`RecordedCommand` a resolved event names, or a could-not-run reason.

    A string return is the reason nothing can be replayed - never the finding's own text.
    Three shapes back a reproduction:

    * a ``command`` event (``payload`` alone) - argv, cwd, exit code and output all
      recorded directly on the event.
    * a ``tool_call`` event whose ``tool`` is in :data:`COMMAND_TOOLS` (``payload`` is the
      call; ``result_payload`` is its paired TOOL_RESULT, when known) - argv tokenized from
      the call's own recorded command string; outcome from the result.
    * a ``tool_result`` event whose parent call's ``tool`` is in :data:`COMMAND_TOOLS`
      (``payload`` is the result; ``call_payload`` is its paired TOOL_CALL) - the same argv;
      outcome from this event's own payload.

    Neither tool-call shape ever records a working directory (the Claude Code and Codex
    adapters do not capture one), so ``cwd_known`` is always ``False`` for either.
    """
    if event_type == "command":
        raw_argv = payload.get("argv")
        if (
            not isinstance(raw_argv, list)
            or not raw_argv
            or not all(isinstance(a, str) for a in raw_argv)
        ):
            return "the cited event has no recorded command to re-run"
        raw_cwd = payload.get("cwd")
        cwd_known = isinstance(raw_cwd, str)
        exit_code, failed, output, output_truncated = _outcome_from_result(payload)
        source = str(payload.get("tool") or "command")
        return RecordedCommand(
            argv=tuple(raw_argv),
            cwd=raw_cwd if cwd_known else None,
            cwd_known=cwd_known,
            exit_code=exit_code,
            failed=failed,
            output=output,
            output_truncated=output_truncated,
            source=source,
        )

    call: Mapping[str, Any] | None
    result: Mapping[str, Any] | None
    if event_type == "tool_call":
        call, result = payload, result_payload
    elif event_type == "tool_result":
        call, result = call_payload, payload
    else:
        return "the cited event is not a recorded command"

    if call is None:
        return "the cited event has no recorded command to re-run"
    tool = call.get("tool")
    if tool not in COMMAND_TOOLS:
        return "the cited event is not a recorded command"
    raw_command = (call.get("input") or {}).get("command")
    if not isinstance(raw_command, str) or not raw_command:
        return "the cited event has no recorded command to re-run"
    argv_or_reason = argv_from_command_string(raw_command)
    if isinstance(argv_or_reason, str):
        return argv_or_reason
    exit_code, failed, output, output_truncated = _outcome_from_result(result)
    return RecordedCommand(
        argv=tuple(argv_or_reason),
        cwd=None,
        cwd_known=False,
        exit_code=exit_code,
        failed=failed,
        output=output,
        output_truncated=output_truncated,
        source=str(tool),
    )


def classify(argv: Sequence[str]) -> str:
    """``"search"`` for grep/egrep/fgrep/rg or ``git grep``; ``"command"`` otherwise."""
    if not argv:
        return "command"
    exe = PurePosixPath(argv[0]).name
    if exe in _SEARCH_EXECUTABLES:
        return "search"
    if exe == "git" and parse_git(argv).sub == "grep":
        return "search"
    return "command"


def _looks_like_outside_path(value: str) -> bool:
    """An absolute path, a ``~`` path, or a path with a ``..`` segment."""
    if value.startswith("~"):
        return True
    candidate = PurePosixPath(value)
    if candidate.is_absolute():
        return True
    return ".." in candidate.parts


def _cluster_hit(arg: str, letter: str) -> bool:
    """``letter`` inside a combined short-option cluster (``-vz`` contains ``z``)."""
    return bool(letter) and arg.startswith("-") and not arg.startswith("--") and letter in arg[1:]


def _program_option_hit(arg: str, names: tuple[str, ...], cluster_letter: str) -> str | None:
    name = arg.partition("=")[0]
    if name in names:
        return name
    if _cluster_hit(arg, cluster_letter):
        return f"-{cluster_letter}"
    return None


def git_refusal(argv: Sequence[str], mode: str) -> Refusal | None:
    """The named reason a git invocation is never replayed, or ``None``.

    Decided here, before the command policy, and never delegated to it, for every mode: a
    re-run executes inside a worktree of the run's real repository, so only a git
    subcommand that is read-only whatever its flags (:data:`GIT_REPLAYABLE_SUBCOMMANDS`),
    with no global option before it, is ever replayed. A
    ``git-<sub>`` helper binary is refused outright, and so - for a non-search command - is
    any argument that names git itself, since the program before it (``env``, ``npm exec``,
    ``xargs``...) would run that git invocation without this check ever seeing its
    subcommand. A search's own arguments are patterns and paths, so the word ``git`` there
    is never mistaken for a program.
    """
    if not argv:
        return None
    exe = PurePosixPath(argv[0]).name
    if exe == "git":
        inv = parse_git(argv)
        if inv.global_opts:
            return Refusal(
                GIT_GLOBAL_OPTION_NOT_REPLAYABLE,
                "Global git options before the subcommand are not replayed, since they can "
                "change which repository, settings, or program the command uses.",
            )
        sub = inv.sub
        if sub not in GIT_REPLAYABLE_SUBCOMMANDS:
            return Refusal(
                GIT_SUBCOMMAND_NOT_REPLAYABLE,
                f"git {sub or '(no subcommand)'} is not one of the read-only git "
                "subcommands AgentTape replays, so it is not re-run; a re-run never "
                "changes a repository.",
            )
        return None
    if _is_git_name(exe):
        return Refusal(
            GIT_INDIRECT_NOT_REPLAYABLE,
            f"{exe} is a git helper program, which AgentTape does not replay; a re-run "
            "never changes a repository.",
        )
    if mode != "search" and any(_names_git_program(arg) for arg in argv[1:]):
        return Refusal(
            GIT_INDIRECT_NOT_REPLAYABLE,
            f"The recorded command runs git through {exe}, which AgentTape does not "
            "replay; a re-run never changes a repository.",
        )
    return None


def unsafe_argument(argv: Sequence[str], mode: str) -> str | None:
    """A reason (and nothing runs) an argv this module refuses to replay, or ``None``.

    Checked regardless of what the command policy would otherwise allow: a git invocation
    :func:`git_refusal` refuses is never replayed; a program named by a path (absolute,
    ``~``-relative, ``./``, or ``../``) is never replayed either, since the run's own agent
    could have written it. Only a bare program name is ever re-run, and the service then
    looks it up in the system directories only (``security.executables.REPLAY_PATH``,
    never the worker's inherited ``PATH``); a redacted value cannot be compared honestly;
    a path outside the repository is out of scope for a snapshot pinned to the run's own commit; a search
    option that can run another program of its own (a pager, a decompressor, a pre-filter)
    defeats the "only the recorded argv runs" guarantee.
    """
    if not argv:
        return None
    refusal = git_refusal(argv, mode)
    if refusal is not None:
        return refusal.reason
    if "/" in argv[0]:
        return (
            "The recorded program is named by a path; AgentTape re-runs only a bare "
            "program name, looked up in the system directories (/usr/local/bin, /usr/bin, "
            "/bin), never a program at a path the agent could have written."
        )
    for arg in argv[1:]:
        if has_redaction(arg) or "[secret:" in arg:
            return (
                "The recorded command contains redacted values, so it cannot be replayed exactly."
            )
    for arg in argv[1:]:
        value = arg.partition("=")[2] if arg.startswith("-") and "=" in arg else arg
        if _looks_like_outside_path(value):
            return "An argument names a path outside the repository."

    # Keyed on the executable, never on ``mode``: a program-running option is refused
    # whichever way the argv was classified.
    exe = PurePosixPath(argv[0]).name
    if exe == "git" and parse_git(argv).sub == "grep":
        inv = parse_git(argv)
        for arg in inv.rest:
            hit = _program_option_hit(
                arg, GIT_GREP_PROGRAM_OPTIONS, _GIT_GREP_PROGRAM_CLUSTER_LETTER
            )
            if hit:
                return (
                    f"The search option {hit} can run another program, so AgentTape does "
                    "not replay it."
                )
    elif exe == "rg":
        for arg in argv[1:]:
            hit = _program_option_hit(arg, RG_PROGRAM_OPTIONS, _RG_PROGRAM_CLUSTER_LETTER)
            if hit:
                return (
                    f"The search option {hit} can run another program, so AgentTape does "
                    "not replay it."
                )
    return None


def comparable(mode: str, recorded: RecordedCommand) -> str | None:
    """A reason ``recorded`` cannot be compared honestly, or ``None`` when it can.

    Checked before anything runs, so :class:`~agenttape.application.services.reproductions.
    ReproductionService` never starts a process only to discover afterwards that the
    recorded side offers nothing solid to compare against. A recorded result that reports an
    error with no exit code is refused for a search comparison specifically: its output
    format is unknown (a tool's own error text, not necessarily the search's stdout), so the
    honest outcome is ``could_not_run``, never a silently wrong ``not_reproduced``.
    """
    if mode == "search":
        if not recorded.output or recorded.output_truncated:
            return (
                "The recorded output was cut before AgentTape stored it, so it cannot be compared."
            )
        if recorded.exit_code is None and recorded.failed:
            return (
                "The recorded result reports an error but no exit code was recorded, so "
                "its output format is unknown and cannot be compared."
            )
        return None
    if recorded.exit_code is None and recorded.failed is None:
        return "No exit status was recorded for this command, so it cannot be compared."
    return None


def _exit_matches(recorded: RecordedCommand, exit_code: int | None) -> bool:
    if recorded.exit_code is not None:
        return exit_code == recorded.exit_code
    if recorded.failed is not None:
        return (exit_code != 0) == recorded.failed
    return False


def _recorded_status(recorded: RecordedCommand) -> str:
    if recorded.exit_code is not None:
        return f"exit code {recorded.exit_code}"
    if recorded.failed is not None:
        return "a failure" if recorded.failed else "success"
    return "nothing"


def _sorted_nonblank_lines(text: str) -> list[str]:
    return sorted(line for line in normalize(text).split("\n") if line.strip())


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def compare(
    mode: str,
    recorded: RecordedCommand,
    *,
    exit_code: int | None,
    output: str,
) -> tuple[str, str, dict[str, Any]]:
    """``(result, reason, details)`` from a re-run against what was recorded.

    A search re-run compares the exit code and the sorted, normalized, non-blank output
    lines (order-independent, so a filesystem's own directory-listing order never causes a
    false ``not_reproduced``); a command re-run compares only the exit status, since a test
    or build command's own stdout/stderr is not a stable thing to diff line-for-line.
    """
    details: dict[str, Any] = {}
    if mode == "search":
        recorded_lines = _sorted_nonblank_lines(recorded.output or "")
        rerun_lines = _sorted_nonblank_lines(output)
        details["recorded_line_count"] = len(recorded_lines)
        details["rerun_line_count"] = len(rerun_lines)
        if not _exit_matches(recorded, exit_code):
            return (
                "not_reproduced",
                f"Exit code {exit_code} now, {_recorded_status(recorded)} recorded.",
                details,
            )
        if recorded_lines == rerun_lines:
            return (
                "reproduced",
                f"Same exit code and the same {_plural(len(recorded_lines), 'output line')} "
                "at the pinned commit.",
                details,
            )
        differ = len(set(recorded_lines) ^ set(rerun_lines))
        return (
            "not_reproduced",
            f"{_plural(len(rerun_lines), 'output line')} now, {len(recorded_lines)} "
            f"recorded; {differ} differ.",
            details,
        )
    if not _exit_matches(recorded, exit_code):
        return (
            "not_reproduced",
            f"Exit code {exit_code} now, {_recorded_status(recorded)} recorded.",
            details,
        )
    return "reproduced", "Same exit code at the pinned commit.", details
