"""Executable allowlist for process adapters (generic-command, claude-code, codex-cli).

AgentTape only launches a local executable when its canonical path (symlinks resolved)
is allowlisted. The allowlist is the union of:

* ``AGENTTAPE_ALLOWED_EXECUTABLES`` - comma or ``os.pathsep`` separated absolute paths
  (operator configuration, read from the process environment);
* ``<data_dir>/allowed-executables.json`` - entries added from the UI, API, or CLI.

The allowlist lives in the data directory (not only the database) on purpose: adapters
run inside the worker and never touch the database, yet they re-check the allowlist at
execution time, so removing an entry stops existing profiles from launching it.

Each CLI adapter additionally trusts its own well-known executable name (``claude`` for
Claude Code, ``codex`` for Codex CLI) when it is found on ``PATH``; choosing that adapter
type is the operator's decision to run that tool. Any other path must be allowlisted.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agenttape.domain.errors import ValidationError

ENV_VAR = "AGENTTAPE_ALLOWED_EXECUTABLES"
FILE_NAME = "allowed-executables.json"
MAX_ENTRIES = 200


@dataclass(frozen=True)
class ExecutableCheck:
    """Outcome of checking one configured executable."""

    requested: str
    path: Path | None
    allowed: bool
    error: str | None = None
    #: How it was allowed: "allowlist" or "adapter-default".
    allowed_by: str | None = None

    @property
    def ok(self) -> bool:
        return self.path is not None and self.allowed and self.error is None


def resolve_executable(value: str) -> Path | None:
    """Resolve an absolute path or a bare name on PATH to a canonical file path."""
    if not value or "\x00" in value:
        return None
    candidate: str | None
    if os.sep in value or (os.altsep and os.altsep in value):
        expanded = Path(value).expanduser()
        if not expanded.is_absolute():
            return None
        candidate = str(expanded)
    else:
        candidate = shutil.which(value)
    if candidate is None:
        return None
    try:
        return Path(candidate).resolve(strict=True)
    except (OSError, RuntimeError):
        return None


#: The only directories a replayed (re-run) program is looked up in: the system's own, never
#: the worker's inherited ``PATH``, which on a developer machine lists user-writable
#: directories (``~/.local/bin``, package-manager bin dirs) that a run's own agent, running
#: as the same user, could plant a program in.
REPLAY_PATH = "/usr/local/bin:/usr/bin:/bin"


def _writable_by_this_user(path: Path) -> bool:
    """True when ``path`` is group- or world-writable, or owned by this (non-root) user:
    either way a process running as this user could have written it."""
    st = path.stat()
    if st.st_mode & 0o022:
        return True
    uid = os.geteuid()
    return uid != 0 and st.st_uid == uid


def resolve_replay_program(name: str, search_path: str = REPLAY_PATH) -> str | None:
    """The absolute path to run for the bare program ``name`` in a replay, or ``None``.

    ``name`` is looked up only in ``search_path`` (:data:`REPLAY_PATH`), never the
    inherited ``PATH``. The program is refused when it, the directory it was found in, or
    the file and directory it links to, could have been written by this user (for
    example a ``/usr/local/bin`` that a package manager made user-owned): the run's own
    agent runs as the same user."""
    if not name or "/" in name or "\x00" in name:
        return None
    found = shutil.which(name, path=search_path)
    if found is None:
        return None
    try:
        real = Path(found).resolve(strict=True)
        checked = {Path(found).parent, real, real.parent}
        if any(_writable_by_this_user(p) for p in checked):
            return None
    except (OSError, RuntimeError):
        return None
    return found


def _canonical_entries(entries: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in entries:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            path = Path(raw.strip()).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if path.is_absolute() and path not in out:
            out.append(path)
    return out


def env_entries(environ: dict[str, str] | None = None) -> list[str]:
    raw = (environ if environ is not None else os.environ).get(ENV_VAR, "")
    parts = [p.strip() for p in raw.replace(",", os.pathsep).split(os.pathsep)]
    return [p for p in parts if p]


def allowlist_file(data_dir: Path) -> Path:
    return data_dir / FILE_NAME


def stored_entries(data_dir: Path) -> list[str]:
    try:
        data = json.loads(allowlist_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("executables") if isinstance(data, dict) else None
    return [e for e in entries or [] if isinstance(e, str)]


def save_entries(data_dir: Path, entries: list[str]) -> list[str]:
    """Validate and atomically persist the stored allowlist; returns canonical entries."""
    if len(entries) > MAX_ENTRIES:
        raise ValidationError(f"At most {MAX_ENTRIES} executables can be allowlisted")
    cleaned: list[str] = []
    for raw in entries:
        path = resolve_executable(raw.strip()) if isinstance(raw, str) else None
        if path is None:
            raise ValidationError(
                "Allowlisted executables must be existing absolute paths or names on PATH",
                details={"executable": raw},
            )
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValidationError(
                "Allowlisted entries must be executable files", details={"executable": str(path)}
            )
        if str(path) not in cleaned:
            cleaned.append(str(path))
    data_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".allowed-executables-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"executables": cleaned}, fh, indent=2)
        Path(tmp).replace(allowlist_file(data_dir))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return cleaned


def effective_allowlist(data_dir: Path) -> list[Path]:
    return _canonical_entries([*env_entries(), *stored_entries(data_dir)])


def check_executable(
    value: str | None,
    data_dir: Path,
    *,
    default_names: tuple[str, ...] = (),
) -> ExecutableCheck:
    """Resolve ``value`` and decide whether it may be launched."""
    requested = (value or "").strip()
    if not requested:
        return ExecutableCheck(requested, None, False, "no executable configured")
    path = resolve_executable(requested)
    if path is None:
        where = "at that path" if os.sep in requested else "on PATH"
        return ExecutableCheck(requested, None, False, f"'{requested}' was not found {where}")
    if not path.is_file():
        return ExecutableCheck(requested, path, False, f"{path} is not a file")
    if not os.access(path, os.X_OK):
        return ExecutableCheck(requested, path, False, f"{path} is not executable")
    if path in effective_allowlist(data_dir):
        return ExecutableCheck(requested, path, True, allowed_by="allowlist")
    if requested in default_names:
        return ExecutableCheck(requested, path, True, allowed_by="adapter-default")
    return ExecutableCheck(
        requested,
        path,
        False,
        f"{path} is not on the executable allowlist (add it in Agents > Executable "
        f"allowlist or set {ENV_VAR})",
    )
