"""Path containment for observed sessions: ``_contains`` and the
safe file probe ``_is_file_inside``."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenttape.application.services.observed_sessions import _contains, _is_file_inside
from agenttape.capture.claude_hooks import HookPayloadError, is_absolute_path, parse_hook


def test_contains_same_directory() -> None:
    assert _contains("/r/proj", "/r/proj") is True


def test_contains_subdirectory() -> None:
    assert _contains("/r/proj", "/r/proj/src") is True


def test_contains_sibling_directory_is_not_contained() -> None:
    assert _contains("/r/proj", "/r/project2") is False


def test_contains_root_of_filesystem() -> None:
    """The old ``startswith(rstrip("/") + "/")`` form produced ``//`` and failed this."""
    assert _contains("/", "/anything") is True


def test_is_file_inside_true_for_an_existing_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x")
    assert _is_file_inside(root, "src/a.py") is True


def test_is_file_inside_refuses_an_absolute_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert _is_file_inside(root, "/etc/passwd") is False


def test_is_file_inside_refuses_a_parent_escaping_path(tmp_path: Path) -> None:
    """Even when the target file exists next to ``root``, ``..`` must never resolve
    outside it - both cases fail before the fix because ``root / rel`` replaces or
    escapes ``root``."""
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("secret")
    assert _is_file_inside(root, "../outside.txt") is False


def test_contains_fails_closed_when_the_caller_path_cannot_be_resolved() -> None:
    """A NUL byte makes ``Path.resolve()`` raise; the lexical fallback used to treat
    ``/repo/../../etc\\x00`` as inside ``/repo``. Any resolution error now means "not
    contained"."""
    assert _contains("/r/proj", "/r/proj/../../../../etc\x00") is False
    assert _contains("/r/proj\x00", "/r/proj") is False


def test_contains_is_false_for_a_symlink_loop_rather_than_raising(tmp_path: Path) -> None:
    """On Python 3.12 a path through a symlink loop raises ``RuntimeError`` from
    ``resolve()``; it must be a clean "not contained", not an exception (a 500 on the MCP
    path, an aborted project lookup on the hooks path)."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "loop").symlink_to(root / "loop")
    assert _contains(str(root), str(root / "loop" / "x")) is False


@pytest.mark.parametrize("cwd", ["/r/proj\x00", "/r/pr\noj", "/r/proj\x7f", "/r/\tproj"])
def test_a_cwd_with_control_characters_is_not_an_absolute_path(cwd: str) -> None:
    """``is_absolute_path`` guards both ``parse_hook`` and the MCP router."""
    assert is_absolute_path(cwd) is False


def test_parse_hook_refuses_a_cwd_with_a_nul_byte() -> None:
    body = {"hook_event_name": "SessionStart", "session_id": "s", "cwd": "/r/proj/../..\x00"}
    with pytest.raises(HookPayloadError):
        parse_hook(body)
