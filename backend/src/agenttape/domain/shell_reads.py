"""Classifies a shell command string as a read-only file read for evidence purposes only.

The string is parsed, never executed, and never fed to ``domain.policy.check_command`` or
a process. The grammar is deliberately narrow (bounded to ``cat``, ``head``,
``tail``, and ``sed -n '<a>[,<b>|$]p'`` with no pipes, redirects, substitutions,
variables, globs, or env assignments): anything outside it returns ``None``, exactly as
if the command had never been seen.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any

#: Programs this module knows how to classify as reads.
SHELL_READ_PROGRAMS = frozenset({"cat", "head", "tail", "sed"})

#: Any of these characters (or a length over the cap) rejects the command before parsing.
_REJECT_CHARS = frozenset("|&;<>`$(){}*?[]~!#\\\n\r")
_MAX_COMMAND_LENGTH = 2000
_MAX_PATH_LENGTH = 4096
_MAX_CAT_PATHS = 16
_MAX_LINE = 10_000_000
_PROGRAM_PREFIXES = ("/bin/", "/usr/bin/")

_SED_SCRIPT_RE = re.compile(r"^(\d+)(,(\d+|\$))?p$")


@dataclass(frozen=True)
class ShellRead:
    """A bounded read-only shell command, as evidence: never executed, only described."""

    program: str
    paths: tuple[str, ...]
    line_start: int | None = None
    line_end: int | None = None
    whole_file: bool = False
    from_end: int | None = None
    numbered: bool = False

    def as_details(self) -> dict[str, Any]:
        """The fields shared verbatim by both callers' evidence ``details``."""
        details: dict[str, Any] = {"program": self.program}
        if self.line_start is not None:
            details["line_start"] = self.line_start
        if self.line_end is not None:
            details["line_end"] = self.line_end
        if self.whole_file:
            details["whole_file"] = True
        if self.from_end is not None:
            details["from_end"] = self.from_end
        if self.numbered:
            details["numbered"] = True
        return details


def classify_shell_read(command: object) -> ShellRead | None:
    """``command`` if (and only if) it is a bounded read-only shell read; else ``None``.

    Never raises: any input outside the grammar - malformed, too long, ambiguous, or
    simply not one of the recognized forms - yields ``None``.
    """
    if not isinstance(command, str):
        return None
    if not command or len(command) > _MAX_COMMAND_LENGTH:
        return None
    if _has_unquoted_reject_char(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    program = _resolve_program(tokens[0])
    if program is None:
        return None
    args = tokens[1:]
    if program == "cat":
        return _classify_cat(args)
    if program == "head":
        return _classify_head(args)
    if program == "tail":
        return _classify_tail(args)
    if program == "sed":
        return _classify_sed(args)
    return None


def _has_unquoted_reject_char(command: str) -> bool:
    """True when a reject character appears outside a single-quoted region.

    Single quotes make their contents fully literal in POSIX shells (no expansion, no
    metacharacters), which is what lets a bounded ``sed -n '<a>,<b>p'`` script use ``$``
    for "to end of file" without it being a shell-expansion risk. Everything else - bare
    text and double-quoted text (where ``$`` and backticks still expand) - is checked in
    full.
    """
    in_single_quote = False
    for ch in command:
        if in_single_quote:
            if ch == "'":
                in_single_quote = False
            continue
        if ch == "'":
            in_single_quote = True
            continue
        if ch in _REJECT_CHARS:
            return True
    return False


def _resolve_program(exe: str) -> str | None:
    if exe in SHELL_READ_PROGRAMS:
        return exe
    for prefix in _PROGRAM_PREFIXES:
        if exe.startswith(prefix) and exe[len(prefix) :] in SHELL_READ_PROGRAMS:
            return exe[len(prefix) :]
    return None


def _valid_path(path: str, *, allow_dash: bool = False) -> bool:
    if not path or len(path) > _MAX_PATH_LENGTH or "\x00" in path:
        return False
    return not (not allow_dash and path.startswith("-"))


def _positive_int(text: str) -> int | None:
    if not text.isdigit():
        return None
    value = int(text)
    if value <= 0 or value > _MAX_LINE:
        return None
    return value


def _classify_cat(args: list[str]) -> ShellRead | None:
    numbered = False
    paths: list[str] = []
    after_dashdash = False
    for a in args:
        if not after_dashdash and a == "--":
            after_dashdash = True
            continue
        if not after_dashdash and a.startswith("-"):
            if a == "-n":
                numbered = True
                continue
            return None
        if not _valid_path(a, allow_dash=after_dashdash):
            return None
        paths.append(a)
    if not (1 <= len(paths) <= _MAX_CAT_PATHS):
        return None
    return ShellRead(program="cat", paths=tuple(paths), whole_file=True, numbered=numbered)


def _classify_head(args: list[str]) -> ShellRead | None:
    n = 10
    path: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--lines="):
            value = _positive_int(a[len("--lines=") :])
            if value is None:
                return None
            n = value
            i += 1
            continue
        if a == "-n":
            if i + 1 >= len(args):
                return None
            value = _positive_int(args[i + 1])
            if value is None:
                return None
            n = value
            i += 2
            continue
        if a.startswith("-n") and len(a) > 2:
            value = _positive_int(a[2:])
            if value is None:
                return None
            n = value
            i += 1
            continue
        if a.startswith("-") and len(a) > 1 and a[1:].isdigit():
            value = _positive_int(a[1:])
            if value is None:
                return None
            n = value
            i += 1
            continue
        if a.startswith("-"):
            return None
        if path is not None:
            return None
        if not _valid_path(a):
            return None
        path = a
        i += 1
    if path is None:
        return None
    return ShellRead(program="head", paths=(path,), line_start=1, line_end=n)


def _tail_count(value: str) -> tuple[int | None, int | None] | None:
    """``(from_end, line_start)``, exactly one populated, for a tail ``-n`` value."""
    if value.startswith("+"):
        start = _positive_int(value[1:])
        return None if start is None else (None, start)
    from_end = _positive_int(value)
    return None if from_end is None else (from_end, None)


def _classify_tail(args: list[str]) -> ShellRead | None:
    from_end: int | None = 10
    line_start: int | None = None
    path: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--lines="):
            parsed = _tail_count(a[len("--lines=") :])
            if parsed is None:
                return None
            from_end, line_start = parsed
            i += 1
            continue
        if a == "-n":
            if i + 1 >= len(args):
                return None
            parsed = _tail_count(args[i + 1])
            if parsed is None:
                return None
            from_end, line_start = parsed
            i += 2
            continue
        if a.startswith("-n") and len(a) > 2:
            parsed = _tail_count(a[2:])
            if parsed is None:
                return None
            from_end, line_start = parsed
            i += 1
            continue
        if a.startswith("-") and len(a) > 1 and a[1:].isdigit():
            from_end = _positive_int(a[1:])
            if from_end is None:
                return None
            line_start = None
            i += 1
            continue
        if a.startswith("-"):
            return None
        if path is not None:
            return None
        if not _valid_path(a):
            return None
        path = a
        i += 1
    if path is None:
        return None
    if line_start is not None:
        return ShellRead(program="tail", paths=(path,), line_start=line_start)
    return ShellRead(program="tail", paths=(path,), from_end=from_end)


def _classify_sed(args: list[str]) -> ShellRead | None:
    quiet = False
    script: str | None = None
    path: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-n", "--quiet", "--silent"):
            quiet = True
            i += 1
            continue
        if a == "-e":
            if script is not None or i + 1 >= len(args):
                return None
            script = args[i + 1]
            i += 2
            continue
        if a.startswith("-"):
            return None  # -i/--in-place, -f, -E, -r, -s, -z, or any other unknown option
        if script is None:
            script = a
            i += 1
            continue
        if path is not None:
            return None
        if not _valid_path(a):
            return None
        path = a
        i += 1
    if not quiet or script is None or path is None:
        return None
    m = _SED_SCRIPT_RE.match(script)
    if not m:
        return None
    start = int(m.group(1))
    end_raw = m.group(3)
    if end_raw is None:
        end: int | None = start
    elif end_raw == "$":
        end = None
    else:
        end = int(end_raw)
    if start <= 0 or start > _MAX_LINE:
        return None
    if end is not None and (end > _MAX_LINE or start > end):
        return None
    return ShellRead(program="sed", paths=(path,), line_start=start, line_end=end)
