"""Security review fixes: option injection, wrapper parsing, env allowlists, secrets."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agenttape.adapters.base import build_child_env
from agenttape.application.schemas.catalog import ProjectSettings
from agenttape.domain.policy import Verdict, check_command, check_git
from agenttape.security.secrets import EnvSecretResolver, SecretLocator, SecretUnavailableError
from agenttape.workflows.nodes.command import injected_arguments

# --------------------------------------------------------------------------- finding 1


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "grep", "--open-files-in-pager=touch /tmp/pwned", "x"],
        ["git", "grep", "--open-files", "x"],  # git accepts unambiguous prefixes
        ["git", "grep", "-Otouch", "x"],
        ["git", "grep", "-iO", "x"],
        ["git", "log", "--output=/tmp/x"],
        ["git", "diff", "--output", "/tmp/x"],
        ["git", "format-patch", "-o", "/tmp/out", "HEAD~1"],
        ["git", "format-patch", "--output-directory=/tmp/out"],
        ["git", "fetch", "--upload-pack=sh -c 'git push origin HEAD:main'", "."],
        ["git", "fetch", "--upload-p=x", "."],
        ["git", "clone", "-u", "x", "src", "dst"],
        ["git", "ls-remote", "--upload-pack=x", "."],
        ["git", "archive", "--remote=.", "--exec=x", "HEAD"],
        ["git", "push", "--receive-pack=x"],
        ["git", "clone", "--template=/tmp/hooks", "src", "dst"],
        ["git", "clone", "--config", "core.fsmonitor=x", "src", "dst"],
        ["git", "-c", "core.sshCommand=evil", "fetch"],
        ["git", "-c", "core.hooksPath=/tmp", "status"],
        ["git", "-c", "core.pager=sh", "log"],
        ["git", "--config-env", "core.pager=PAGER_VAR", "log"],
    ],
)
def test_git_options_that_run_programs_or_write_files_are_denied(argv: list[str]) -> None:
    decision = check_command(argv)
    assert decision.verdict == Verdict.DENY, decision


def test_fetch_is_no_longer_read_only() -> None:
    assert check_git(["git", "fetch", "origin"]).verdict == Verdict.REQUIRE_APPROVAL


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "log", "-1", "--format=%s"],
        ["git", "ls-files", "-o", "--exclude-standard"],  # -o is --others here
        ["git", "grep", "-n", "TODO"],
        ["git", "diff", "--output-indicator-new=+"],
        ["git", "log", "--", "--output"],  # a pathspec after --, not an option
    ],
)
def test_harmless_git_forms_stay_allowed(argv: list[str]) -> None:
    assert check_command(argv).verdict == Verdict.ALLOW


def test_safe_git_config_overrides_still_need_approval() -> None:
    decision = check_git(["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-m", "x"])
    assert decision.verdict == Verdict.REQUIRE_APPROVAL


def test_template_values_cannot_start_an_option() -> None:
    template = ["git", "log", "{{ inputs.ref }}", "--grep={{ inputs.q }}", "x{{ inputs.ref }}"]
    rendered = ["git", "log", "--output=/tmp/x", "--grep=-v", "x--output"]
    problems = injected_arguments(template, rendered)
    assert len(problems) == 1 and "argument 2" in problems[0]
    assert injected_arguments(template, ["git", "log", "main", "--grep=a", "xmain"]) == []
    # Two substitutions where the first renders empty still start the argument.
    assert injected_arguments(["ls", "{{ a }}{{ b }}"], ["ls", "-la"])


def test_template_values_cannot_add_assignments_for_env_or_make() -> None:
    assert injected_arguments(["make", "{{ inputs.t }}"], ["make", "SHELL=/tmp/x"])
    assert injected_arguments(["make", "{{ inputs.t }}"], ["make", "test"]) == []
    assert injected_arguments(["pytest", "{{ inputs.t }}"], ["pytest", "k=v"]) == []


# --------------------------------------------------------------------------- finding 2


@pytest.mark.parametrize(
    "argv",
    [
        ["env", "-u", "pytest", "git", "push", "origin", "HEAD:main"],
        ["env", "--unset=pytest", "git", "push"],
        ["env", "-C", "/tmp", "git", "push"],
        ["env", "-i", "X=1", "git", "push"],
        ["env", "-S", "git push"],
        ["env", "--split-string=git push"],
        ["env", "GIT_SSH_COMMAND=evil", "git", "status"],
        ["env", "LD_PRELOAD=/tmp/x.so", "pytest"],
        ["env", "-i"],
        ["nice", "-n", "5", "git", "push"],
        ["nice", "-5", "git", "push"],
        ["nice", "--adjustment", "5", "git", "push"],
        ["timeout", "5", "git", "push"],
        ["timeout", "-s", "KILL", "5", "git", "push"],
        ["timeout", "--kill-after", "1", "5", "git", "push"],
        ["stdbuf", "-o", "L", "git", "push"],
        ["ionice", "-c", "3", "git", "push"],
        ["xargs", "-n", "1", "git", "push"],
        ["setsid", "git", "push"],
        ["nohup", "git", "merge", "x"],
        ["npm", "exec", "--", "git", "push"],
        ["nice", "--frobnicate", "git", "status"],  # unknown option: cannot be judged
    ],
)
def test_wrappers_never_downgrade_a_deny(argv: list[str]) -> None:
    decision = check_command(argv, allowlist=[argv[0], "git", "pytest"])
    assert decision.verdict == Verdict.DENY, decision


def test_wrappers_must_themselves_be_allowed() -> None:
    assert check_command(["env", "X=1", "pytest"]).verdict == Verdict.REQUIRE_APPROVAL
    assert check_command(["env", "X=1", "pytest"], allowlist=["env", "pytest"]).allowed
    trusted = [["timeout", "600", "python", "-m", "pytest"]]
    assert check_command(trusted[0], trusted_commands=trusted).allowed


# --------------------------------------------------------------------------- finding 4


@pytest.mark.parametrize(
    "name", ["GIT_SSH_COMMAND", "GIT_CONFIG_PARAMETERS", "GIT_DIR", "LD_PRELOAD", "BASH_ENV"]
)
def test_project_env_allowlist_rejects_forbidden_names(name: str) -> None:
    with pytest.raises(ValidationError):
        ProjectSettings(env_allowlist=[name])


def test_project_env_allowlist_rejects_non_names_and_keeps_good_ones() -> None:
    with pytest.raises(ValidationError):
        ProjectSettings(env_allowlist=["bad name"])
    assert ProjectSettings(env_allowlist=["CI", "HTTP_PROXY"]).env_allowlist == [
        "CI",
        "HTTP_PROXY",
    ]


def test_child_env_drops_forbidden_allowlisted_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_SSH_COMMAND", "evil")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/x.so")
    monkeypatch.setenv("SAFE_ONE", "ok")
    env = build_child_env(["GIT_SSH_COMMAND", "LD_PRELOAD", "SAFE_ONE"], {})
    assert env["SAFE_ONE"] == "ok"
    assert "GIT_SSH_COMMAND" not in env and "LD_PRELOAD" not in env


# --------------------------------------------------------------------------- finding 3


def test_project_validation_commands_are_checked_at_save_time() -> None:
    with pytest.raises(ValidationError):
        ProjectSettings(validation_commands=[["git", "push", "origin", "main"]])
    with pytest.raises(ValidationError):
        ProjectSettings(validation_commands=[["env", "-u", "x", "git", "push"]])
    ok = ProjectSettings(validation_commands=[["python", "-m", "compileall", "-q", "src"]])
    assert ok.validation_commands


# --------------------------------------------------------------------------- finding 5


def test_env_fallback_never_reads_agenttape_settings() -> None:
    env = {"AGENTTAPE_DATABASE_URL": "postgresql://u:p@h/db", "DIRECT": "v"}
    resolver = EnvSecretResolver(environ=env)
    assert resolver.resolve("DIRECT") == "v"
    with pytest.raises(SecretUnavailableError):
        resolver.resolve("AGENTTAPE_DATABASE_URL")
    explicit = EnvSecretResolver(
        {"AGENTTAPE_DATABASE_URL": SecretLocator("env", "AGENTTAPE_DATABASE_URL")}, environ=env
    )
    assert explicit.resolve("AGENTTAPE_DATABASE_URL").startswith("postgresql")
