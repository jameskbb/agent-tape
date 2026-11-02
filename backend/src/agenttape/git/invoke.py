"""Hardened Git invocation: the one way AgentTape itself starts a ``git`` process.

An agent can write files in the directory AgentTape later inspects with Git. Without
care, Git would then run programs named in files the agent controls: ``core.fsmonitor``,
``diff.external``, ``diff.<driver>.command``/``textconv``, ``filter.<driver>.clean``/
``smudge``/``process`` (selected by an agent-written ``.gitattributes``), pagers,
hooks, credential helpers. In an isolated worktree the agent can even rewrite the
``.git`` *file* so Git discovers a planted repository whose config sets all of these.

Every AgentTape Git call therefore goes through :func:`run_git_hardened`, which:

* passes fixed ``-c`` overrides (:data:`HARDENING_CONFIG`) that disable fsmonitor, the
  untracked cache, external diff, pagers, hooks, signing, credential helpers, transports,
  submodule recursion, and auto-maintenance;
* reads the effective configuration first (``git config --list --show-scope``, which
  executes nothing) and blanks every program-valued key that comes from a repository
  scope (local, worktree, includes): clean/smudge/process filters, diff and merge
  drivers, pagers, editors, gpg programs, ssh/askpass/proxy commands. The overrides are
  passed through ``GIT_CONFIG_COUNT`` so a subsection name containing ``=`` cannot
  escape them. The user's own global config (``~/.gitconfig``) is trusted, so a global
  Git LFS setup keeps working;
* always adds ``--no-ext-diff --no-textconv`` to ``diff``/``show``/``log`` and
  ``--ignore-submodules=dirty`` to ``diff``/``status`` (so Git never spawns itself inside
  an agent-controlled submodule);
* runs with a minimal environment: ``GIT_CONFIG_NOSYSTEM=1``, ``GIT_TERMINAL_PROMPT=0``,
  ``GIT_PAGER=cat``, no ``GIT_DIR``/``GIT_EXTERNAL_DIFF``/``GIT_CONFIG_PARAMETERS``
  inherited;
* for an AgentTape-managed worktree, never lets Git discover the repository from the
  agent-writable ``.git`` file: it passes the ``--git-dir`` recorded when AgentTape
  created the worktree (:func:`record_worktree_git_dir`) and ``--work-tree`` explicitly.
  A changed ``.git`` file is logged and ignored.

A failed configuration read fails closed: the requested command is not run.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0

#: Fixed ``-c`` overrides for every AgentTape Git call.
HARDENING_CONFIG: tuple[str, ...] = (
    "core.fsmonitor=false",
    "core.untrackedCache=false",
    # Empty makes an external diff fail loudly instead of running; --no-ext-diff (always
    # added to diff-like commands below) means it is never consulted in practice.
    "diff.external=",
    "core.pager=cat",
    "core.hooksPath=/dev/null",
    "commit.gpgsign=false",
    "tag.gpgsign=false",
    "core.quotepath=false",
    "credential.helper=",
    "core.askPass=",
    "core.sshCommand=",
    "submodule.recurse=false",
    "protocol.allow=never",
    "gc.auto=0",
    "maintenance.auto=false",
)

_FIXED_KEYS = frozenset(item.split("=", 1)[0].lower() for item in HARDENING_CONFIG)

#: Keys whose value names a program Git may run. Section and variable names are
#: case-insensitive; subsection names (the middle part) are matched as-is.
_PROGRAM_KEY = re.compile(
    r"""^(?:
        filter\..+\.(?:clean|smudge|process)
      | diff\..+\.(?:command|textconv)
      | merge\..+\.driver
      | gpg\.(?:.+\.)?program
      | pager\..+
      | credential\.(?:.+\.)?helper
      | core\.(?:editor|gitproxy|alternaterefscommand|sshcommand|askpass|pager|fsmonitor)
      | sequence\.editor
      | uploadpack\.packobjectshook
      | remote\..+\.(?:uploadpack|receivepack)
    )$""",
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

#: Configuration scopes that are not repository-controlled.
_TRUSTED_SCOPES = frozenset({"global", "system", "command"})

#: Diff-producing subcommands that always get ``--no-ext-diff --no-textconv``.
_DIFF_LIKE = frozenset({"diff", "show", "log"})
_NO_PROGRAM_FLAGS = ("--no-ext-diff", "--no-textconv")
_SUBMODULE_SAFE = frozenset({"diff", "status"})

#: Sidecar written next to a managed worktree: ``<root>/.<name>.agenttape-gitdir``.
SIDECAR_SUFFIX = ".agenttape-gitdir"


#: ``GIT_*`` variables a caller may add (identity only; never GIT_DIR, GIT_CONFIG_*...).
_ALLOWED_EXTRA_GIT_ENV = frozenset(
    {
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_DATE",
        # Only narrows what is read (the demo fixture points it at /dev/null).
        "GIT_CONFIG_GLOBAL",
    }
)


def git_executable() -> str:
    exe = shutil.which("git")
    if exe is None:
        raise FileNotFoundError("git executable not found on PATH")
    return exe


def hardened_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal environment for AgentTape's own Git calls."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", tempfile.gettempdir()),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        # Read-only helpers never take the index lock (no racing a running agent).
        "GIT_OPTIONAL_LOCKS": "0",
    }
    for name in ("GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    for key, value in (extra or {}).items():
        if key.startswith("GIT_") and key not in _ALLOWED_EXTRA_GIT_ENV:
            raise ValueError(f"{key} may not be set for AgentTape's Git calls")
        env[key] = value
    return env


# ---------------------------------------------------------------- managed worktrees


def sidecar_path(worktree: Path) -> Path:
    worktree = Path(worktree).resolve()
    return worktree.parent / f".{worktree.name}{SIDECAR_SUFFIX}"


def _read_git_link(worktree: Path) -> str | None:
    try:
        link = worktree / ".git"
        if link.is_symlink() or not link.is_file():
            return None
        return link.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def record_worktree_git_dir(worktree: Path) -> Path:
    """Record the admin git dir of a worktree AgentTape just created (before any agent ran).

    Later calls use this recorded directory instead of the agent-writable ``.git`` file.
    """
    worktree = Path(worktree).resolve()
    content = _read_git_link(worktree)
    if content is None or not content.startswith("gitdir:"):
        raise RuntimeError(f"{worktree} has no .git link file")
    raw = content[len("gitdir:") :].strip()
    git_dir = Path(raw) if Path(raw).is_absolute() else worktree / raw
    git_dir = git_dir.resolve()
    if not (git_dir / "HEAD").is_file() or not (git_dir / "commondir").is_file():
        raise RuntimeError(f"{git_dir} is not a Git worktree admin directory")
    sidecar_path(worktree).write_text(
        json.dumps({"git_dir": str(git_dir), "git_link": content}), encoding="utf-8"
    )
    return git_dir


def forget_worktree_git_dir(worktree: Path) -> None:
    with contextlib.suppress(OSError):
        sidecar_path(worktree).unlink(missing_ok=True)


def trusted_git_dir(repo: Path) -> Path | None:
    """The recorded git dir for an AgentTape-managed worktree, or ``None`` if unmanaged."""
    repo = Path(repo).resolve()
    sidecar = sidecar_path(repo)
    try:
        if not sidecar.is_file():
            return None
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    git_dir = Path(str(data.get("git_dir") or ""))
    if not git_dir.is_absolute():
        return None
    if _read_git_link(repo) != data.get("git_link"):
        logger.warning(
            "the .git link in managed worktree %s was changed; ignoring it and using the "
            "git dir AgentTape recorded (%s)",
            repo,
            git_dir,
        )
    return git_dir


def git_link_changed(repo: Path) -> bool:
    """True when a managed worktree's ``.git`` file no longer matches what was recorded."""
    repo = Path(repo).resolve()
    try:
        data = json.loads(sidecar_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(_read_git_link(repo) != data.get("git_link"))


# ---------------------------------------------------------------- command building


def base_argv(repo: Path) -> list[str]:
    """``git -C <repo> [--git-dir --work-tree] -c ...`` for ``repo``."""
    argv = [git_executable(), "-C", str(repo)]
    git_dir = trusted_git_dir(repo)
    if git_dir is not None:
        argv += [f"--git-dir={git_dir}", f"--work-tree={Path(repo).resolve()}"]
    for item in HARDENING_CONFIG:
        argv += ["-c", item]
    return argv


def harden_args(args: Sequence[str]) -> list[str]:
    """Insert the flags that stop diff-like commands from running configured programs."""
    if not args:
        return []
    sub, rest = args[0], list(args[1:])
    extra: list[str] = []
    if sub in _DIFF_LIKE:
        extra += [flag for flag in _NO_PROGRAM_FLAGS if flag not in rest]
    if sub in _SUBMODULE_SAFE and not any(a.startswith("--ignore-submodules") for a in rest):
        extra.append("--ignore-submodules=dirty")
    return [sub, *extra, *rest]


def program_overrides(config_listing: str) -> list[str]:
    """Keys to blank, from ``git config --list --show-scope --name-only -z`` output."""
    tokens = config_listing.split("\0")
    keys: list[str] = []
    seen: set[str] = set()
    for i in range(0, len(tokens) - 1, 2):
        scope, key = tokens[i], tokens[i + 1]
        if scope in _TRUSTED_SCOPES or not key or key.lower() in _FIXED_KEYS:
            continue
        if _PROGRAM_KEY.match(key) and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _override_env(keys: Sequence[str]) -> dict[str, str]:
    env = {"GIT_CONFIG_COUNT": str(len(keys))}
    for i, key in enumerate(keys):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = ""
    return env


def run_git_hardened(
    repo: Path,
    *args: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``repo`` with every hardening above. Never uses a shell."""
    prefix = base_argv(Path(repo))
    child_env = hardened_env(env)
    listing = subprocess.run(  # argument array; reading config executes nothing
        [*prefix, "config", "--list", "--show-scope", "--name-only", "-z"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
        env=child_env,
        stdin=subprocess.DEVNULL,
    )
    argv = [*prefix, *harden_args(args)]
    if listing.returncode != 0:
        # Fail closed: without knowing which programs the config names, run nothing.
        return subprocess.CompletedProcess(
            argv,
            listing.returncode,
            "",
            "agenttape: could not read the Git configuration safely: " + listing.stderr,
        )
    child_env.update(_override_env(program_overrides(listing.stdout)))
    return subprocess.run(  # argument array, never a shell
        argv,
        capture_output=True,
        text=True,
        input=input_text,
        timeout=timeout,
        check=False,
        env=child_env,
        stdin=None if input_text is not None else subprocess.DEVNULL,
    )
