"""Bounded, conservatively parsed read-only shell reads: cat, head, tail, sed -n.

Accept/reject table for the grammar in ``domain/shell_reads.py``. The command
string is only ever parsed here, never executed.
"""

from __future__ import annotations

import pytest

from agenttape.domain.shell_reads import ShellRead, classify_shell_read


def test_cat_single_path() -> None:
    assert classify_shell_read("cat src/a.py") == ShellRead(
        program="cat", paths=("src/a.py",), whole_file=True
    )


def test_cat_numbered_multiple_paths() -> None:
    assert classify_shell_read("cat -n a.py b.py") == ShellRead(
        program="cat", paths=("a.py", "b.py"), whole_file=True, numbered=True
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "head -n 20 a.py",
            ShellRead(program="head", paths=("a.py",), line_start=1, line_end=20),
        ),
        (
            "head -20 a.py",
            ShellRead(program="head", paths=("a.py",), line_start=1, line_end=20),
        ),
        (
            "head --lines=20 a.py",
            ShellRead(program="head", paths=("a.py",), line_start=1, line_end=20),
        ),
        (
            "head a.py",
            ShellRead(program="head", paths=("a.py",), line_start=1, line_end=10),
        ),
    ],
)
def test_head_forms(command: str, expected: ShellRead) -> None:
    assert classify_shell_read(command) == expected


def test_tail_from_end_default_and_explicit() -> None:
    assert classify_shell_read("tail a.py") == ShellRead(
        program="tail", paths=("a.py",), from_end=10
    )
    assert classify_shell_read("tail -n 5 a.py") == ShellRead(
        program="tail", paths=("a.py",), from_end=5
    )
    assert classify_shell_read("tail -n5 a.py") == ShellRead(
        program="tail", paths=("a.py",), from_end=5
    )
    assert classify_shell_read("tail -5 a.py") == ShellRead(
        program="tail", paths=("a.py",), from_end=5
    )


def test_tail_from_start() -> None:
    assert classify_shell_read("tail -n +40 a.py") == ShellRead(
        program="tail", paths=("a.py",), line_start=40
    )
    assert classify_shell_read("tail -n+40 a.py") == ShellRead(
        program="tail", paths=("a.py",), line_start=40
    )
    assert classify_shell_read("tail --lines=+40 a.py") == ShellRead(
        program="tail", paths=("a.py",), line_start=40
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "sed -n '10,20p' a.py",
            ShellRead(program="sed", paths=("a.py",), line_start=10, line_end=20),
        ),
        (
            "sed -n -e 10,20p a.py",
            ShellRead(program="sed", paths=("a.py",), line_start=10, line_end=20),
        ),
        (
            "sed -n '7p' a.py",
            ShellRead(program="sed", paths=("a.py",), line_start=7, line_end=7),
        ),
        (
            "sed -n '3,$p' a.py",
            ShellRead(program="sed", paths=("a.py",), line_start=3, line_end=None),
        ),
    ],
)
def test_sed_forms(command: str, expected: ShellRead) -> None:
    assert classify_shell_read(command) == expected


@pytest.mark.parametrize("command", ["/usr/bin/cat a.py", "/bin/cat a.py"])
def test_absolute_program_paths_are_accepted(command: str) -> None:
    r = classify_shell_read(command)
    assert r is not None and r.program == "cat"


def test_dash_dash_allows_a_dash_leading_path_only_for_cat() -> None:
    assert classify_shell_read("cat -- -notes.txt") == ShellRead(
        program="cat", paths=("-notes.txt",), whole_file=True
    )


@pytest.mark.parametrize(
    "command",
    [
        "cat a.py | head",
        "cat a.py > b",
        "cat a.py; rm b",
        "cat $F",
        "cat *.py",
        "cat ~/x",
        "FOO=1 cat a",
        "cat -A a",
        "cat -rf",
        "head -c 10 a",
        "tail -f a",
        "sed -i 's/a/b/' a",
        "sed -n '10,20p' a b",
        "sed '10,20p' a",
        "sed -n '20,10p' a",
        "sed -n '/x/p' a",
        "head -n 5 a b",
        "grep x a",
        "",
        "cat",
        "./cat a.py",
        "busybox cat a.py",
    ],
)
def test_rejected_forms(command: str) -> None:
    assert classify_shell_read(command) is None


@pytest.mark.parametrize("ch", list("|&;<>`$(){}*?[]~!#\\"))
def test_every_reject_character_is_rejected(ch: str) -> None:
    assert classify_shell_read(f"cat a{ch}b.py") is None


def test_newline_and_carriage_return_are_rejected() -> None:
    assert classify_shell_read("cat a.py\n") is None
    assert classify_shell_read("cat a.py\r") is None


def test_command_longer_than_2000_characters_is_rejected() -> None:
    assert classify_shell_read("cat " + "a" * 2000 + ".py") is None


def test_non_string_command_is_rejected() -> None:
    assert classify_shell_read(None) is None
    assert classify_shell_read(123) is None


def test_unbalanced_quotes_are_rejected() -> None:
    assert classify_shell_read("cat 'a.py") is None


def test_cat_path_cap_is_sixteen() -> None:
    paths = " ".join(f"f{i}.py" for i in range(16))
    r = classify_shell_read(f"cat {paths}")
    assert r is not None and len(r.paths) == 16
    too_many = " ".join(f"f{i}.py" for i in range(17))
    assert classify_shell_read(f"cat {too_many}") is None


def test_shell_read_as_details_contains_only_meaningful_fields() -> None:
    cat = classify_shell_read("cat a.py")
    assert cat is not None and cat.as_details() == {"program": "cat", "whole_file": True}
    sed = classify_shell_read("sed -n '2,3p' a.py")
    assert sed is not None and sed.as_details() == {
        "program": "sed",
        "line_start": 2,
        "line_end": 3,
    }
    tail = classify_shell_read("tail -n 5 a.py")
    assert tail is not None and tail.as_details() == {"program": "tail", "from_end": 5}
