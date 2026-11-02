"""Unified-diff parsing, rename detection, and protected-path matching."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agenttape.git.changes import capture_changes, pair_renames, rename_diff, similarity
from agenttape.git.diffparse import TRUNCATION_MARKER, parse_unified_diff, split_by_file
from agenttape.git.protected import is_protected, matches, matching_patterns
from agenttape.git.runner import GuardedGit

DIFF = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,4 +1,5 @@ def main():
 import os
-print("old")
+print("new")
+print("more")

 x = 1
\\ No newline at end of file
diff --git a/docs/old.md b/docs/new.md
similarity index 92%
rename from docs/old.md
rename to docs/new.md
index 3333333..4444444 100644
--- a/docs/old.md
+++ b/docs/new.md
@@ -3 +3 @@
-Old line
+New line
diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
index 0000000..5555555
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1,2 @@
+def test_it():
+    assert True
diff --git a/README.md b/README.md
deleted file mode 100644
index 6666666..0000000
--- a/README.md
+++ /dev/null
@@ -1,2 +0,0 @@
-# Title
-Body
diff --git a/logo.png b/logo.png
new file mode 100644
index 0000000..7777777
Binary files /dev/null and b/logo.png differ
"""


def test_parse_multi_file_diff() -> None:
    files = {f.path: f for f in parse_unified_diff(DIFF)}
    assert set(files) == {"src/app.py", "docs/new.md", "tests/test_new.py", "README.md", "logo.png"}

    app = files["src/app.py"]
    assert app.change_type == "modified"
    assert (app.insertions, app.deletions) == (2, 1)
    hunk = app.hunks[0]
    assert hunk.old_start == 1 and hunk.new_start == 1 and hunk.section == "def main():"
    kinds = [ln.kind for ln in hunk.lines]
    assert kinds == ["ctx", "del", "add", "add", "ctx", "ctx", "note"]
    added = [ln for ln in hunk.lines if ln.kind == "add"]
    assert [(ln.new, ln.old) for ln in added] == [(2, None), (3, None)]
    assert hunk.lines[1].old == 2 and hunk.lines[1].new is None
    assert hunk.lines[4].text == ""  # blank context line keeps numbering
    assert hunk.lines[5].old == 4 and hunk.lines[5].new == 5


def test_rename_detection() -> None:
    files = {f.path: f for f in parse_unified_diff(DIFF)}
    renamed = files["docs/new.md"]
    assert renamed.change_type == "renamed"
    assert renamed.old_path == "docs/old.md"
    assert renamed.similarity == 92
    assert (renamed.insertions, renamed.deletions) == (1, 1)
    assert renamed.hunks[0].old_count == 1  # "@@ -3 +3 @@" means one line


def test_added_deleted_and_binary() -> None:
    files = {f.path: f for f in parse_unified_diff(DIFF)}
    assert files["tests/test_new.py"].change_type == "added"
    assert files["tests/test_new.py"].insertions == 2
    assert files["README.md"].change_type == "deleted"
    assert files["README.md"].deletions == 2
    logo = files["logo.png"]
    assert logo.is_binary and logo.hunks == [] and logo.change_type == "added"


def test_truncated_diff_is_flagged_and_stops() -> None:
    text = DIFF.split("diff --git a/docs")[0] + TRUNCATION_MARKER + "\n"
    files = parse_unified_diff(text)
    assert len(files) == 1 and files[0].truncated


def test_line_cap_counts_but_does_not_keep_lines() -> None:
    body = "\n".join(f"+line {i}" for i in range(50))
    text = f"diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -0,0 +1,50 @@\n{body}\n"
    (f,) = parse_unified_diff(text, max_lines_per_file=10)
    assert f.truncated and f.insertions == 50 and len(f.hunks[0].lines) == 10


def test_quoted_paths_and_split_by_file() -> None:
    text = (
        'diff --git "a/dir/with space.txt" "b/dir/with space.txt"\n'
        '--- "a/dir/with space.txt"\n+++ "b/dir/with space.txt"\n@@ -1 +1 @@\n-a\n+b\n'
    )
    (f,) = parse_unified_diff(text)
    assert f.path == "dir/with space.txt"
    chunks = split_by_file(DIFF)
    assert set(chunks) >= {"src/app.py", "docs/new.md"}
    assert chunks["src/app.py"].startswith("diff --git a/src/app.py")


def test_empty_diff() -> None:
    assert parse_unified_diff("") == []


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        (".github/workflows/ci.yml", ".github/**", True),
        (".github", ".github/**", False),
        ("sub/.github/x.yml", ".github/**", False),
        ("poetry.lock", "*.lock", True),
        ("deep/nested/uv.lock", "*.lock", True),
        ("lockfile", "*.lock", False),
        ("CHANGELOG.md", "CHANGELOG.md", True),
        ("docs/CHANGELOG.md", "CHANGELOG.md", True),
        ("docs/CHANGELOG.md", "/CHANGELOG.md", False),
        ("src/secrets/key.pem", "src/secrets/", True),
        ("src/app.py", "src/*.py", True),
        ("src/pkg/app.py", "src/*.py", False),
        ("src/pkg/app.py", "src/**/*.py", True),
        ("src/app.py", "src/**/*.py", True),
        (".env", ".env", True),
        ("config/.env", ".env", True),
        (".env.local", ".env", False),
        ("a1.txt", "a?.txt", True),
        ("ab.txt", "a[0-9].txt", False),
        ("a5.txt", "a[0-9].txt", True),
        ("x", "", False),
    ],
)
def test_protected_path_matching(path: str, pattern: str, expected: bool) -> None:
    assert matches(path, pattern) is expected


def test_rename_pairing_by_content() -> None:
    guide = "# Guide\n" + "Line of text.\n" * 20
    pairs = pair_renames(
        {"docs/guide.md": guide, "gone.txt": "totally different\n"},
        {"docs/manual.md": guide + "One more line.\n", "new.txt": "unrelated content\n"},
    )
    assert [(old, new) for old, new, _ in pairs] == [("docs/guide.md", "docs/manual.md")]
    assert pairs[0][2] > 0.9
    assert similarity("a\nb\n", "a\nb\n") == 1.0
    assert pair_renames({"a.txt": "x\n"}, {"b.txt": "y\n"}) == []
    text, ins, dels = rename_diff("docs/guide.md", "docs/manual.md", "a\nb\n", "a\nc\n", 0.5)
    assert (ins, dels) == (1, 1)
    (parsed,) = parse_unified_diff(text)
    assert parsed.change_type == "renamed" and parsed.old_path == "docs/guide.md"
    assert parsed.path == "docs/manual.md" and parsed.similarity == 50


def test_rename_prefers_same_file_name_on_ties() -> None:
    body = "same body\n" * 5
    pairs = pair_renames({"old/util.py": body}, {"x/other.py": body, "new/util.py": body})
    assert pairs[0][:2] == ("old/util.py", "new/util.py")


def test_protected_helpers() -> None:
    patterns = [".github/**", "*.lock", "CHANGELOG.md"]
    assert matching_patterns("uv.lock", patterns) == ["*.lock"]
    assert is_protected("docs/new.md", patterns, old_path="CHANGELOG.md")
    assert not is_protected("src/app.py", patterns)


def test_capture_changes_bounds_untracked_diff_subprocesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ``git diff --no-index`` per untracked file stops at the cap; the list stays whole."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], capture_output=True, text=True, env=env, check=True
        ).stdout.strip()

    git("init", "--quiet", "--initial-branch=main")
    (tmp_path / "tracked.txt").write_text("one\n")
    git("add", "tracked.txt")
    git("commit", "--quiet", "-m", "init")
    base = git("rev-parse", "HEAD")
    untracked = [f"new/f{i}.txt" for i in range(8)]
    (tmp_path / "new").mkdir()
    for i, rel in enumerate(untracked):
        (tmp_path / rel).write_text(f"file {i}\n")

    no_index_calls: list[str] = []
    real_run = GuardedGit.run

    def counting_run(self: GuardedGit, *args: str, **kwargs: Any) -> Any:
        if "--no-index" in args:
            no_index_calls.append(args[-1])
        return real_run(self, *args, **kwargs)

    monkeypatch.setattr(GuardedGit, "run", counting_run)
    captured = capture_changes(tmp_path, base, max_untracked_diffed=5)

    assert len(no_index_calls) == 5
    assert {e.path for e in captured.entries} == set(untracked)
    assert sorted(captured.skipped_untracked) == sorted(set(untracked) - set(no_index_calls))
    assert len(captured.skipped_untracked) == 3
    assert captured.diff_text.count("diff --git") == 5
    # A file skipped for the count cap is not reported as over the size cap.
    assert captured.diff_omitted == {p: "count_cap" for p in captured.skipped_untracked}

    by_size = capture_changes(tmp_path, base, max_untracked_file_bytes=3)
    assert by_size.diff_omitted == {p: "size_cap" for p in untracked}
