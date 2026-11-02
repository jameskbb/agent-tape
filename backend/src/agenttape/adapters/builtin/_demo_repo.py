"""The Demo Agent's fixture Git repository.

AgentTape creates and owns this repository under ``<data_dir>/fixtures/demo-repo``. The
demo adapter never writes to it directly: each attempt clones it into
``<data_dir>/workspaces/demo/<run_id>/attempt-<n>`` and edits the clone, so user
repositories are never touched and concurrent demo runs cannot interfere.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from agenttape.git.invoke import run_git_hardened

FIXTURE_NAME = "demo-repo"

GREETER_V1 = '''"""Tiny greeting service used by the AgentTape demo project."""


def greet(name):
    return "Hello, " + name + "!"
'''

GREETER_V2 = '''"""Tiny greeting service used by the AgentTape demo project."""


def greet(name: str) -> str:
    """Return a friendly greeting for ``name``.

    Leading and trailing whitespace is ignored; an empty name is rejected.
    """
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("name must not be empty")
    return f"Hello, {cleaned}!"
'''

TEST_FILE = """from src.greeter import greet


def test_greet_strips_whitespace() -> None:
    assert greet("  Ada ") == "Hello, Ada!"


def test_greet_rejects_empty_name() -> None:
    try:
        greet("   ")
    except ValueError:
        return
    raise AssertionError("expected ValueError")
"""

README = """# AgentTape demo project

A deliberately tiny repository owned by AgentTape. The built-in Demo Agent clones it for
every run and makes a small, safe change so you can inspect a real diff.
"""

CHANGELOG_V1 = """# Changelog

## Unreleased

- Initial greeting service.
"""

CHANGELOG_ENTRY = "- Validate names in `greet()` and add unit tests (Demo Agent).\n"


def git_env() -> dict[str, str]:
    """An isolated Git environment: no user/system config, hooks, prompts, or signing."""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", tempfile.gettempdir()),
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "AgentTape Demo",
        "GIT_AUTHOR_EMAIL": "demo@agenttape.invalid",
        "GIT_COMMITTER_NAME": "AgentTape Demo",
        "GIT_COMMITTER_EMAIL": "demo@agenttape.invalid",
        "GIT_AUTHOR_DATE": "2026-01-01T09:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T09:00:00Z",
    }


def fixture_path(data_dir: Path) -> Path:
    return data_dir / "fixtures" / FIXTURE_NAME


def _git(repo: Path, *args: str) -> None:
    # The hardened invoker (the one way AgentTape runs Git), with the demo identity and
    # no global config.
    env = {
        k: v
        for k, v in git_env().items()
        if k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_")) or k == "GIT_CONFIG_GLOBAL"
    }
    result = run_git_hardened(repo, *args, env=env, timeout=30)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )


def ensure_fixture_repo(data_dir: Path) -> Path:
    """Create the fixture repository if missing (idempotent). Returns its path."""
    repo = fixture_path(data_dir)
    if (repo / ".git").is_dir():
        return repo
    tmp = repo.with_name(f"{repo.name}.tmp-{uuid.uuid4().hex[:8]}")
    (tmp / "src").mkdir(parents=True)
    (tmp / "README.md").write_text(README, encoding="utf-8")
    (tmp / "CHANGELOG.md").write_text(CHANGELOG_V1, encoding="utf-8")
    (tmp / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp / "src" / "greeter.py").write_text(GREETER_V1, encoding="utf-8")
    _git(tmp, "init", "--quiet", "--initial-branch=main")
    _git(tmp, "add", "-A")
    _git(tmp, "commit", "--quiet", "-m", "Initial greeting service")
    (tmp / "CHANGELOG.md").write_text(
        CHANGELOG_V1 + "- Document the project in README.\n", encoding="utf-8"
    )
    _git(tmp, "commit", "--quiet", "-am", "Document the demo project")
    repo.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.rename(repo)
    except OSError:
        # Another process created it concurrently; keep theirs.
        shutil.rmtree(tmp, ignore_errors=True)
        if not (repo / ".git").is_dir():
            raise
    return repo
