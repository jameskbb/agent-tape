"""Redaction tests. Credential-shaped strings are assembled at runtime so the repository's
secret scanner never sees a literal credential in source."""

from __future__ import annotations

import pytest

from agenttape.security.redaction import RedactionReport, Redactor, redact

AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
ANTHROPIC = "sk-ant-" + "api03-" + "x" * 40
OPENAI = "sk-" + "proj-" + "Q" * 40
PEM = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA " + "PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        (f"key={AWS} in logs", "aws_access_key"),
        (f"token {GH}", "github_token"),
        (f"using {ANTHROPIC}", "anthropic_key"),
        (f"OPENAI {OPENAI}", "openai_key"),
        (f"header Authorization: Bearer {'z' * 30}", "bearer_token"),
        (PEM, "private_key"),
        ("https://user:hunter2hunter2@example.com/repo.git", "url_credentials"),
    ],
)
def test_patterns(raw: str, kind: str) -> None:
    report = RedactionReport()
    out = Redactor().redact_text(raw, report)
    assert f"[REDACTED:{kind}]" in out
    assert report.count >= 1


def test_raw_secret_bodies_never_survive() -> None:
    text = f"{AWS} {GH} {ANTHROPIC} {OPENAI}"
    out = redact(text)
    for secret in (AWS, GH, ANTHROPIC, OPENAI):
        assert secret not in out


def test_password_assignment_keeps_key() -> None:
    assert redact("password=hunter2 user=bob") == "password=[REDACTED:assignment] user=bob"
    assert redact('DB_PASSWORD: "s3cr3t value"') == "DB_PASSWORD: [REDACTED:assignment]"


def test_benign_assignments_are_kept() -> None:
    assert redact("max_tokens=100") == "max_tokens=100"
    assert redact("password=") == "password="
    assert redact("secret=true") == "secret=true"


def test_a_prompt_templates_own_secret_reference_syntax_survives_redaction() -> None:
    """``{{ secret:NAME }}`` (docs/SECURITY.md T4) is a symbolic reference, not a pasted
    credential; the assignment pattern must never mangle the name a later RESOLVE-mode
    render needs to find it again (issue: an inline prompt's stored ``body`` was corrupted
    to ``{{ secret:[REDACTED:assignment] }}``, silently breaking secret resolution)."""
    assert redact("Handle {{ secret:REVIEW_CANARY }}.") == "Handle {{ secret:REVIEW_CANARY }}."
    assert redact("Use {{secret:TOKEN}} now") == "Use {{secret:TOKEN}} now"
    # A genuine pasted assignment right next to (but outside) a template block still redacts.
    assert redact("token=abc123xyz {{ other }}") == "token=[REDACTED:assignment] {{ other }}"
    # Whitespace after the colon is still the template syntax (domain/prompts.py _TOKEN).
    assert redact("Use {{ secret: GITHUB_TOKEN }} now") == "Use {{ secret: GITHUB_TOKEN }} now"
    assert redact("{{secret:A_KEY}}{{secret:B_KEY}}") == "{{secret:A_KEY}}{{secret:B_KEY}}"
    # A value that runs past the reference's closing braces is not a reference.
    assert "hunter2tail" not in redact("{{secret:A_KEY}}hunter2tail")


@pytest.mark.parametrize(
    ("raw", "leaked"),
    [
        ("Leaked: {{ password: hunter2isastrongpassword }}", "hunter2isastrongpassword"),
        ("Leaked: {{password:hunter2isastrongpassword}}", "hunter2isastrongpassword"),
        ("Leaked: {{ api_key: qqqqqqqqqqqqqqqqqqqqqq }}", "qqqqqqqqqqqqqqqqqqqqqq"),
        ("Leaked: {{ passwd: zzzzzzzzzzzzzz }}", "zzzzzzzzzzzzzz"),
        (f"Leaked: {{{{ token: {GH} }}}}", GH),
        (f"Leaked: {{{{ api_key: {OPENAI} }}}}", OPENAI),
        ("Leaked: {{ secret: hunter2 value with spaces }}", "hunter2"),
        ("Leaked: {{ secret: " + '"hunter2-quoted!" }}', "hunter2-quoted!"),
        ("Leaked: {{ secret:hunter2-with-dashes }}", "hunter2-with-dashes"),
        ("Leaked: {{ secret:9startswithdigit }}", "9startswithdigit"),
        ("Leaked: {{ secret:" + "A" * 129 + " }}", "A" * 129),
    ],
)
def test_brace_wrapping_does_not_exempt_a_pasted_credential(raw: str, leaked: str) -> None:
    """Only AgentTape's own ``{{ secret:NAME }}`` reference (NAME matching the secret
    reference name rule) is exempt; any other keyword, or a ``secret:`` value that is not a
    reference name, is a pasted credential and redacts exactly as it would unwrapped."""
    out = redact(raw)
    assert leaked not in out
    assert "[REDACTED:" in out


def test_a_known_secret_value_inside_template_braces_is_still_redacted() -> None:
    """Exact-value substitution runs regardless of the template exemption: a registered
    secret value that happens to be identifier-shaped cannot hide inside ``{{ secret:... }}``."""
    r = Redactor(["HUNTER2_REAL_VALUE"])
    assert r.redact_text("{{ secret:HUNTER2_REAL_VALUE }}") == "{{ secret:[REDACTED:secret] }}"
    assert r.redact_text("{{ HUNTER2_REAL_VALUE }}") == "{{ [REDACTED:secret] }}"
    assert r.redact_text("{{ password: HUNTER2_REAL_VALUE }}") == (
        "{{ password: [REDACTED:secret] }}"
    )


def test_configured_values_are_redacted() -> None:
    r = Redactor(["my-internal-value-123"])
    assert r.redact_text("x my-internal-value-123 y") == "x [REDACTED:secret] y"
    r.add_secrets(["abc"])  # too short to redact safely
    assert r.redact_text("abc") == "abc"


def test_copy_is_independent() -> None:
    base = Redactor(["value-one"])
    clone = base.copy()
    clone.add_secrets(["value-two"])
    assert base.redact_text("value-two") == "value-two"
    assert clone.redact_text("value-one value-two") == "[REDACTED:secret] [REDACTED:secret]"


def test_agenttape_secret_markers_are_preserved() -> None:
    assert redact("use [secret:API_KEY] now") == "use [secret:API_KEY] now"


def test_nested_values_and_sensitive_keys() -> None:
    report = RedactionReport()
    out = Redactor().redact_value(
        {
            "headers": {"Authorization": "whatever", "X-Other": "fine"},
            "items": [f"a {GH}", 3, None],
            "api_key": "plain",
            "secret_ref": "GITHUB_TOKEN",
            "input_tokens": 12,
        },
        report,
    )
    assert out["headers"]["Authorization"] == "[REDACTED:sensitive_field]"
    assert out["headers"]["X-Other"] == "fine"
    assert out["items"][0] == "a [REDACTED:github_token]" and out["items"][1:] == [3, None]
    assert out["api_key"] == "[REDACTED:sensitive_field]"
    assert out["secret_ref"] == "GITHUB_TOKEN"
    assert out["input_tokens"] == 12
    assert report.as_metadata()["redacted"] is True


def test_idempotent() -> None:
    once = redact(f"token {GH} password=abc123")
    assert redact(once) == once


def test_labels_and_status_words_under_sensitive_looking_keys_survive() -> None:
    out = Redactor().redact_value(
        {"Token budget": "skipped", "api_key": "pass", "token": "abc-123-xyz"}
    )
    assert out == {
        "Token budget": "skipped",
        "api_key": "pass",
        "token": "[REDACTED:sensitive_field]",
    }


BEGIN_KEY = "-----BEGIN " + "PRIVATE KEY-----"
END_KEY = "-----END " + "PRIVATE KEY-----"


def test_private_key_redaction_is_linear_when_a_begin_has_no_end() -> None:
    """The lazy ``BEGIN [\\s\\S]*? END`` pattern rescanned the rest of the
    text from every BEGIN with no END after it, so 256 KiB of BEGIN lines took seconds
    (about 8 s; 560 KiB took 36 s), all on the worker's event loop. A linear scan takes
    milliseconds; the bound here is generous so only the quadratic scan can fail it."""
    import time

    text = (BEGIN_KEY + "\n") * (256 * 1024 // (len(BEGIN_KEY) + 1))
    started = time.monotonic()
    assert redact(text) == text
    assert time.monotonic() - started < 1.5


def test_private_key_redaction_keeps_the_patterns_matches() -> None:
    body = f"{BEGIN_KEY}\nMIIE\n{END_KEY}"
    marker = "[REDACTED:private_key]"
    assert redact(f"a {body} b {body} c") == f"a {marker} b {marker} c"
    # A BEGIN with no END is left alone, whether before or after a whole key.
    assert redact(f"{body} then {BEGIN_KEY} only") == f"{marker} then {BEGIN_KEY} only"
    # Nested BEGINs before one END: redacted from the first BEGIN to the first END.
    assert redact(f"x {BEGIN_KEY} {BEGIN_KEY}\nk\n{END_KEY} y") == f"x {marker} y"
    assert redact(PEM + "\n" + PEM) == f"{marker}\n{marker}"
