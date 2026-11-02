"""V1 safety policy (docs/ORCHESTRATION.md). Pure rules; no I/O.

Two layers:

1. **Hard-disallowed** actions are refused even with an approval: automatic push,
   automatic merge, force push, destructive Git reset, deleting branches or worktrees
   AgentTape did not create, arbitrary host filesystem deletion, and displaying secret
   values (dumping the environment).
2. **Approval required by default**: committing Git changes, elevated commands or
   commands outside the allowlist, writing a protected path, increasing an established
   run budget, and continuing after a failed required evaluation (when override is
   permitted at all).

Everything else is allowed. The command checks inspect argument arrays (AgentTape never
runs a shell), so they are exact for direct invocations; they cannot see inside an
allowlisted interpreter's own code, which is why interpreters are only allowlisted by an
operator's explicit choice and elevated commands always need an approval.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from agenttape.domain.enums import RiskLevel


class ApprovalType(StrEnum):
    RUN_CONTINUE = "run_continue"
    WORKFLOW_NODE = "workflow_node"
    #: Same value as the Git service's commit approvals (application/services/git.py).
    GIT_COMMIT = "commit"
    ELEVATED_COMMAND = "elevated_command"
    PROTECTED_PATH = "protected_path"
    BUDGET_INCREASE = "budget_increase"
    EVALUATION_OVERRIDE = "evaluation_override"


#: Transitions that always need a human decision by default.
APPROVAL_REQUIRED_BY_DEFAULT: frozenset[ApprovalType] = frozenset(
    {
        ApprovalType.GIT_COMMIT,
        ApprovalType.ELEVATED_COMMAND,
        ApprovalType.PROTECTED_PATH,
        ApprovalType.BUDGET_INCREASE,
        ApprovalType.EVALUATION_OVERRIDE,
    }
)

DEFAULT_RISK: dict[ApprovalType, RiskLevel] = {
    ApprovalType.RUN_CONTINUE: RiskLevel.MEDIUM,
    ApprovalType.WORKFLOW_NODE: RiskLevel.MEDIUM,
    ApprovalType.GIT_COMMIT: RiskLevel.HIGH,
    ApprovalType.ELEVATED_COMMAND: RiskLevel.HIGH,
    ApprovalType.PROTECTED_PATH: RiskLevel.HIGH,
    ApprovalType.BUDGET_INCREASE: RiskLevel.MEDIUM,
    ApprovalType.EVALUATION_OVERRIDE: RiskLevel.HIGH,
}


class DisallowedAction(StrEnum):
    AUTOMATIC_PUSH = "automatic_push"
    AUTOMATIC_MERGE = "automatic_merge"
    FORCE_PUSH = "force_push"
    DESTRUCTIVE_RESET = "destructive_git_reset"
    DELETE_FOREIGN_BRANCH_OR_WORKTREE = "delete_branch_or_worktree"
    HOST_FILE_DELETION = "host_filesystem_deletion"
    DISPLAY_SECRETS = "display_secret_values"


HARD_DISALLOWED: dict[DisallowedAction, str] = {
    DisallowedAction.AUTOMATIC_PUSH: "Pushing to a remote is never automated in V1.",
    DisallowedAction.AUTOMATIC_MERGE: "Merging, rebasing onto, or pulling branches is never automated in V1.",
    DisallowedAction.FORCE_PUSH: "Force pushes are never allowed.",
    DisallowedAction.DESTRUCTIVE_RESET: "Destructive Git resets and cleans are never allowed.",
    DisallowedAction.DELETE_FOREIGN_BRANCH_OR_WORKTREE: "Deleting branches or worktrees is only done by AgentTape for worktrees it created.",
    DisallowedAction.HOST_FILE_DELETION: "Arbitrary host filesystem deletion is never allowed.",
    DisallowedAction.DISPLAY_SECRETS: "Dumping environment or secret values is never allowed.",
}


class Verdict(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyDecision:
    verdict: Verdict
    reason: str
    approval_type: ApprovalType | None = None
    risk_level: RiskLevel = RiskLevel.LOW
    disallowed: DisallowedAction | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.verdict == Verdict.ALLOW

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "approval_type": self.approval_type.value if self.approval_type else None,
            "risk_level": self.risk_level.value,
            "disallowed": self.disallowed.value if self.disallowed else None,
            "details": self.details,
        }


def allow(reason: str = "Allowed by policy") -> PolicyDecision:
    return PolicyDecision(Verdict.ALLOW, reason)


def deny(action: DisallowedAction, detail: str | None = None) -> PolicyDecision:
    reason = HARD_DISALLOWED[action] + (f" ({detail})" if detail else "")
    return PolicyDecision(Verdict.DENY, reason, risk_level=RiskLevel.CRITICAL, disallowed=action)


def needs_approval(kind: ApprovalType, reason: str, **details: Any) -> PolicyDecision:
    return PolicyDecision(
        Verdict.REQUIRE_APPROVAL,
        reason,
        approval_type=kind,
        risk_level=DEFAULT_RISK[kind],
        details=details,
    )


# --------------------------------------------------------------------------- commands

#: Executables allowed without approval when nothing else is configured. Read-only or
#: build/test tools; interpreters are *not* here (an operator may add them).
DEFAULT_COMMAND_ALLOWLIST: tuple[str, ...] = (
    "git",
    "make",
    "pytest",
    "ruff",
    "mypy",
    "npm",
    "pnpm",
    "yarn",
    "cargo",
    "ls",
    "cat",
    "echo",
    "true",
    "false",
    "test",
    "wc",
    "grep",
    "diff",
)

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "pwsh", "powershell"}
_PRIVILEGE = {"sudo", "su", "doas", "pkexec", "runuser"}
_DELETERS = {"rm", "rmdir", "shred", "unlink", "srm", "wipe"}
_ENV_DUMPERS = {"printenv", "env", "set", "export", "declare"}

#: Environment names a child may never be given from configuration (a project or profile
#: ``env_allowlist``, or an ``env NAME=value`` prefix): they change what git or the dynamic
#: loader does without appearing in argv (GIT_CONFIG_*, GIT_DIR, GIT_SSH_COMMAND,
#: GIT_EXEC_PATH, LD_PRELOAD...).
FORBIDDEN_ENV_NAME = re.compile(
    r"^(GIT_.*|LD_PRELOAD|LD_LIBRARY_PATH|LD_AUDIT|DYLD_.*|BASH_ENV|ENV)$"
)


def forbidden_env_name(name: str) -> bool:
    return bool(FORBIDDEN_ENV_NAME.match(name))


#: git subcommands that are read-only or local, never destructive.
_GIT_READONLY = {
    "status",
    "diff",
    "log",
    "show",
    "rev-parse",
    "ls-files",
    "blame",
    "describe",
    "shortlog",
    "grep",
    "cat-file",
    "branch",  # listing only; deletion is denied and creation needs approval below
    "worktree",  # listing only; removal is denied and add/move/lock need approval below
    "stash",  # checked below for drop/clear
    "config",
    "remote",
    "tag",
    "add",
    "commit",
    "checkout",
    "switch",
    "restore",
    "reset",  # checked below for --hard/--merge/--keep
    "clean",  # checked below
}


def _base(exe: str) -> str:
    return PurePosixPath(exe).name


#: git global options that take a separate value argument.
_GIT_OPTS_WITH_VALUE = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
    "--exec-path",
    "--super-prefix",
}
#: git helper programs that push (``git-send-pack`` et al. are pushes by another name).
_GIT_PUSH_PLUMBING = {"send-pack", "http-push", "receive-pack"}


@dataclass(frozen=True)
class GitInvocation:
    """A git argv split into global options, subcommand, and the rest."""

    global_opts: list[str]
    sub: str | None
    rest: list[str]
    #: ``-c key=value`` / ``--config-env key=ENV`` settings (key lower-cased).
    config: list[tuple[str, str]]


def parse_git(argv: Sequence[str]) -> GitInvocation:
    """Skip git's global options (``-C path``, ``-c k=v``, ``--git-dir=...``)."""
    opts: list[str] = []
    config: list[tuple[str, str]] = []
    i = 1
    while i < len(argv):
        a = argv[i]
        name, eq, joined = a.partition("=")
        if a in _GIT_OPTS_WITH_VALUE:
            value = argv[i + 1] if i + 1 < len(argv) else ""
            opts += [a, value]
            if a in ("-c", "--config-env"):
                key, _, val = value.partition("=")
                config.append((key.strip().lower(), val))
            i += 2
            continue
        if a.startswith("-"):
            opts.append(a)
            if eq and name in ("--config-env",):
                key, _, val = joined.partition("=")
                config.append((key.strip().lower(), val))
            elif a.startswith("-c") and len(a) > 2:  # ``-ckey=value``
                key, _, val = a[2:].partition("=")
                config.append((key.strip().lower(), val))
            i += 1
            continue
        return GitInvocation(opts, a, list(argv[i + 1 :]), config)
    return GitInvocation(opts, None, [], config)


def _short_flags(rest: Sequence[str]) -> set[str]:
    """Letters of combined short options (``-fu`` -> {f, u}); long options excluded."""
    letters: set[str] = set()
    for r in rest:
        if r.startswith("-") and not r.startswith("--") and len(r) > 1:
            letters.update(r[1:])
    return letters


def _options(rest: Sequence[str]) -> list[str]:
    """Arguments before ``--`` (after it git reads pathspecs, never options)."""
    out: list[str] = []
    for r in rest:
        if r == "--":
            break
        out.append(r)
    return out


#: Long options that make git start another program or write a file of the caller's
#: choosing: ``fetch --upload-pack=<cmd>``, ``archive --exec=<cmd>``, ``grep
#: --open-files-in-pager=<cmd>``, ``log --output=<path>``, ``format-patch
#: --output-directory``. Git accepts any unambiguous prefix of a long option
#: (``--upload-p``), so a prefix of one of these names counts as the option.
_GIT_DANGEROUS_LONG = (
    "upload-pack",
    "receive-pack",
    "exec",
    "output",
    "output-directory",
    "open-files-in-pager",
)
#: Subcommands whose ``-u`` is ``--upload-pack`` (a program to run).
_GIT_UPLOAD_PACK_SHORT = {"fetch", "clone", "ls-remote", "pull", "archive"}
#: Subcommands whose ``-o`` names an output file or directory.
_GIT_OUTPUT_SHORT = {"format-patch", "archive"}
#: ``git -c`` keys that cannot run programs or rewrite subcommands. Any other ``-c`` key
#: (core.sshCommand, core.pager, core.hooksPath, core.fsmonitor, diff.external,
#: protocol.*.allow, alias.* ...) is refused outright; these still need an approval.
_GIT_SAFE_CONFIG = {"user.name", "user.email", "core.quotepath", "color.ui"}


def _git_runs_program(what: str) -> PolicyDecision:
    return PolicyDecision(
        Verdict.DENY,
        f"{what} can make git run another program or write a file of the caller's choosing "
        "(which could push, merge, or reset), so it is never run",
        risk_level=RiskLevel.CRITICAL,
        disallowed=DisallowedAction.AUTOMATIC_PUSH,
    )


def _git_long_name(arg: str) -> str | None:
    if not arg.startswith("--") or len(arg) <= 2:
        return None
    return arg[2:].partition("=")[0].lower()


def _git_dangerous_option(rest: Sequence[str]) -> str | None:
    for r in _options(rest):
        name = _git_long_name(r)
        if name and any(d.startswith(name) for d in _GIT_DANGEROUS_LONG):
            return r
    return None


_GIT_CONFIG_READ = {
    "--get",
    "--get-all",
    "--get-regexp",
    "--list",
    "-l",
    "--show-origin",
    "get",
    "list",
}


def _git_config_writes(rest: Sequence[str]) -> bool:
    """``git config KEY`` reads; ``git config KEY VALUE``, ``--add``, ``set``... write."""
    if set(rest) & _GIT_CONFIG_READ:
        return False
    positional = [r for r in rest if not r.startswith("-")]
    scopes = ("--global", "--local", "--system")
    options = [r for r in rest if r.startswith("-") and r not in scopes]
    return len(positional) >= 2 or bool(options)


#: ``git branch`` options that only select or format a listing. A value-taking option also
#: accepts its value as the next argument.
_GIT_BRANCH_LIST_LONG = frozenset(
    {
        "list",
        "all",
        "remotes",
        "verbose",
        "quiet",
        "show-current",
        "ignore-case",
        "omit-empty",
        "column",
        "no-column",
        "color",
        "no-color",
        "abbrev",
        "no-abbrev",
    }
)
_GIT_BRANCH_LIST_LONG_VALUE = frozenset({"sort", "format", "points-at"})
#: Filter options: any of them puts ``git branch`` in listing mode (git's own rule), so
#: arguments after them are patterns or commits, never a new branch name.
_GIT_BRANCH_FILTER_LONG = frozenset(
    {"contains", "no-contains", "merged", "no-merged", "points-at", "list"}
)
_GIT_BRANCH_LIST_SHORT = frozenset("arvqli")


def _git_branch_lists(rest: Sequence[str]) -> bool:
    """``git branch ...`` that only lists branches (no name to create, copy, or configure).

    Listing mode is git's own: no positional argument at all, or ``--list``/``-l`` or a
    filter option (``--contains``, ``--merged``...) with patterns after it. Every option
    must be a known listing or formatting option; anything else (``-c``/``-C``/``--copy``,
    ``-u``/``--set-upstream-to``, ``--unset-upstream``, ``-t``/``--track``,
    ``--edit-description``, ``--create-reflog``...) is not a listing.
    """
    list_mode = False
    positional = False
    i = 0
    while i < len(rest):
        r = rest[i]
        i += 1
        if r == "--":
            positional = positional or i < len(rest)
            break
        if r.startswith("--"):
            name, eq, _value = r[2:].partition("=")
            if name in _GIT_BRANCH_FILTER_LONG:
                list_mode = True
            if name in _GIT_BRANCH_LIST_LONG_VALUE:
                if not eq:
                    i += 1  # the value is the next argument
                continue
            if name in _GIT_BRANCH_LIST_LONG or name in _GIT_BRANCH_FILTER_LONG:
                continue
            return False
        if r.startswith("-") and len(r) > 1:
            letters = set(r[1:])
            if not letters <= _GIT_BRANCH_LIST_SHORT:
                return False
            if "l" in letters:
                list_mode = True
            continue
        positional = True
    return list_mode or not positional


def check_git(argv: Sequence[str], *, owned_worktrees: Iterable[str] = ()) -> PolicyDecision:
    inv = parse_git(argv)
    sub, rest = inv.sub, inv.rest
    # Config injection can rename or replace any subcommand, so it is judged first.
    for key, _value in inv.config:
        if key.startswith("alias."):
            return deny(
                DisallowedAction.AUTOMATIC_PUSH,
                f"git -c {key}=... can hide push, merge, or reset behind another name",
            )
    if any(o == "--config-env" or o.startswith("--config-env=") for o in inv.global_opts):
        return _git_runs_program("git --config-env")
    for key, _value in inv.config:
        if key not in _GIT_SAFE_CONFIG and not key.startswith("advice."):
            return _git_runs_program(f"git -c {key}=...")
    if any(o == "--exec-path" or o.startswith("--exec-path=") for o in inv.global_opts):
        return deny(
            DisallowedAction.AUTOMATIC_PUSH,
            "git --exec-path can replace subcommands with other programs",
        )
    if sub is None:
        return allow("git without a subcommand")
    dangerous = _git_dangerous_option(rest)
    if dangerous is not None:
        return _git_runs_program(f"git {sub} {dangerous.partition('=')[0]}")
    opt_short = _short_flags(_options(rest))
    if sub in _GIT_UPLOAD_PACK_SHORT and "u" in opt_short:
        return _git_runs_program(f"git {sub} -u (upload-pack program)")
    if sub in _GIT_OUTPUT_SHORT and "o" in opt_short:
        return _git_runs_program(f"git {sub} -o (output file)")
    if sub == "grep" and "O" in opt_short:
        return _git_runs_program("git grep -O (runs a pager program)")
    if sub in ("clone", "init") and any(
        (n := _git_long_name(r)) is not None
        and (
            ("template".startswith(n) and len(n) >= 2)
            or (sub == "clone" and len(n) >= 3 and "config".startswith(n))
        )
        for r in _options(rest)
    ):
        return _git_runs_program(f"git {sub} --template/--config")
    if sub == "clone" and "c" in opt_short:
        return _git_runs_program("git clone -c (config that can run programs)")
    flags = set(rest)
    short = _short_flags(rest)
    if sub in _GIT_PUSH_PLUMBING:
        return deny(DisallowedAction.AUTOMATIC_PUSH, f"git {sub}")
    if sub == "push":
        if (
            flags & {"--force", "-f", "--force-with-lease", "--mirror"}
            or "f" in short
            or any(r.startswith("+") or r.startswith("--force") for r in rest)
        ):
            return deny(DisallowedAction.FORCE_PUSH, "git push --force")
        if flags & {"--delete", "-d"} or "d" in short or any(r.startswith(":") for r in rest):
            return deny(DisallowedAction.DELETE_FOREIGN_BRANCH_OR_WORKTREE, "git push --delete")
        return deny(DisallowedAction.AUTOMATIC_PUSH, "git push")
    if sub in ("merge", "pull", "rebase", "cherry-pick", "am"):
        return deny(DisallowedAction.AUTOMATIC_MERGE, f"git {sub}")
    if sub == "reset" and flags & {"--hard", "--merge", "--keep"}:
        return deny(DisallowedAction.DESTRUCTIVE_RESET, "git reset " + " ".join(sorted(flags)))
    if sub == "clean" and (
        any(f.startswith("-") and not f.startswith("--") and ("f" in f or "x" in f) for f in rest)
        or "--force" in flags
    ):
        return deny(DisallowedAction.DESTRUCTIVE_RESET, "git clean -f")
    if sub in ("checkout", "restore", "switch") and (
        ("--" in rest and "." in rest)
        or "-f" in flags
        or "--force" in flags
        or "--discard-changes" in flags
        or (sub == "checkout" and "f" in short)
    ):
        return deny(DisallowedAction.DESTRUCTIVE_RESET, f"git {sub} discarding changes")
    if sub == "stash" and rest[:1] and rest[0] in ("drop", "clear"):
        return deny(DisallowedAction.DESTRUCTIVE_RESET, f"git stash {rest[0]}")
    if sub == "branch" and (
        flags & {"--delete", "--move", "--force"} or short & {"d", "D", "m", "M", "f"}
    ):
        return deny(DisallowedAction.DELETE_FOREIGN_BRANCH_OR_WORKTREE, "git branch --delete")
    if sub == "branch" and not _git_branch_lists(rest):
        # Creating, copying, or reconfiguring a branch changes the repository's refs or
        # config; only a listing is read-only. AgentTape's own run branches are created
        # by the guarded Git runner (git/runner.py), which never consults this policy.
        return needs_approval(
            ApprovalType.ELEVATED_COMMAND,
            "'git branch' with a name or a non-listing option creates, copies, or "
            "reconfigures a branch",
            argv=list(argv),
        )
    if sub == "tag" and ("-d" in flags or "--delete" in flags or "d" in short):
        return deny(DisallowedAction.DELETE_FOREIGN_BRANCH_OR_WORKTREE, "git tag --delete")
    if sub in ("update-ref", "symbolic-ref") and ("-d" in flags or "--delete" in flags):
        return deny(DisallowedAction.DELETE_FOREIGN_BRANCH_OR_WORKTREE, f"git {sub} -d")
    if sub == "remote" and rest[:1] and rest[0] in ("remove", "rm", "set-url", "prune"):
        return needs_approval(
            ApprovalType.ELEVATED_COMMAND,
            f"'git remote {rest[0]}' changes remotes",
            argv=list(argv),
        )
    if sub == "worktree" and rest[:1] and rest[0] in ("remove", "prune"):
        owned = {posixpath.normpath(p) for p in owned_worktrees}
        targets = [r for r in rest[1:] if not r.startswith("-")]
        if (
            rest[0] == "prune"
            or not targets
            or any(posixpath.normpath(t) not in owned for t in targets)
        ):
            return deny(
                DisallowedAction.DELETE_FOREIGN_BRANCH_OR_WORKTREE, f"git worktree {rest[0]}"
            )
    if sub == "worktree" and rest[:1] and rest[0] not in ("list", "remove"):
        # add, move, lock, unlock, repair...: each registers or rewrites a worktree in the
        # repository's shared admin data. AgentTape's own managed worktrees and snapshots
        # are added by the guarded Git runner (git/runner.py), never through this policy.
        return needs_approval(
            ApprovalType.ELEVATED_COMMAND,
            f"'git worktree {rest[0]}' changes the repository's worktrees",
            argv=list(argv),
        )
    if inv.config:
        return needs_approval(
            ApprovalType.ELEVATED_COMMAND,
            "git -c settings can run other programs (hooks, pagers, ssh commands)",
            argv=list(argv),
        )
    if sub == "config" and _git_config_writes(rest):
        return needs_approval(
            ApprovalType.ELEVATED_COMMAND,
            "Changing git config can add aliases or hooks",
            argv=list(argv),
        )
    if sub == "commit":
        return needs_approval(
            ApprovalType.GIT_COMMIT, "Committing Git changes requires approval", argv=list(argv)
        )
    if sub not in _GIT_READONLY:
        return needs_approval(
            ApprovalType.ELEVATED_COMMAND,
            f"'git {sub}' is not on the list of read-only git commands",
            argv=list(argv),
        )
    return allow("git read-only or local command")


@dataclass(frozen=True)
class _WrapperSyntax:
    """Option syntax of a program that runs another program (GNU getopt_long style)."""

    #: Short options that take a value (``-n 5`` or ``-n5``).
    short_value: str = ""
    #: Short options whose value is optional and only ever attached (``xargs -i{}``).
    short_optional: str = ""
    #: Short options without a value.
    short_flags: str = ""
    #: Long options that take a value (``--signal KILL`` or ``--signal=KILL``).
    long_value: frozenset[str] = frozenset()
    #: Long options whose value is optional and only ever attached with ``=``.
    long_optional: frozenset[str] = frozenset()
    long_flags: frozenset[str] = frozenset()
    #: Positional arguments before the command (``timeout DURATION command``).
    positional: int = 0
    #: ``nice -5 command`` (an obsolete numeric option).
    numeric_option: bool = False


_WRAPPERS: dict[str, _WrapperSyntax] = {
    "nohup": _WrapperSyntax(),
    "nice": _WrapperSyntax(
        short_value="n", long_value=frozenset({"adjustment"}), numeric_option=True
    ),
    "timeout": _WrapperSyntax(
        short_value="sk",
        short_flags="v",
        long_value=frozenset({"signal", "kill-after"}),
        long_flags=frozenset({"preserve-status", "foreground", "verbose"}),
        positional=1,
    ),
    "ionice": _WrapperSyntax(
        short_value="cnpPu",
        short_flags="t",
        long_value=frozenset({"class", "classdata", "pid", "pgid", "uid"}),
        long_flags=frozenset({"ignore"}),
    ),
    "stdbuf": _WrapperSyntax(short_value="ioe", long_value=frozenset({"input", "output", "error"})),
    "time": _WrapperSyntax(
        short_value="fo",
        short_flags="apqvV",
        long_value=frozenset({"format", "output"}),
        long_flags=frozenset({"append", "portability", "verbose", "quiet"}),
    ),
    "xargs": _WrapperSyntax(
        short_value="adEILnPs",
        short_optional="eil",
        short_flags="0oprtx",
        long_value=frozenset(
            {"arg-file", "delimiter", "max-args", "max-procs", "max-chars", "process-slot-var"}
        ),
        long_optional=frozenset({"eof", "replace", "max-lines"}),
        long_flags=frozenset(
            {"null", "no-run-if-empty", "interactive", "verbose", "exit", "open-tty"}
        ),
    ),
    "setsid": _WrapperSyntax(short_flags="cfw", long_flags=frozenset({"ctty", "fork", "wait"})),
}


def _unparseable(exe: str, arg: str) -> PolicyDecision:
    return PolicyDecision(
        Verdict.DENY,
        f"'{exe}' option {arg[:40]!r} is not understood, so the program it runs cannot be "
        "judged; pass the program directly",
        risk_level=RiskLevel.HIGH,
    )


def _long_option(arg: str, names: Iterable[str]) -> str | None:
    """The long option ``arg`` names (getopt_long accepts unambiguous prefixes)."""
    given = arg[2:].partition("=")[0]
    candidates = sorted(set(names))
    if given in candidates:
        return given
    matches = [n for n in candidates if given and n.startswith(given)]
    return matches[0] if len(matches) == 1 else None


def _unwrap(exe: str, args: Sequence[str]) -> list[str] | PolicyDecision:
    """The command a wrapper runs, or a refusal when its options cannot be parsed."""
    syntax = _WRAPPERS[exe]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a.startswith("--"):
            all_long = syntax.long_value | syntax.long_optional | syntax.long_flags
            name = _long_option(a, all_long)
            if name is None:
                return _unparseable(exe, a)
            if name in syntax.long_value and "=" not in a:
                i += 1  # the value is the next argument
            i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            if syntax.numeric_option and a[1:].isdigit():
                i += 1
                continue
            j = 1
            while j < len(a):
                ch = a[j]
                if ch in syntax.short_value:
                    if j == len(a) - 1:
                        i += 1  # ``-n 5``: the value is the next argument
                    break  # ``-n5``: the rest of this argument is the value
                if ch in syntax.short_optional:
                    break
                if ch not in syntax.short_flags:
                    return _unparseable(exe, a)
                j += 1
            i += 1
            continue
        break
    i += syntax.positional
    return list(args[i:])


_ENV_SHORT_VALUE = "uC"
_ENV_SHORT_FLAGS = "i0v"
_ENV_LONG_VALUE = frozenset({"unset", "chdir", "split-string"})
_ENV_LONG_OPTIONAL = frozenset({"block-signal", "default-signal", "ignore-signal"})
_ENV_LONG_FLAGS = frozenset({"ignore-environment", "null", "debug", "list-signal-handling"})


def _unwrap_env(args: Sequence[str]) -> list[str] | PolicyDecision:
    """``env [OPTION]... [NAME=VALUE]... [COMMAND [ARG]...]`` -> COMMAND [ARG]..."""
    split_string = PolicyDecision(
        Verdict.DENY,
        "env -S/--split-string splits a string into a command line like a shell does",
        risk_level=RiskLevel.CRITICAL,
    )
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a == "-":
            i += 1
            continue
        if a.startswith("--"):
            name = _long_option(a, _ENV_LONG_VALUE | _ENV_LONG_OPTIONAL | _ENV_LONG_FLAGS)
            if name is None:
                return _unparseable("env", a)
            if name == "split-string":
                return split_string
            if name in _ENV_LONG_VALUE and "=" not in a:
                i += 1
            i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            for j, ch in enumerate(a[1:], start=1):
                if ch == "S":
                    return split_string
                if ch in _ENV_SHORT_VALUE:
                    if j == len(a) - 1:
                        i += 1
                    break
                if ch not in _ENV_SHORT_FLAGS:
                    return _unparseable("env", a)
            i += 1
            continue
        break
    while i < len(args) and "=" in args[i]:
        name = args[i].partition("=")[0]
        if forbidden_env_name(name):
            return PolicyDecision(
                Verdict.DENY,
                f"env {name}=... changes what git or the dynamic loader runs without "
                "appearing in the command",
                risk_level=RiskLevel.CRITICAL,
            )
        i += 1
    return list(args[i:])


def _embedded_git_denial(
    args: Sequence[str], owned_worktrees: Iterable[str]
) -> PolicyDecision | None:
    owned = list(owned_worktrees)
    for i, a in enumerate(args):
        name = _base(a)
        if name == "git":
            decision = check_git(["git", *args[i + 1 :]], owned_worktrees=owned)
        elif name.startswith("git-") and len(name) > 4:
            decision = check_git(["git", name[4:], *args[i + 1 :]], owned_worktrees=owned)
        else:
            continue
        if decision.verdict == Verdict.DENY:
            return decision
    return None


def check_command(
    argv: Sequence[str],
    *,
    allowlist: Iterable[str] | None = None,
    elevated: bool = False,
    owned_worktrees: Iterable[str] = (),
    trusted_commands: Iterable[Sequence[str]] = (),
) -> PolicyDecision:
    """Decide whether a command may run: deny, require approval, or allow.

    ``trusted_commands`` are exact argument arrays an operator configured (a project's
    validation commands); an exact match is allowed after the hard-disallow checks.
    """
    if not argv:
        return PolicyDecision(Verdict.DENY, "Empty command", risk_level=RiskLevel.LOW)
    trusted_commands = [list(t) for t in trusted_commands]
    exe = _base(argv[0])
    args = list(argv[1:])
    if exe in _SHELLS:
        return PolicyDecision(
            Verdict.DENY,
            "Shells are never started: pass the program and its arguments directly",
            risk_level=RiskLevel.CRITICAL,
            disallowed=DisallowedAction.HOST_FILE_DELETION,
        )
    if exe in _PRIVILEGE:
        return PolicyDecision(
            Verdict.DENY,
            "Privilege escalation (sudo/su/doas) is never run by AgentTape",
            risk_level=RiskLevel.CRITICAL,
        )
    if exe in _DELETERS or (exe == "find" and "-delete" in args):
        return deny(DisallowedAction.HOST_FILE_DELETION, exe)
    if exe in _ENV_DUMPERS - {"env"} and not [
        a for a in args if not a.startswith("-") and "=" not in a
    ]:
        return deny(DisallowedAction.DISPLAY_SECRETS, exe)
    inner: PolicyDecision | None = None
    if exe == "env" or exe in _WRAPPERS:
        # ``env VAR=1 program``, ``timeout 5 program``... run another program: judge it
        # (its hard denies always win) and the wrapper itself.
        unwrapped = _unwrap_env(args) if exe == "env" else _unwrap(exe, args)
        if isinstance(unwrapped, PolicyDecision):
            return unwrapped
        if unwrapped:
            inner = check_command(
                unwrapped,
                allowlist=allowlist,
                elevated=elevated or exe != "env",
                owned_worktrees=owned_worktrees,
            )
            if inner.verdict == Verdict.DENY:
                return inner
        elif exe == "env":
            return deny(DisallowedAction.DISPLAY_SECRETS, "env without a command")
    if exe == "gh" and args[:2] in (["pr", "merge"], ["repo", "delete"]):
        return deny(DisallowedAction.AUTOMATIC_MERGE, "gh " + " ".join(args[:2]))
    if exe.startswith("git-") and len(exe) > 4:
        # ``/usr/lib/git-core/git-push`` is ``git push`` by another name.
        decision = check_git(["git", exe[4:], *args], owned_worktrees=owned_worktrees)
        if decision.verdict != Verdict.ALLOW:
            return decision
    if exe == "git":
        decision = check_git(argv, owned_worktrees=owned_worktrees)
        if decision.verdict != Verdict.ALLOW:
            return decision
    else:
        # ``npm exec git push``, ``setsid git push``, ``flock l git push``...: any git
        # invocation inside another program's arguments keeps its hard denies.
        embedded = _embedded_git_denial(args, owned_worktrees)
        if embedded is not None:
            return embedded
    decision = _judge_self(argv, exe, allowlist, elevated, trusted_commands)
    if inner is not None and _STRICTNESS[inner.verdict] > _STRICTNESS[decision.verdict]:
        if any(list(t) == list(argv) for t in trusted_commands):
            return decision
        return inner
    return decision


_STRICTNESS = {Verdict.ALLOW: 0, Verdict.REQUIRE_APPROVAL: 1, Verdict.DENY: 2}


def _judge_self(
    argv: Sequence[str],
    exe: str,
    allowlist: Iterable[str] | None,
    elevated: bool,
    trusted_commands: Iterable[Sequence[str]],
) -> PolicyDecision:
    """The allowlist / elevated / trusted-command decision for the executable itself."""
    if elevated:
        return needs_approval(
            ApprovalType.ELEVATED_COMMAND,
            "The command is marked elevated",
            argv=list(argv),
        )
    if any(list(t) == list(argv) for t in trusted_commands):
        return allow("Exact match of an operator-configured validation command")
    allowed = {
        _base(a) for a in (allowlist if allowlist is not None else DEFAULT_COMMAND_ALLOWLIST)
    }
    if exe not in allowed:
        return needs_approval(
            ApprovalType.ELEVATED_COMMAND,
            f"'{exe}' is not on the command allowlist",
            argv=list(argv),
            allowlist=sorted(allowed),
        )
    return allow(f"'{exe}' is on the command allowlist")


# --------------------------------------------------------------------------- other checks


def matches_protected(path: str, patterns: Iterable[str]) -> list[str]:
    """Protected-path patterns (glob, ``**`` allowed) matching ``path``."""
    norm = posixpath.normpath(path.replace("\\", "/")).removeprefix("./").lstrip("/") or path
    hits: list[str] = []
    for pattern in patterns:
        pat = pattern.strip()
        if not pat:
            continue
        candidates = {pat, pat.replace("**/", ""), pat.rstrip("/") + "/**"}
        if any(
            fnmatch.fnmatchcase(norm, c) or fnmatch.fnmatchcase(PurePosixPath(norm).name, c)
            for c in candidates
        ):
            hits.append(pattern)
    return hits


def check_protected_write(paths: Iterable[str], patterns: Iterable[str]) -> PolicyDecision:
    pats = list(patterns)
    hits = {p: matches_protected(p, pats) for p in paths}
    hit = {p: m for p, m in hits.items() if m}
    if hit:
        return needs_approval(
            ApprovalType.PROTECTED_PATH,
            f"{len(hit)} protected path(s) would be written",
            paths=sorted(hit),
            patterns=sorted({m for ms in hit.values() for m in ms}),
        )
    return allow("No protected paths are written")


def check_budget_change(
    current: dict[str, Any] | None, proposed: dict[str, Any] | None
) -> PolicyDecision:
    """Raising any established limit (or removing one) needs approval; lowering does not."""
    cur = dict(current or {})
    new = dict(proposed or {})
    increases: dict[str, Any] = {}
    for key, old in cur.items():
        if old is None:
            continue
        value = new.get(key)
        if value is None or (
            isinstance(old, int | float) and isinstance(value, int | float) and value > old
        ):
            increases[key] = {"from": old, "to": value}
    if increases:
        return needs_approval(
            ApprovalType.BUDGET_INCREASE,
            "Raising or removing an established budget limit requires approval",
            increases=increases,
        )
    return allow("No established limit increases")


def check_evaluation_override(*, override_permitted: bool, failed: list[str]) -> PolicyDecision:
    if not failed:
        return allow("All required evaluations passed")
    if not override_permitted:
        return PolicyDecision(
            Verdict.DENY,
            "Required evaluation failed and override is not permitted: " + ", ".join(failed),
            risk_level=RiskLevel.HIGH,
        )
    return needs_approval(
        ApprovalType.EVALUATION_OVERRIDE,
        "Continuing after a failed required evaluation requires approval",
        failed=failed,
    )


def check_commit() -> PolicyDecision:
    return needs_approval(ApprovalType.GIT_COMMIT, "Committing Git changes requires approval")
