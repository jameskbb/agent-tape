"""The evidence demo agent: a deterministic, credential-free "agent" process.

    python -m agenttape.demo_agent --workdir DIR --telemetry PATH_OR_DASH --phase start|resume
                                   [--variant a|b] [--step-delay-ms N]

It runs in ``DIR`` (a Git checkout of the AgentTape demo fixture) and writes JSON Lines
telemetry to ``PATH`` (appended, flushed per line) or stdout (``-``). The same process runs
locally (the Demo Agent's ``full_evidence`` scenario) and inside a Herdr pane, so every
runtime sees identical telemetry. Exit codes: 0 done, 3 needs approval (the last record
before ``exit`` is ``approval_request``), 1 failed. The final line is always
``{"type": "exit", "code": N}``.

Phase ``start`` performs the File Evidence and Context Evidence story (every file action is
real; the "model" text, knowledge store, and MCP tool are fixtures):

* reads three files: ``src/greeter.py`` and ``CHANGELOG.md`` through Claude-Code-shaped
  ``Read`` tool calls whose results carry the content (the ``cat -n`` layout Claude Code's
  ``Read`` tool returns), and ``README.md`` through a bare read report with no content;
  searches a compact repository index first;
* runs a real, read-only ``git grep`` over ``src/greeter.py`` for text the run is about to
  change (recorded under the id ``SEARCH_REF`` so a citation can reference it; AgentTape's
  automatic re-run replays this exact argv at the run's starting commit);
* modifies ``src/greeter.py`` and leaves it changed;
* modifies ``README.md`` and restores its original bytes;
* creates ``src/helpers_draft.py`` and renames it to ``src/formatting.py``;
* creates and deletes ``scratch_notes.txt``;
* runs a real formatter subprocess over four files (one net change), a real "dependency
  install" writing 300 files under ``node_modules/``, and a real test command (recorded
  under the id ``CHECKS_REF`` so a citation can reference it) that byte-compiles ``src`` and
  writes ``reports/junit.xml``;
* emits context records (available, retrieved, stale, unavailable, gap, cited), finding F1
  (unchanged: three legacy source links), then findings F2-F8, each citing evidence the run
  actually recorded so its citations exercise every verdict: F2 stale then quote matches (a
  file citation and an event citation of the real search), F3 quote matches (a file citation
  and an event citation), F4 quote not found, F5 never read, F6 not checkable, F7 read not
  observed (a documented stand-in: the only evidence is a command that processes the file,
  the closest honest analogue of an incomplete evidence window a scripted run can produce), F1 and F8 declared only (no citation). See ``DESIGNED_VERDICTS`` and
  ``DECLARED_ONLY_FINDINGS`` below. The agent never emits a verdict itself, only findings and
  citations; then a simulated user correction;
* asks for approval to write the protected ``.github/workflows/ci.yml`` and exits 3.

Phase ``resume`` (a fresh process is fine) writes that file and exits 0. Variant ``b``
uses a different knowledge source (fresh, not stale) and resolves the context gap.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

from agenttape.adapters.builtin._demo_repo import TEST_FILE
from agenttape.findings.normalize import to_cat_n

EXIT_DONE = 0
EXIT_FAILED = 1
EXIT_NEEDS_APPROVAL = 3

PROTECTED_TARGET = ".github/workflows/ci.yml"
CI_WORKFLOW = """name: ci
on: [push]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python -m compileall -q src
      - run: python -m demo_formatter --check src
"""
HELPER = '''"""Formatting helpers for the greeting service."""


def punctuate(text: str, formal: bool = False) -> str:
    """End a greeting with the house punctuation."""
    return text + ("." if formal else "!")
'''
GITIGNORE = "node_modules/\nreports/\n__pycache__/\n"
NOISY_FILES = 300

#: The id given to the tests command's telemetry record so a finding's event citation
#: (``{"event_ref": CHECKS_REF, ...}``) can reference it.
CHECKS_REF = "cmd_demo_tests"
#: The id given to the real, read-only ``git grep`` command's telemetry record so F2's
#: second citation (an event citation) can reference it; AgentTape's automatic re-run
#: replays this exact argv at the run's starting commit.
SEARCH_REF = "cmd_demo_search"
#: The exact substring ``search()`` looks for in ``src/greeter.py`` - also the literal the
#: later ``write()`` call below replaces, so the search's own recorded output names text
#: the run genuinely goes on to change.
_GREETER_RETURN_LINE = 'return "Hello, " + name + "!"'
#: A claim quoted for F4: designed to NOT appear in CHANGELOG.md, so the citation's quote
#: cannot be found in the recorded content (the "quote not found" verdict).
_F4_QUOTE = "- Strip whitespace from names in greet()."

#: Which citation position each finding's evidence was designed to exercise, one of the six
#: citation verdict strings. Read by the demo seed and by
#: ``test_demo_agent_telemetry_backs_every_designed_verdict``; never emitted in the recorded
#: telemetry itself: the agent only declares findings and citations, never verdicts. F7's
#: "read_not_observed" is a documented stand-in: the recorded evidence only shows a command
#: processing the file, the closest honest analogue of an incomplete evidence window the
#: scripted demo adapter can produce (a genuine incomplete window needs an observed
#: session).
DESIGNED_VERDICTS: dict[tuple[str, int], str] = {
    ("F2", 0): "stale",
    ("F2", 1): "quote_matches",
    ("F3", 0): "quote_matches",
    ("F3", 1): "quote_matches",
    ("F4", 0): "quote_not_found",
    ("F5", 0): "never_read",
    ("F6", 0): "not_checkable",
    ("F7", 0): "read_not_observed",
}
#: Findings with no citation-shaped link: the agent's claim or decision stands undeclared
#: (F1 keeps its three legacy source links, which are not citation-shaped).
DECLARED_ONLY_FINDINGS = ("F1", "F8")

FORMATTER = r"""
import pathlib, sys
changed = 0
for name in sys.argv[1:]:
    p = pathlib.Path(name)
    text = p.read_text(encoding="utf-8")
    lines = [line.rstrip() for line in text.splitlines()]
    new = ("\n".join(lines) + "\n") if lines else ""
    changed += new != text
    p.write_text(new, encoding="utf-8")  # always rewrite: formatters touch every file
print(f"formatted {len(sys.argv) - 1} files, {changed} changed")
"""
INSTALL = r"""
import pathlib, sys
root = pathlib.Path("node_modules/left-pad-demo")
for i in range(int(sys.argv[1])):
    d = root / f"lib{i // 50}"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"part{i}.js").write_text(f"module.exports = {i};\n", encoding="utf-8")
(root / "package.json").write_text('{"name": "left-pad-demo", "version": "1.0.0"}\n')
print(f"installed left-pad-demo ({sys.argv[1]} files)")
"""
TESTS = r"""
import compileall, pathlib, sys, time
ok = compileall.compile_dir("src", quiet=1)
pathlib.Path("reports").mkdir(exist_ok=True)
time.sleep(0.5)  # a watcher only sees files in a new directory once it watches it
pathlib.Path("reports/junit.xml").write_text(
    '<testsuite name="demo" tests="2" failures="0"><testcase name="compile"/>'
    '<testcase name="greet"/></testsuite>\n', encoding="utf-8")
print("2 checks passed" if ok else "compile failed")
sys.exit(0 if ok else 1)
"""

#: Fixture knowledge store (a snapshot source) and a fake MCP warehouse tool.
KNOWLEDGE: dict[str, dict[str, Any]] = {
    "a": {
        "id": "kb:greeting-style",
        "name": "Greeting style guide (snapshot)",
        "version": "2026.03",
        "synced_days_ago": 120,
        "text": "Informal greetings end with an exclamation mark.",
    },
    "b": {
        "id": "kb:greeting-style-v2",
        "name": "Greeting style guide v2 (snapshot)",
        "version": "2026.09",
        "synced_days_ago": 1,
        "text": "Informal greetings end with '!'; formal greetings end with '.'.",
    },
}
MCP_ROWS = [["en", 42], ["de", 7], ["fr", 3]]


def _now() -> datetime:
    return datetime.now(UTC)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _find_lines(text: str, marker: str, count: int) -> tuple[int, int, str]:
    """The 1-based (start, end) line numbers and joined text of the first ``count`` lines
    beginning at the first line containing ``marker``.

    Raises ``RuntimeError`` when ``marker`` is missing so fixture drift fails the run loudly
    (exit 1) instead of emitting a citation whose quote does not actually come from the text.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if marker in line:
            start = i + 1
            end = start + count - 1
            return start, end, "\n".join(lines[i : i + count])
    raise RuntimeError(f"expected a line containing {marker!r} in the fixture text")


def _grep_code_text(grep_line: str) -> str:
    """The code text after the ``path:line:`` prefix of one ``git grep -n`` output line."""
    _path, _, rest = grep_line.partition(":")
    _line_no, _, code = rest.partition(":")
    return code


class Agent:
    def __init__(self, workdir: Path, out: IO[str], *, variant: str, delay: float) -> None:
        self.root = workdir
        self.out = out
        self.variant = variant
        self.delay = delay
        self.read_tool_seq = 0

    # -- telemetry ---------------------------------------------------------------------

    def emit(self, record: dict[str, Any]) -> None:
        self.out.write(json.dumps(record, sort_keys=True) + "\n")
        self.out.flush()

    def pause(self, minimum: float = 0.0) -> None:
        time.sleep(max(self.delay, minimum))

    def say(self, text: str) -> None:
        self.emit({"type": "assistant", "text": text})
        self.pause()

    def context(
        self,
        stage: str,
        source: dict[str, Any],
        *,
        query: str | None = None,
        tool: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> None:
        self.emit(
            {
                "type": "context",
                "stage": stage,
                "source": source,
                "query": query,
                "tool": tool,
                "result": result,
                "reason": reason,
            }
        )

    # -- files -------------------------------------------------------------------------

    def path(self, rel: str) -> Path:
        return self.root / rel

    def read(self, rel: str) -> str:
        data = self.path(rel).read_bytes()
        self.emit({"type": "file_read", "path": rel, "bytes": len(data)})
        return data.decode("utf-8")

    def read_tool(self, rel: str) -> str:
        """Read ``rel`` through a Claude-Code-shaped ``Read`` tool call, content and all.

        Reported as a ``tool_use`` (``id``, ``name`` "Read", ``input`` with the *relative*
        path, so no absolute temp path lands in events) followed by a ``tool_result`` whose
        output is the ``cat -n`` layout Claude Code's ``Read`` tool returns: each line as a
        6-wide right-aligned 1-based number, a tab, then the line.
        """
        self.read_tool_seq += 1
        tool_use_id = f"toolu_demo_read_{self.read_tool_seq:02d}"
        text = self.path(rel).read_text(encoding="utf-8")
        self.emit(
            {"type": "tool_use", "id": tool_use_id, "name": "Read", "input": {"file_path": rel}}
        )
        output = to_cat_n(text.splitlines())
        self.emit(
            {"type": "tool_result", "tool_use_id": tool_use_id, "output": output, "is_error": False}
        )
        return text

    def write(self, rel: str, text: str, operation: str) -> None:
        target = self.path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        self.emit(
            {
                "type": "file_write",
                "path": rel,
                "bytes": len(text.encode("utf-8")),
                "operation": operation,
            }
        )

    def command(
        self, script: str, args: list[str], *, category: str, label: str, ref: str | None = None
    ) -> None:
        """Run ``python -c <script> ARGS`` for real and report it with its start and finish.

        The reported argv is the one that ran, with the interpreter reduced to its name
        and the inline script body elided; ``label`` names what the script does. When ``ref``
        is given, the reported record carries ``"id": ref`` so an event citation can reference
        this exact record (``adapters.builtin.demo`` passes it through as ``tool_use_id``).
        """
        argv = [sys.executable, "-c", script, *args]
        reported = [Path(sys.executable).name, "-c", f"<{label} script>", *args]
        started = _now()
        proc = subprocess.run(
            argv,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        finished = _now()
        record: dict[str, Any] = {
            "type": "command",
            "argv": reported,
            "label": label,
            "cwd": ".",
            "exit_code": proc.returncode,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "category": category,
            "output": (proc.stdout + proc.stderr).strip()[:500],
        }
        if ref:
            record["id"] = ref
        self.emit(record)
        if proc.returncode != 0:
            raise RuntimeError(f"{label} exited with {proc.returncode}")

    def search(self, literal: str, rel: str) -> str:
        """Run a real, read-only ``git grep`` for ``literal`` in ``rel`` and report it
        exactly as it ran (nothing elided), so AgentTape's automatic re-run can
        replay this same argv.

        Raises ``RuntimeError`` when the exit code is not 0: the literal must actually be
        found (a fixture-drift guard, like :func:`_find_lines`), so a citation quoting the
        output is never a designed string that was not actually searched for. Returns the
        recorded ``output`` (what the finding's quote is drawn from).
        """
        argv = ["git", "grep", "-n", "-F", literal, "--", rel]
        started = _now()
        proc = subprocess.run(
            argv,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        finished = _now()
        output = (proc.stdout + proc.stderr).strip()[:500]
        self.emit(
            {
                "type": "command",
                "argv": argv,
                "label": "demo search",
                "cwd": ".",
                "exit_code": proc.returncode,
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "category": "search",
                "output": output,
                "id": SEARCH_REF,
            }
        )
        if proc.returncode != 0:
            raise RuntimeError(f"demo search exited with {proc.returncode}")
        return output

    # -- sources -----------------------------------------------------------------------

    def repo_index(self) -> dict[str, Any]:
        return {
            "id": "repo:index",
            "type": "repository_index",
            "name": "Compact repository index",
            "provider": "demo-agent",
            "locator": "repo-index://demo-fixture",
            "authority": "generated",
            "version": None,
            "synced_at": _now().isoformat(),
            "live": True,
        }

    def repo_file(self, rel: str) -> dict[str, Any]:
        return {
            "id": f"repo:{rel}",
            "type": "repository_file",
            "name": rel,
            "provider": "git",
            "locator": rel,
            "authority": "authoritative",
            "version": None,
            "synced_at": None,
            "live": True,
        }

    def knowledge(self) -> dict[str, Any]:
        kb = KNOWLEDGE[self.variant]
        return {
            "id": kb["id"],
            "type": "knowledge_store",
            "name": kb["name"],
            "provider": "demo-knowledge-store",
            "locator": f"kb://demo/{kb['id'].split(':', 1)[1]}",
            "authority": "snapshot",
            "version": kb["version"],
            "synced_at": (_now() - timedelta(days=int(kb["synced_days_ago"]))).isoformat(),
            "live": False,
        }

    def warehouse(self) -> dict[str, Any]:
        return {
            "id": "mcp:demo-warehouse/run_query",
            "type": "mcp_tool",
            "name": "Demo warehouse run_query",
            "provider": "mcp:demo-warehouse",
            "locator": "mcp://demo-warehouse/run_query",
            "authority": "authoritative",
            "version": "1",
            "synced_at": None,
            "live": True,
        }

    def wiki(self) -> dict[str, Any]:
        return {
            "id": "wiki:style",
            "type": "wiki",
            "name": "Team wiki: localisation style",
            "provider": "demo-wiki",
            "locator": "wiki://demo/localisation-style",
            "authority": "reference",
            "version": None,
            "synced_at": None,
            "live": True,
        }

    def owners(self) -> dict[str, Any]:
        return {
            "id": "kb:localisation-owners",
            "type": "knowledge_store",
            "name": "Localisation owners list (snapshot)",
            "provider": "demo-knowledge-store",
            "locator": "kb://demo/localisation-owners",
            "authority": "snapshot",
            "version": "2026.09",
            "synced_at": (_now() - timedelta(days=2)).isoformat(),
            "live": False,
        }

    # -- phases ------------------------------------------------------------------------

    def start(self) -> int:
        self.say("Starting: refactor greet() and record full file and context evidence.")
        kb = self.knowledge()
        for source in (self.repo_index(), kb, self.warehouse(), self.wiki()):
            self.context("available", source, reason="Configured for this project")
        for name in ("AGENTS.md", "CLAUDE.md"):
            if self.path(name).is_file():
                self.read(name)
                self.context("retrieved", self.repo_file(name), reason="Project instruction file")

        # Compact index first, then targeted retrieval.
        tracked = sorted(
            str(p.relative_to(self.root)).replace(os.sep, "/")
            for p in self.root.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(self.root).parts
        )
        hits = [
            rel
            for rel in tracked
            if rel.endswith((".py", ".md"))
            and "greet" in self.path(rel).read_text(encoding="utf-8", errors="replace").lower()
        ]
        self.emit({"type": "file_search", "query": "greet", "paths": hits})
        self.context(
            "retrieved",
            self.repo_index(),
            query="greet",
            result={
                "hash": _sha("\n".join(hits)),
                "rows": len(hits),
                "schema": ["path"],
                "truncated": False,
                "media_type": "text/plain",
            },
        )
        self.pause()
        greeter = self.read_tool("src/greeter.py")
        self.context(
            "retrieved",
            self.repo_file("src/greeter.py"),
            result={
                "hash": _sha(greeter),
                "rows": None,
                "schema": None,
                "truncated": False,
                "media_type": "text/x-python",
            },
        )
        # A real, read-only search before anything writes src/greeter.py: F2's second
        # citation (an event citation) quotes this command's own recorded output.
        search_output = self.search(_GREETER_RETURN_LINE, "src/greeter.py")
        # Left on the bare `read` on purpose: recorded without content, which is what makes
        # F6's citation against it "not checkable" rather than "verified"/"stale".
        readme = self.read("README.md")
        self.context("retrieved", self.repo_file("README.md"))
        changelog = self.read_tool("CHANGELOG.md")
        self.pause()

        # Knowledge store (snapshot) and live verification through a fake MCP tool.
        guide = KNOWLEDGE[self.variant]
        self.context(
            "retrieved",
            kb,
            query="greeting punctuation policy",
            result={
                "hash": _sha(str(guide["text"])),
                "rows": 1,
                "schema": ["title", "text"],
                "truncated": False,
                "media_type": "application/json",
            },
        )
        if int(guide["synced_days_ago"]) > 30:
            self.context(
                "stale",
                kb,
                reason=f"Snapshot last synced {guide['synced_days_ago']} days ago",
            )
        sql = "SELECT locale, COUNT(*) AS greetings FROM greetings GROUP BY locale"
        self.context(
            "retrieved",
            self.warehouse(),
            query=sql,
            tool={
                "server": "demo-warehouse",
                "name": "run_query",
                "args": {"connection": "demo-dw", "sql": sql, "auth": "[redacted]"},
            },
            result={
                "hash": _sha(json.dumps(MCP_ROWS)),
                "rows": len(MCP_ROWS),
                "schema": ["locale", "greetings"],
                "truncated": False,
                "media_type": "application/json",
            },
        )
        self.context(
            "unavailable",
            self.wiki(),
            reason="The wiki connector has no credentials in this demo",
        )
        if self.variant == "b":
            self.context(
                "retrieved",
                self.owners(),
                query="owner of localized greetings",
                result={
                    "hash": _sha("de: Team Berlin; fr: Team Lyon"),
                    "rows": 2,
                    "schema": ["locale", "owner"],
                    "truncated": False,
                    "media_type": "application/json",
                },
            )
        else:
            self.context(
                "gap",
                self.wiki(),
                reason="No documented owner for localized greetings",
            )
        self.pause()

        # File work.
        self.write(".gitignore", GITIGNORE, "create")
        edited = greeter.replace(
            'return "Hello, " + name + "!"',
            'cleaned = name.strip()  \n    return "Hello, " + cleaned + "!"',
        )
        if edited == greeter:
            edited = greeter.rstrip("\n") + "\n# refactored by the evidence demo agent  \n"
        self.write("src/greeter.py", edited, "modify")
        self.pause()
        self.write("README.md", readme + "\nDraft note: greeting refactor in progress.\n", "modify")
        self.pause(0.7)  # let the filesystem watcher see the intermediate content
        self.write("README.md", readme, "modify")
        self.say("Restored README.md; the draft note belongs in the pull request instead.")
        self.write("src/helpers_draft.py", HELPER, "create")
        self.pause(0.5)
        self.path("src/helpers_draft.py").rename(self.path("src/formatting.py"))
        self.emit(
            {"type": "file_rename", "path": "src/formatting.py", "old_path": "src/helpers_draft.py"}
        )
        self.write("scratch_notes.txt", "Temporary notes: check the formal locale.\n", "create")
        self.pause(0.5)
        self.path("scratch_notes.txt").unlink()
        self.emit(
            {"type": "file_write", "path": "scratch_notes.txt", "bytes": 0, "operation": "delete"}
        )
        self.pause()

        files = ["src/greeter.py", "src/__init__.py", "src/formatting.py", ".gitignore"]
        self.command(FORMATTER, files, category="formatter", label="demo formatter")
        self.pause()
        self.command(INSTALL, [str(NOISY_FILES)], category="install", label="demo install")
        self.pause()
        self.command(TESTS, [], category="test", label="demo tests", ref=CHECKS_REF)
        self.pause()

        # Findings, citations, and a simulated correction.
        self.context("cited", self.repo_file("src/greeter.py"), reason="Finding F1")
        self.context("cited", kb, reason="Finding F1")
        self.emit(
            {
                "type": "finding",
                "id": "F1",
                "kind": "recommendation",
                "text": "Strip whitespace before greeting and keep informal greetings ending "
                "with '!'; move punctuation into src/formatting.py.",
                "links": [
                    {"source_id": "repo:src/greeter.py", "relation": "supports"},
                    {"source_id": kb["id"], "relation": "supports"},
                    {"source_id": "wiki:style", "relation": "missing"},
                ],
            }
        )

        # F2-F8: citations designed so the run's own recorded evidence backs each of the six
        # citation verdicts (see DESIGNED_VERDICTS); F1 above and F8 below are declared only.
        stale_start, stale_end, stale_quote = _find_lines(greeter, "def greet(", 2)
        self.emit(
            {
                "type": "finding",
                "id": "F2",
                "kind": "claim",
                "text": "greet() concatenates the name as given, so surrounding whitespace "
                "ends up in the greeting.",
                "links": [
                    {
                        "path": "src/greeter.py",
                        "line_start": stale_start,
                        "line_end": stale_end,
                        "quote": stale_quote,
                    },
                    {
                        "event_ref": SEARCH_REF,
                        "quote": _grep_code_text(search_output.splitlines()[0]),
                    },
                ],
            }
        )

        unreleased_start, unreleased_end, unreleased_quote = _find_lines(
            changelog, "## Unreleased", 3
        )
        self.emit(
            {
                "type": "finding",
                "id": "F3",
                "kind": "claim",
                "text": "The change is ready to record: the changelog has an Unreleased "
                "section and the demo checks pass.",
                "links": [
                    {
                        "path": "CHANGELOG.md",
                        "line_start": unreleased_start,
                        "line_end": unreleased_end,
                        "quote": unreleased_quote,
                    },
                    {"event_ref": CHECKS_REF, "quote": "2 checks passed"},
                ],
            }
        )

        not_found_start, not_found_end, _existing_quote = _find_lines(
            changelog, "- Initial greeting service.", 2
        )
        self.emit(
            {
                "type": "finding",
                "id": "F4",
                "kind": "claim",
                "text": "Whitespace stripping is already listed in the changelog.",
                "links": [
                    {
                        "path": "CHANGELOG.md",
                        "line_start": not_found_start,
                        "line_end": not_found_end,
                        # Designed NOT to appear in the changelog (see _F4_QUOTE): the
                        # citation's quote does not match the recorded content.
                        "quote": _F4_QUOTE,
                    }
                ],
            }
        )

        # tests/test_greeter.py does not exist in this fixture and the run never reads it.
        never_read_quote = "\n".join(TEST_FILE.splitlines()[3:5])
        self.emit(
            {
                "type": "finding",
                "id": "F5",
                "kind": "claim",
                "text": "Existing unit tests already cover whitespace in names.",
                "links": [
                    {
                        "path": "tests/test_greeter.py",
                        "line_start": 4,
                        "line_end": 5,
                        "quote": never_read_quote,
                    }
                ],
            }
        )

        # README.md was read with the bare `read` above: no content was recorded for it, so
        # this citation cannot be checked even though the quote is genuinely in the file.
        not_checkable_start, not_checkable_end, not_checkable_quote = _find_lines(
            readme, "Demo Agent clones", 2
        )
        self.emit(
            {
                "type": "finding",
                "id": "F6",
                "kind": "claim",
                "text": "The README says the Demo Agent clones the repository for every run.",
                "links": [
                    {
                        "path": "README.md",
                        "line_start": not_checkable_start,
                        "line_end": not_checkable_end,
                        "quote": not_checkable_quote,
                    }
                ],
            }
        )

        # src/formatting.py is only named in the formatter command's argv above: no Read, no
        # file_read record. The closest honest analogue of an incomplete evidence window.
        read_not_observed_start, read_not_observed_end, read_not_observed_quote = _find_lines(
            HELPER, "return text +", 1
        )
        self.emit(
            {
                "type": "finding",
                "id": "F7",
                "kind": "claim",
                "text": "punctuate() ends informal greetings with '!'.",
                "links": [
                    {
                        "path": "src/formatting.py",
                        "line_start": read_not_observed_start,
                        "line_end": read_not_observed_end,
                        "quote": read_not_observed_quote,
                    }
                ],
            }
        )

        self.emit(
            {
                "type": "finding",
                "id": "F8",
                "kind": "decision",
                "text": "Keep the change to greet() and one helper module; leave the public "
                "API alone.",
                "links": [],
            }
        )

        self.emit(
            {
                "type": "correction",
                "text": "Formal greetings must end with a period, not an exclamation mark.",
                "category": None,
            }
        )
        self.emit({"type": "usage", "input_tokens": 1830, "output_tokens": 412})
        self.emit(
            {
                "type": "approval_request",
                "title": f"Update {PROTECTED_TARGET} to run the new formatter",
                "rationale": "CI workflows are protected paths. The agent wants to add a "
                "formatter check; approve to let it write the file.",
                "paths": [PROTECTED_TARGET],
            }
        )
        return EXIT_NEEDS_APPROVAL

    def resume(self) -> int:
        self.say(f"Approval granted. Writing {PROTECTED_TARGET}.")
        existed = self.path(PROTECTED_TARGET).exists()
        self.write(PROTECTED_TARGET, CI_WORKFLOW, "modify" if existed else "create")
        self.pause()
        self.emit({"type": "usage", "input_tokens": 240, "output_tokens": 96})
        self.say("Done: greet() refactored, formatter wired into CI, evidence recorded.")
        return EXIT_DONE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agenttape.demo_agent", description=__doc__)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--telemetry", required=True, help="JSON Lines file, or - for stdout")
    parser.add_argument("--phase", choices=("start", "resume"), default="start")
    parser.add_argument("--variant", choices=("a", "b"), default="a")
    parser.add_argument("--step-delay-ms", type=int, default=150)
    args = parser.parse_args(argv)
    workdir = args.workdir.resolve()
    if args.telemetry == "-":
        out: IO[str] = sys.stdout
        close = False
    else:
        out = Path(args.telemetry).open("a", encoding="utf-8")  # noqa: SIM115
        close = True
    agent = Agent(workdir, out, variant=args.variant, delay=max(0, args.step_delay_ms) / 1000.0)
    code = EXIT_FAILED
    try:
        if not (workdir / "src" / "greeter.py").is_file():
            agent.emit(
                {"type": "assistant", "text": "The workdir is not the AgentTape demo fixture"}
            )
        else:
            code = agent.start() if args.phase == "start" else agent.resume()
    except Exception as exc:
        agent.emit({"type": "assistant", "text": f"Failed: {type(exc).__name__}: {exc}"})
        code = EXIT_FAILED
    finally:
        agent.emit({"type": "exit", "code": code})
        if close:
            out.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
