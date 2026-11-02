"""The findings contract and the record_finding tool description.

In M3's acceptance run an agent followed the contract as written, cited lines it had seen only in a Grep
result as a file citation, and got "Never read": search output is not a file read
(FINDINGS.md "Which reads count"). Both texts an agent can see must say so.
"""

from __future__ import annotations

from pathlib import Path

from agenttape.mcp.launch import FINDINGS_PROMPT_CONTRACT
from agenttape.mcp.server import _RECORD_FINDING_DOCSTRING

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_the_launched_run_contract_says_search_output_is_not_a_file_read() -> None:
    assert "\n" not in FINDINGS_PROMPT_CONTRACT
    assert "Grep or Glob output is not a file read" in FINDINGS_PROMPT_CONTRACT
    assert "Read the file before" in FINDINGS_PROMPT_CONTRACT
    assert "event citation" in FINDINGS_PROMPT_CONTRACT


def test_the_tool_description_interactive_sessions_see_says_the_same() -> None:
    assert "Grep or Glob output is not a file read" in _RECORD_FINDING_DOCSTRING
    assert "Read the file before" in _RECORD_FINDING_DOCSTRING


def test_the_integration_guide_quotes_the_contract_verbatim() -> None:
    guide = (REPO_ROOT / "docs" / "integrations" / "claude-code.md").read_text(encoding="utf-8")
    assert FINDINGS_PROMPT_CONTRACT in guide
