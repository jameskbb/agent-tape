"""API and webhook tokens. Only hashes are stored; comparison is constant-time.

Tokens are 256-bit random values, so a single SHA-256 is an appropriate at-rest hash
(no password-style stretching is needed for high-entropy secrets). Stored format:
``sha256:<hex>``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

TOKEN_PREFIX = "atk_"  # noqa: S105 - public token prefix
_HASH_PREFIX = "sha256:"


def generate_token(prefix: str = TOKEN_PREFIX) -> str:
    return prefix + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return _HASH_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_display_prefix(token: str, length: int = 8) -> str:
    """A short non-secret prefix shown in the UI to identify a token."""
    return token[: len(TOKEN_PREFIX) + length]


def verify_token(token: str | None, stored_hash: str | None) -> bool:
    if not token or not stored_hash:
        return False
    expected = stored_hash.strip()
    if not expected.startswith(_HASH_PREFIX):
        # Accept a bare hex digest for convenience in AGENTTAPE_API_TOKEN_HASH.
        expected = _HASH_PREFIX + expected
    return hmac.compare_digest(hash_token(token), expected)
