"""What may be replayed, and how a re-run is compared: the pure logic.

Pure unit tests: no database, no ``Session``, matching ``findings/reproduce.py``'s own
"pure, no I/O" convention.
"""

from __future__ import annotations

import pytest

from agenttape.findings.reproduce import (
    GIT_GLOBAL_OPTION_NOT_REPLAYABLE,
    GIT_INDIRECT_NOT_REPLAYABLE,
    GIT_SUBCOMMAND_NOT_REPLAYABLE,
    RecordedCommand,
    argv_from_command_string,
    classify,
    comparable,
    compare,
    git_refusal,
    recorded_command,
    unsafe_argument,
)


def _recorded(
    argv: list[str],
    *,
    cwd: str | None = ".",
    exit_code: int | None = 0,
    failed: bool | None = None,
    output: str | None = "",
    output_truncated: bool = False,
) -> RecordedCommand:
    return RecordedCommand(
        argv=tuple(argv),
        cwd=cwd,
        cwd_known=cwd is not None,
        exit_code=exit_code,
        failed=failed,
        output=output,
        output_truncated=output_truncated,
        source="command",
    )


# --------------------------------------------------------------------------- recorded_command


def test_recorded_command_extracts_a_command_event() -> None:
    payload = {"argv": ["grep", "-n", "x"], "cwd": ".", "exit_code": 0, "output": "x:1:x"}
    result = recorded_command("command", payload)
    assert isinstance(result, RecordedCommand)
    assert result.argv == ("grep", "-n", "x")
    assert result.exit_code == 0
    assert result.output == "x:1:x"


def test_recorded_command_refuses_a_non_command_event() -> None:
    assert isinstance(recorded_command("tool_result", {"output": "hi"}), str)


def test_recorded_command_refuses_an_empty_argv() -> None:
    assert isinstance(recorded_command("command", {"argv": []}), str)
    assert isinstance(recorded_command("command", {}), str)


def test_recorded_command_extracts_a_claude_code_bash_tool_result() -> None:
    call_payload = {
        "tool": "Bash",
        "input": {"command": "grep -rn 'def greet' src"},
        "tool_use_id": "t1",
    }
    result_payload = {
        "tool_use_id": "t1",
        "output": "src/greeter.py:1:def greet():",
        "is_error": False,
    }
    result = recorded_command("tool_result", result_payload, call_payload=call_payload)
    assert isinstance(result, RecordedCommand)
    assert result.argv == ("grep", "-rn", "def greet", "src")
    assert result.output == "src/greeter.py:1:def greet():"
    assert result.failed is False
    assert result.exit_code is None
    assert result.cwd_known is False


def test_recorded_command_extracts_a_codex_shell_tool_call() -> None:
    call_payload = {"tool": "shell", "input": {"command": "grep -rn x src"}, "tool_use_id": "t2"}
    result_payload = {"tool_use_id": "t2", "output": "x", "is_error": True, "exit_code": 1}
    result = recorded_command("tool_call", call_payload, result_payload=result_payload)
    assert isinstance(result, RecordedCommand)
    assert result.exit_code == 1
    assert result.failed is True
    assert result.cwd_known is False


def test_recorded_command_refuses_a_non_command_tool() -> None:
    call_payload = {"tool": "Read", "input": {"file_path": "a.py"}, "tool_use_id": "t3"}
    result_payload = {"tool_use_id": "t3", "output": "1\tx", "is_error": False}
    reason = recorded_command("tool_result", result_payload, call_payload=call_payload)
    assert isinstance(reason, str)


def test_recorded_command_refuses_a_tool_result_with_no_parent_call() -> None:
    result_payload = {"tool_use_id": "t3", "output": "x", "is_error": False}
    assert isinstance(recorded_command("tool_result", result_payload, call_payload=None), str)


def test_recorded_command_bubbles_up_a_shell_syntax_refusal() -> None:
    call_payload = {
        "tool": "Bash",
        "input": {"command": "grep -rn x src | head -1"},
        "tool_use_id": "t4",
    }
    reason = recorded_command("tool_call", call_payload)
    assert isinstance(reason, str)
    assert "|" in reason


# --------------------------------------------------------------------- argv_from_command_string


def test_argv_from_command_string_tokenizes_single_quotes() -> None:
    result = argv_from_command_string("grep -rn 'def greet' src")
    assert result == ["grep", "-rn", "def greet", "src"]


def test_argv_from_command_string_tokenizes_double_quotes() -> None:
    result = argv_from_command_string('grep -rn "def greet" src')
    assert result == ["grep", "-rn", "def greet", "src"]


@pytest.mark.parametrize(
    ("text", "must_name"),
    [
        ("grep -rn x src | head -1", "|"),
        ("true && false", "&"),
        ("true; false", ";"),
        ("cat < file", "<"),
        ("cat > file", ">"),
        ("echo (x)", "("),
        ("echo x)", ")"),
        ("echo $VAR", "$"),
        ("echo `x`", "`"),
        ("echo a\\b", "\\"),
        ("echo a*b", "*"),
        ("echo a?b", "?"),
        ("echo [ab]", "["),
        ("echo ab]", "]"),
        ("echo {a,b}", "{"),
        ("echo a}", "}"),
        ("echo ~", "~"),
        ("echo a!b", "!"),
        ("echo a#b", "#"),
    ],
)
def test_argv_from_command_string_refuses_shell_syntax_naming_the_character(
    text: str, must_name: str
) -> None:
    reason = argv_from_command_string(text)
    assert isinstance(reason, str)
    assert must_name in reason


def test_argv_from_command_string_refuses_a_newline() -> None:
    assert isinstance(argv_from_command_string("grep -rn x\nsrc"), str)


def test_argv_from_command_string_refuses_a_carriage_return() -> None:
    assert isinstance(argv_from_command_string("grep -rn x\rsrc"), str)


def test_argv_from_command_string_refuses_an_empty_string() -> None:
    assert isinstance(argv_from_command_string(""), str)


def test_argv_from_command_string_refuses_an_unbalanced_quote() -> None:
    assert isinstance(argv_from_command_string("grep -rn 'def greet src"), str)


def test_argv_from_command_string_refuses_over_2000_characters() -> None:
    assert isinstance(argv_from_command_string("echo " + "a" * 2000), str)


def test_argv_from_command_string_refuses_a_leading_assignment() -> None:
    assert isinstance(argv_from_command_string("FOO=bar grep -rn x src"), str)


# --------------------------------------------------------------------------- classify


@pytest.mark.parametrize(
    "argv",
    [
        ["grep", "-rn", "x", "src"],
        ["egrep", "x", "src"],
        ["fgrep", "x", "src"],
        ["rg", "x"],
        ["git", "grep", "-n", "x"],
        ["/usr/bin/grep", "-n", "x"],
    ],
)
def test_classify_search(argv: list[str]) -> None:
    assert classify(argv) == "search"


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "log"],
        ["python3", "-m", "compileall", "-q", "src"],
        ["pytest"],
        [],
    ],
)
def test_classify_command(argv: list[str]) -> None:
    assert classify(argv) == "command"


# --------------------------------------------------------------------------- unsafe_argument


@pytest.mark.parametrize(
    "argv",
    [
        ["grep", "-rn", "def greet", "src"],
        ["git", "grep", "-n", "x"],
    ],
)
def test_unsafe_argument_allows_ordinary_searches(argv: list[str]) -> None:
    assert unsafe_argument(argv, "search") is None


def test_unsafe_argument_refuses_a_redacted_value() -> None:
    reason = unsafe_argument(["grep", "-rn", "[REDACTED:api_key]", "src"], "search")
    assert reason is not None


def test_unsafe_argument_refuses_a_secret_placeholder() -> None:
    reason = unsafe_argument(["grep", "-rn", "[secret:x]", "src"], "search")
    assert reason is not None


@pytest.mark.parametrize(
    "argv",
    [
        ["grep", "-rn", "x", "/etc"],
        ["grep", "-rn", "x", "~/secrets"],
        ["grep", "-rn", "x", "../outside"],
        ["grep", "--file=/etc/passwd", "src"],
        ["git", "-C", "/etc", "grep", "-n", "x"],
    ],
)
def test_unsafe_argument_refuses_a_path_outside_the_repository(argv: list[str]) -> None:
    reason = unsafe_argument(argv, "search")
    assert reason is not None


@pytest.mark.parametrize(
    "argv",
    [
        ["rg", "--pre", "cat", "x"],
        ["rg", "--pre=cat", "x"],
        ["rg", "--pre-glob", "*.gz", "x"],
        ["rg", "-z", "x"],
        ["rg", "-vz", "x"],
        ["rg", "--search-zip", "x"],
        ["rg", "--hostname-bin=foo", "x"],
    ],
)
def test_unsafe_argument_refuses_rg_program_options(argv: list[str]) -> None:
    reason = unsafe_argument(argv, "search")
    assert reason is not None


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "grep", "-O", "x"],
        ["git", "grep", "-Ovim", "x"],
        ["git", "grep", "--open-files-in-pager=less", "x"],
        ["git", "grep", "--textconv", "x"],
        ["git", "-c", "core.pager=less", "grep", "-n", "x"],
    ],
)
def test_unsafe_argument_refuses_git_grep_program_options(argv: list[str]) -> None:
    reason = unsafe_argument(argv, "search")
    assert reason is not None


def test_unsafe_argument_path_check_applies_regardless_of_mode() -> None:
    assert unsafe_argument(["python3", "-m", "pytest", "/etc/passwd"], "command") is not None


@pytest.mark.parametrize(
    "argv",
    [
        ["/tmp/evil/grep", "foo", "src"],
        ["/home/x/.local/bin/pytest", "-q"],
        ["../../tmp/grep", "foo"],
        ["./scripts/grep", "foo"],
    ],
)
def test_unsafe_argument_refuses_a_program_named_by_a_path(argv: list[str]) -> None:
    """Only a bare program name is ever replayed (and then looked up only in
    the system directories); a program named by any path (absolute, relative, ./, ../) is refused in
    every mode, whatever classification the argv was given."""
    for mode in ("search", "command"):
        assert unsafe_argument(argv, mode) is not None, (argv, mode)


def test_classify_is_unchanged_for_a_path_qualified_search_program() -> None:
    """The path refusal lives in unsafe_argument, not classify: a path-qualified grep is
    still classified as a search (the reproduction still queues it automatically; it is
    unsafe_argument, not classify, that refuses to run it)."""
    assert classify(["/tmp/evil/grep", "foo", "src"]) == "search"


@pytest.mark.parametrize(
    "argv",
    [
        ["rg", "--pre", "cat", "x"],
        ["rg", "-vz", "x"],
        ["git", "grep", "-O", "x"],
        ["git", "grep", "--textconv", "x"],
    ],
)
def test_unsafe_argument_program_options_are_refused_in_every_mode(argv: list[str]) -> None:
    """Keyed on the executable, never on the mode the argv was classified as."""
    for mode in ("search", "command"):
        assert unsafe_argument(argv, mode) is not None, (argv, mode)


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "-C", "sub", "log", "-1"],
        ["git", "-C", "sub", "branch", "experimental"],
        ["git", "--git-dir=sub/.git", "status"],
        ["git", "--git-dir", "sub/.git", "status"],
        ["git", "--work-tree=subdir", "diff"],
        ["git", "--work-tree", "subdir", "status"],
        ["git", "-c", "core.pager=less", "log"],
        ["git", "-c", "user.name=x", "show"],
        ["git", "--exec-path=bin", "status"],
        ["git", "--namespace", "x", "log"],
        ["git", "--no-pager", "log"],
        ["git", "--version"],
        ["git", "-C", "sub", "grep", "-n", "x"],
    ],
)
def test_git_global_options_are_refused_in_every_mode(argv: list[str]) -> None:
    """An in-repository-looking value (``-C sub``, ``--work-tree=subdir``) is
    refused by name, not only when it happens to look like an outside path."""
    for mode in ("search", "command"):
        refusal = git_refusal(argv, mode)
        assert refusal is not None, (argv, mode)
        assert refusal.code == GIT_GLOBAL_OPTION_NOT_REPLAYABLE
        assert unsafe_argument(argv, mode) == refusal.reason


# --------------------------------------------------------------------------- git_refusal


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "worktree", "add", "x", "HEAD"],
        ["git", "worktree", "move", "a", "b"],
        ["git", "worktree", "list"],
        ["git", "branch", "experimental"],
        ["git", "branch", "--list"],
        ["git", "tag", "v1"],
        ["git", "stash"],
        ["git", "remote", "add", "x", "y"],
        ["git", "config", "user.name", "x"],
        ["git", "checkout", "other"],
        ["git", "reset", "HEAD~1"],
        ["git", "add", "src"],
        ["git", "commit", "-m", "x"],
        ["git", "gc"],
        ["git", "st"],
        ["git"],
        ["/usr/bin/git", "branch", "experimental"],
    ],
)
def test_git_refusal_refuses_every_subcommand_outside_the_read_only_set(
    argv: list[str],
) -> None:
    """A re-run never relies on the command policy's git classification to
    stay read-only, in either mode."""
    for mode in ("command", "search"):
        refusal = git_refusal(argv, mode)
        assert refusal is not None, (argv, mode)
        assert refusal.code == GIT_SUBCOMMAND_NOT_REPLAYABLE
        assert unsafe_argument(argv, mode) == refusal.reason


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "status"],
        ["git", "diff", "--stat"],
        ["git", "log", "-1"],
        ["git", "show", "HEAD"],
        ["git", "rev-parse", "HEAD"],
        ["git", "ls-files"],
        ["git", "cat-file", "-p", "HEAD"],
        ["git", "blame", "src/greeter.py"],
        ["git", "describe", "--always"],
        ["git", "shortlog", "-s"],
        ["git", "grep", "-n", "x"],
    ],
)
def test_git_refusal_allows_the_read_only_subcommands(argv: list[str]) -> None:
    assert git_refusal(argv, "command") is None


@pytest.mark.parametrize(
    "argv",
    [
        ["git-worktree", "add", "x"],
        ["/usr/lib/git-core/git-branch", "experimental"],
        ["npm", "exec", "git", "tag", "v1"],
        ["env", "git", "branch", "experimental"],
        ["xargs", "/usr/bin/git", "branch"],
        ["uv", "run", "git-branch", "experimental"],
    ],
)
def test_git_refusal_refuses_git_run_indirectly(argv: list[str]) -> None:
    refusal = git_refusal(argv, "command")
    assert refusal is not None, argv
    assert refusal.code == GIT_INDIRECT_NOT_REPLAYABLE


@pytest.mark.parametrize(
    "argv",
    [
        ["pytest", "tests/git"],
        ["grep", "-rn", "git", "src"],
        ["make", "check"],
    ],
)
def test_git_refusal_ignores_git_as_a_plain_argument(argv: list[str]) -> None:
    """A relative ``tests/git`` directory, or ``git`` as a search pattern, is not a program."""
    mode = "search" if argv[0] == "grep" else "command"
    assert git_refusal(argv, mode) is None


# --------------------------------------------------------------------------- compare (search)


def test_compare_search_reordered_lines_reproduced() -> None:
    recorded = _recorded(["grep", "-rn", "x"], output="a:1:x\nb:2:x\n")
    result, _reason, _details = compare("search", recorded, exit_code=0, output="b:2:x\na:1:x\n")
    assert result == "reproduced"


def test_compare_search_extra_line_not_reproduced() -> None:
    recorded = _recorded(["grep", "-rn", "x"], output="a:1:x\n")
    result, reason, details = compare("search", recorded, exit_code=0, output="a:1:x\nb:2:x\n")
    assert result == "not_reproduced"
    assert details["recorded_line_count"] == 1
    assert details["rerun_line_count"] == 2
    assert "2" in reason or "1" in reason


def test_compare_search_missing_line_not_reproduced() -> None:
    recorded = _recorded(["grep", "-rn", "x"], output="a:1:x\nb:2:x\n")
    result, _reason, details = compare("search", recorded, exit_code=0, output="a:1:x\n")
    assert result == "not_reproduced"
    assert details["rerun_line_count"] == 1


def test_compare_search_different_exit_code_not_reproduced() -> None:
    recorded = _recorded(["grep", "-rn", "x"], exit_code=0, output="a:1:x\n")
    result, reason, _details = compare("search", recorded, exit_code=1, output="")
    assert result == "not_reproduced"
    assert "1" in reason


def test_compare_search_crlf_and_trailing_space_only_reproduced() -> None:
    recorded = _recorded(["grep", "-rn", "x"], output="a:1:x  \n")
    result, _reason, _details = compare("search", recorded, exit_code=0, output="a:1:x\r\n")
    assert result == "reproduced"


# --------------------------------------------------------------------------- compare (command)


def test_compare_command_equal_exit_codes_reproduced_ignoring_output() -> None:
    recorded = _recorded(["pytest"], exit_code=0, output="whatever ran last time")
    result, _reason, _details = compare(
        "command", recorded, exit_code=0, output="completely different output"
    )
    assert result == "reproduced"


def test_compare_command_different_exit_codes_names_both() -> None:
    recorded = _recorded(["pytest"], exit_code=0)
    result, reason, _details = compare("command", recorded, exit_code=1, output="")
    assert result == "not_reproduced"
    assert "1" in reason and "0" in reason


def test_compare_command_compared_by_failed_class_when_only_that_was_recorded() -> None:
    recorded = _recorded(["pytest"], exit_code=None, failed=True, output="")
    reproduced, _reason, _details = compare("command", recorded, exit_code=2, output="")
    not_reproduced, _reason2, _details2 = compare("command", recorded, exit_code=0, output="")
    assert reproduced == "reproduced"
    assert not_reproduced == "not_reproduced"


# --------------------------------------------------------------------------- comparable


def test_comparable_search_needs_recorded_output() -> None:
    assert comparable("search", _recorded(["grep", "x"], output=None)) is not None
    assert comparable("search", _recorded(["grep", "x"], output="")) is not None


def test_comparable_search_refuses_a_cut_output() -> None:
    recorded = _recorded(["grep", "x"], output="x:1:x", output_truncated=True)
    assert comparable("search", recorded) is not None


def test_comparable_search_refuses_an_error_report_without_an_exit_code() -> None:
    recorded = _recorded(["grep", "x"], exit_code=None, failed=True, output="x:1:x")
    assert comparable("search", recorded) is not None


def test_comparable_search_ok_with_output_and_exit_code() -> None:
    recorded = _recorded(["grep", "x"], exit_code=0, output="x:1:x")
    assert comparable("search", recorded) is None


def test_comparable_search_ok_with_output_and_only_failed_recorded() -> None:
    recorded = _recorded(["grep", "x"], exit_code=None, failed=False, output="x:1:x")
    assert comparable("search", recorded) is None


def test_comparable_command_needs_an_exit_code_or_a_failed_flag() -> None:
    assert comparable("command", _recorded(["pytest"], exit_code=None, failed=None)) is not None


def test_comparable_command_ok_with_only_failed_recorded() -> None:
    assert comparable("command", _recorded(["pytest"], exit_code=None, failed=True)) is None


def test_comparable_command_ok_with_exit_code() -> None:
    assert comparable("command", _recorded(["pytest"], exit_code=0)) is None
