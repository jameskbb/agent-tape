from __future__ import annotations

import os
from pathlib import Path

import pytest

from agenttape.config import Settings, is_loopback_host
from agenttape.domain.errors import PolicyViolationError, ValidationError
from agenttape.security.capture import is_attempt_id
from agenttape.security.executables import REPLAY_PATH, resolve_replay_program
from agenttape.security.paths import canonicalize, ensure_allowed, safe_join
from agenttape.security.ratelimit import RateLimiter
from agenttape.security.secrets import EnvSecretResolver, SecretLocator, SecretUnavailableError
from agenttape.security.tokens import generate_token, hash_token, verify_token


class TestPaths:
    def test_canonicalize_requires_absolute(self) -> None:
        with pytest.raises(ValidationError):
            canonicalize("relative/path")
        with pytest.raises(ValidationError):
            canonicalize("/tmp/\x00bad")

    def test_allowed_inside_root(self, tmp_path: Path) -> None:
        repo = tmp_path / "root" / "repo"
        repo.mkdir(parents=True)
        assert ensure_allowed(str(repo), [tmp_path / "root"]) == repo.resolve()

    def test_rejects_outside_and_dotdot(self, tmp_path: Path) -> None:
        (tmp_path / "root").mkdir()
        (tmp_path / "other").mkdir()
        with pytest.raises(PolicyViolationError):
            ensure_allowed(str(tmp_path / "other"), [tmp_path / "root"])
        with pytest.raises(PolicyViolationError):
            ensure_allowed(str(tmp_path / "root" / ".." / "other"), [tmp_path / "root"])

    def test_rejects_symlink_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "link").symlink_to(outside)
        with pytest.raises(PolicyViolationError):
            ensure_allowed(str(root / "link"), [root])

    def test_filesystem_root_is_never_a_root(self, tmp_path: Path) -> None:
        with pytest.raises(PolicyViolationError, match="No project roots"):
            ensure_allowed(str(tmp_path), ["/"])

    def test_safe_join(self, tmp_path: Path) -> None:
        assert safe_join(tmp_path, "a/b.txt") == (tmp_path / "a" / "b.txt").resolve()
        for bad in ("../x", "/etc/passwd", "a/../../x", ""):
            with pytest.raises(PolicyViolationError):
                safe_join(tmp_path, bad)

    def test_safe_join_rejects_symlink_escape(self, tmp_path: Path) -> None:
        base = tmp_path / "base"
        base.mkdir()
        (base / "evil").symlink_to(tmp_path)
        with pytest.raises(PolicyViolationError):
            safe_join(base, "evil/file")


class TestTokens:
    def test_roundtrip(self) -> None:
        token = generate_token()
        stored = hash_token(token)
        assert token not in stored
        assert verify_token(token, stored)
        assert verify_token(token, stored.removeprefix("sha256:"))
        assert not verify_token(token + "x", stored)
        assert not verify_token(None, stored)
        assert not verify_token(token, None)


class TestRateLimiter:
    def test_bucket_refills(self) -> None:
        now = [0.0]
        limiter = RateLimiter(rate=2, per_seconds=10, clock=lambda: now[0])
        assert limiter.allow("a") and limiter.allow("a")
        assert not limiter.allow("a")
        assert limiter.allow("b")
        assert limiter.retry_after("a") == pytest.approx(5.0)
        now[0] = 5.0
        assert limiter.allow("a")

    def test_reset_clears_every_bucket(self) -> None:
        """A public reset() lets a test start a key's bucket over instead of
        reaching into the limiter's private internals."""
        limiter = RateLimiter(rate=1, per_seconds=60)
        assert limiter.allow("a")
        assert not limiter.allow("a")
        limiter.reset()
        assert limiter.allow("a")


class TestSecrets:
    def test_env_resolution_and_references(self) -> None:
        env = {"REAL_NAME": "value-1", "DIRECT": "value-2"}
        resolver = EnvSecretResolver({"ALIAS": SecretLocator("env", "REAL_NAME")}, environ=env)
        assert resolver.resolve("ALIAS") == "value-1"
        assert resolver.resolve("DIRECT") == "value-2"
        with pytest.raises(SecretUnavailableError):
            resolver.resolve("MISSING")


class TestSettings:
    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
    def test_loopback(self, host: str) -> None:
        assert is_loopback_host(host)

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "::"])
    def test_not_loopback(self, host: str) -> None:
        assert not is_loopback_host(host)

    def test_token_required_beyond_loopback(self, tmp_path: Path) -> None:
        assert not Settings(data_dir=tmp_path).token_required
        assert Settings(data_dir=tmp_path, host="0.0.0.0").token_required
        assert Settings(data_dir=tmp_path, require_token=True).token_required

    def test_allowed_roots_parsing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "AGENTTAPE_ALLOWED_ROOTS", f"{tmp_path}/a{os.pathsep}{tmp_path}/b,{tmp_path}/c"
        )
        s = Settings(data_dir=tmp_path)
        assert [p.name for p in s.allowed_roots] == ["a", "b", "c"]

    def test_default_database_in_data_dir(self, tmp_path: Path) -> None:
        s = Settings(data_dir=tmp_path)
        assert s.resolved_database_url == f"sqlite:///{tmp_path.resolve()}/agenttape.db"


def test_a_replay_program_resolves_only_in_the_trusted_system_directories() -> None:
    """A bare program name is resolved against REPLAY_PATH, never the inherited
    PATH, and to an absolute path the worker then executes."""
    grep = resolve_replay_program("grep")
    assert grep is not None
    assert Path(grep).is_absolute()
    assert Path(grep).parent in {Path(d) for d in REPLAY_PATH.split(os.pathsep)}
    assert resolve_replay_program("agenttape-no-such-program") is None


def test_a_replay_program_in_a_user_writable_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A program the worker's own user could have written (a planted
    ``~/.local/bin/grep``) is refused even when it sits first on PATH, and even when a
    search directory itself is one the user owns."""
    planted = tmp_path / "bin"
    planted.mkdir()
    fake = planted / "grep"
    fake.write_text("#!/bin/sh\necho planted\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{planted}{os.pathsep}{os.environ.get('PATH', '')}")

    grep = resolve_replay_program("grep")
    assert grep is not None and Path(grep).parent != planted
    if os.geteuid() != 0:
        assert resolve_replay_program("grep", search_path=str(planted)) is None
    fake.chmod(0o777)
    assert resolve_replay_program("grep", search_path=str(planted)) is None


@pytest.mark.parametrize(
    "value",
    [
        "{12345678-1234-4234-8234-123456789abc}",
        "urn:uuid:12345678-1234-4234-8234-123456789abc",
        "12345678123442348234123456789abc",
        "12345678-1234-4234-8234-123456789ABC",
        "",
    ],
)
def test_is_attempt_id_refuses_non_canonical_uuid_forms(value: str) -> None:
    """Only the canonical lowercase hyphenated form ``new_id`` produces is an
    attempt id; ``uuid.UUID`` alone also accepts braces, a urn prefix, and bare hex."""
    assert is_attempt_id(value) is False


def test_is_attempt_id_accepts_a_canonical_uuid() -> None:
    assert is_attempt_id("12345678-1234-4234-8234-123456789abc") is True
