"""Safe JSON path syntax for webhook input mapping, and the token-bucket limiter."""

from __future__ import annotations

import pytest

from agenttape.application.services.triggers import is_json_content_type
from agenttape.security.ratelimit import RateLimiter
from agenttape.triggers.jsonpath import MISSING, JsonPathError, extract, parse_path

PAYLOAD = {
    "action": "deploy",
    "client_payload": {"branch": "main", "nested": {"n": 3}, "flags": [True, False]},
    "commits": [{"id": "abc"}, {"id": "def"}],
    "weird key": {"x.y": 1},
    "none": None,
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("$", PAYLOAD),
        ("$.action", "deploy"),
        ("$.client_payload.branch", "main"),
        ("$.client_payload.nested.n", 3),
        ("$.client_payload.flags[1]", False),
        ("$.commits[1].id", "def"),
        ('$["weird key"]["x.y"]', 1),
        ("$['weird key']['x.y']", 1),
        ("$.none", None),
    ],
)
def test_extract(path: str, expected: object) -> None:
    assert extract(PAYLOAD, path) == expected


@pytest.mark.parametrize(
    "path",
    ["$.missing", "$.commits[5].id", "$.action.length", "$.client_payload[0]", "$.commits.id"],
)
def test_missing_values(path: str) -> None:
    assert extract(PAYLOAD, path) is MISSING


@pytest.mark.parametrize(
    "path",
    [
        "",
        "client_payload.branch",
        "$..branch",
        "$.*",
        "$.commits[*]",
        "$.commits[-1]",
        "$.commits[0:2]",
        "$[?(@.id)]",
        "$.a b",
        "$['unterminated]",
        "$['']",
        "$." + "a." * 40 + "b",
        "$.x" * 100,
    ],
)
def test_rejects_unsafe_or_invalid_syntax(path: str) -> None:
    with pytest.raises(JsonPathError):
        parse_path(path)


def test_json_content_types() -> None:
    assert is_json_content_type("application/json")
    assert is_json_content_type("application/json; charset=utf-8")
    assert is_json_content_type("application/vnd.github+json")
    assert not is_json_content_type("text/plain")
    assert not is_json_content_type("application/x-www-form-urlencoded")
    assert not is_json_content_type("")


def test_token_bucket_refills() -> None:
    now = [0.0]
    limiter = RateLimiter(rate=3, per_seconds=60, clock=lambda: now[0])
    assert [limiter.allow("t") for _ in range(4)] == [True, True, True, False]
    assert limiter.retry_after("t") == pytest.approx(20.0)
    now[0] += 20
    assert limiter.allow("t") is True
    assert limiter.allow("t") is False
    assert limiter.allow("other") is True
