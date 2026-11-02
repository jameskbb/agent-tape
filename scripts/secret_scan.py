#!/usr/bin/env python3
"""Scan files that git tracks (or would track) for credential-shaped strings.

Uses gitleaks when it is installed; otherwise this built-in scanner, and says which one
ran. The built-in scanner reads every text file (large files included, line by line) and
looks for provider key formats, private keys, JWTs, bearer tokens, `.npmrc` auth tokens,
and quoted or unquoted credential assignments (`API_KEY=...`, `token: "..."`) whose value
looks like a real secret (long, mixed character classes, high entropy, not a placeholder
or a `$VAR` reference).

A line can opt out with the allow marker (``ALLOW_MARKER`` below; use sparingly, never for a
real secret); every use is listed in the summary so reviewers can see them. Tests build
credential-shaped fixtures by concatenation so they never match.

Exit code 1 when findings exist.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOW_MARKER = "secret-scan" + ": allow"  # split so this file does not opt itself out
#: Lines longer than this are scanned in the first MAX_LINE characters only.
MAX_LINE = 20_000

CREDENTIAL_KEY = (
    r"(?:password|passwd|pwd|secret|client[_-]?secret|api[_-]?key|apikey|access[_-]?key|"
    r"secret[_-]?key|private[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|"
    r"bearer[_-]?token|token)"
)

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9]{32,}")),
    ("Slack token", re.compile(r"\bxox[abposr]-[0-9A-Za-z-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Stripe live key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{16,}")),
]

#: Patterns whose captured value must also look like a real secret (see ``looks_secret``).
VALUE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("bearer token", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/\-]{20,}=*)")),
    ("npmrc auth token", re.compile(r"(?i)_auth(?:Token)?\s*=\s*([^\s\"'$]{16,})")),
    (
        "quoted credential assignment",
        re.compile(rf"""(?i)\b\w*{CREDENTIAL_KEY}\w*\b["']?\s*[:=]\s*["']([^"'\s]{{12,}})["']"""),
    ),
    (
        "unquoted credential assignment",
        re.compile(rf"""(?i)\b\w*{CREDENTIAL_KEY}\w*\s*=\s*([^\s"'`,;)\]}}]{{16,}})"""),
    ),
]

REFERENCE_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
PLACEHOLDER = re.compile(
    r"(?i)(^\$|\$\{|\{\{|<|%s|example|placeholder|changeme|change-me|your[_-]|xxx|\.\.\.|"
    r"redacted|dummy|fake|sample|test[_-]?token|not[_-]?a[_-]?secret|^\*+$|os\.environ|"
    r"getenv|process\.env|secrets\.|settings\.|ctx\.|self\.|config\.|request\.|\(|\[)"
)


def entropy(value: str) -> float:
    counts = Counter(value)
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def looks_secret(value: str) -> bool:
    """Real secrets are long, mix character classes, and have high entropy."""
    value = value.strip().rstrip(".")
    if len(value) < 12 or REFERENCE_NAME.match(value) or PLACEHOLDER.search(value):
        return False
    if re.fullmatch(r"[A-Za-z_]+(?:\.[A-Za-z_]+)*", value):  # identifiers, dotted keys
        return False
    # Human-readable phrases ("my-own-webhook-token-123"): mostly dictionary-like words.
    parts = re.split(r"[-_.]", value)
    if len(parts) >= 3 and all(re.fullmatch(r"[a-z]{1,12}|[0-9]{1,8}", p) for p in parts):
        return False
    classes = sum(
        bool(re.search(p, value)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]")
    )
    return classes >= 2 and entropy(value) >= 3.3


def candidate_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    ).stdout.decode()
    return [ROOT / p for p in out.split("\0") if p]


def scan_line(line: str) -> list[str]:
    line = line[:MAX_LINE]
    hits = [label for label, pattern in PATTERNS if pattern.search(line)]
    for label, pattern in VALUE_PATTERNS:
        for match in pattern.finditer(line):
            if looks_secret(match.group(1)):
                hits.append(label)
                break
    return hits


def scan() -> tuple[list[str], list[str], int]:
    findings: list[str] = []
    allowed: list[str] = []
    scanned = 0
    for path in candidate_files():
        if not path.is_file():
            continue
        with path.open("rb") as fh:
            if b"\0" in fh.read(4096):
                continue
        scanned += 1
        rel = path.relative_to(ROOT)
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                if ALLOW_MARKER in line:
                    allowed.append(f"{rel}:{lineno}")
                    continue
                for label in scan_line(line):
                    findings.append(f"{rel}:{lineno}: possible {label}")
    return findings, allowed, scanned


def main() -> int:
    gitleaks = shutil.which("gitleaks")
    if gitleaks:
        print("secret scan: using gitleaks")
        return subprocess.run(
            [gitleaks, "detect", "--no-banner", "--redact", "--source", str(ROOT)]
        ).returncode
    findings, allowed, scanned = scan()
    if allowed:
        print(f"secret scan: {len(allowed)} line(s) opted out with '{ALLOW_MARKER}':")
        for a in allowed:
            print("  " + a)
    if findings:
        print("secret scan: FOUND possible credentials:")
        for f in findings:
            print("  " + f)
        return 1
    print(
        f"secret scan: clean ({scanned} text files checked with the built-in scanner; "
        "gitleaks is not installed, so only the built-in patterns ran)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
