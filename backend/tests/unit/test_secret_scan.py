"""The built-in secret scanner catches common credential shapes.

Samples are assembled by concatenation so this file never matches the scanner itself.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "secret_scan.py"


@pytest.fixture(scope="module")
def scanner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("secret_scan", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RANDOM = "Zq8" + "vN2kLp4Xw9Rt7Bm3Hc6Yd1"

CAUGHT = [
    "API_KEY=" + RANDOM,
    "export GITHUB_TOKEN=" + RANDOM + "aa",
    "client_secret: '" + RANDOM + "'",
    'password = "' + RANDOM + '"',
    "Authorization: Bearer " + RANDOM + "xyzXYZ",
    "//registry.npmjs.org/:_authToken=" + RANDOM,
    "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." + RANDOM,
    "npm_" + "a" * 36,
    "-----BEGIN " + "RSA PRIVATE KEY-----",
]

IGNORED = [
    "API_KEY=$API_KEY",
    "token: ${{ secrets.AGENTTAPE_WEBHOOK_TOKEN }}",
    'password = "<your password here>"',
    "Authorization: Bearer $AGENTTAPE_TOKEN",
    'const TOKEN_KEY = "agenttape.apiToken";',
    'json={"token": "my-own-long-webhook-token-123456"}',
    'secret_env={"TOOL_TOKEN": "GC_TEST_SECRET"}',
    "token = settings.effective_token_hash()",
    "auth_token=[REDACTED:sensitive_field]",
]


@pytest.mark.parametrize("line", CAUGHT)
def test_credential_shapes_are_caught(scanner: ModuleType, line: str) -> None:
    assert scanner.scan_line(line), line


@pytest.mark.parametrize("line", IGNORED)
def test_references_and_placeholders_are_not_flagged(scanner: ModuleType, line: str) -> None:
    assert scanner.scan_line(line) == [], line


def test_allow_marker_is_not_a_literal_in_the_scanner(scanner: ModuleType) -> None:
    assert scanner.ALLOW_MARKER not in SCRIPT.read_text()
