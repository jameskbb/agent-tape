"""A JSON output line is redacted string by string, so redaction never breaks the JSON.

Recorded case: Claude Code's stream-json line for a Read of source code
holding ``ATTEMPT_TOKEN_PREFIX = "atm_"``. Redacting the serialized line as flat text
replaced the backslash of the escaped quote, the line no longer parsed, and the adapter
lost the tool_result.
"""

from __future__ import annotations

import json
from pathlib import Path

from agenttape.adapters.process import redact_line
from agenttape.security.redaction import Redactor

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "adapters"
    / "claude_code_read_redactable_code.jsonl"
)


def _tool_result_lines() -> list[str]:
    return [line for line in FIXTURE.read_text().splitlines() if '"type":"user"' in line]


def test_recorded_read_line_still_parses_after_redaction() -> None:
    redactor = Redactor()
    for line in _tool_result_lines():
        original = json.loads(line)["message"]["content"][0]["content"]
        out = json.loads(redact_line(line, redactor.redact_text))
        content = out["message"]["content"][0]["content"]
        assert content.count("\n") == original.count("\n")
        assert content == redactor.redact_text(original)
    capture = json.loads(redact_line(_tool_result_lines()[0], redactor.redact_text))
    assert (
        "ATTEMPT_TOKEN_PREFIX = [REDACTED:assignment]"
        in capture["message"]["content"][0]["content"]
    )


def test_flat_redaction_of_the_recorded_line_is_what_broke() -> None:
    line = _tool_result_lines()[0]
    broken = Redactor().redact_text(line)
    try:
        json.loads(broken)
    except ValueError:
        return
    raise AssertionError("flat redaction of the recorded line was expected to break its JSON")


def test_secret_inside_a_json_line_is_redacted() -> None:
    secret = "s3cr3t-" + "value-" + "8472"
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    redactor = Redactor([secret])
    line = json.dumps({"type": "user", "content": [f"token is {secret}", {"k": f"aws {key}"}]})
    out = redact_line(line, redactor.redact_text)
    assert secret not in out and key not in out
    assert json.loads(out)["content"][0] == "token is [REDACTED:secret]"
    assert json.loads(out)["content"][1]["k"] == "aws [REDACTED:aws_access_key]"


def test_secret_the_serialization_escapes_is_still_redacted() -> None:
    # JSON escapes the quote inside the value, so the serialized line never contains it verbatim.
    secret = 'pa"ss-' + "w0rd-" + "5521"
    redactor = Redactor([secret])
    line = json.dumps({"type": "user", "content": f"password was {secret} here"})
    out = redact_line(line, redactor.redact_text)
    assert secret not in json.loads(out)["content"]
    assert "[REDACTED:secret]" in json.loads(out)["content"]


def test_line_with_nothing_to_redact_is_returned_unchanged() -> None:
    line = '{"type":"assistant","message":{"content":[{"type":"text","text":"caf\\u00e9"}]}}'
    assert redact_line(line, Redactor().redact_text) == line


def test_non_json_and_invalid_json_lines_use_flat_redaction() -> None:
    redactor = Redactor()
    for line in ("api_key=abcdef123456 plain text", '{"not json": password=hunter22'):
        assert redact_line(line, redactor.redact_text) == redactor.redact_text(line)
