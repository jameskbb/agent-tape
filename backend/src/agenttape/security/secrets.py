"""Secret resolution at the execution boundary.

AgentTape never stores secret values. A ``SecretReference`` row (metadata only) maps a
name to a provider and locator; the resolver reads the value from the process
environment (or a pluggable provider) when an attempt starts, and the value is added to
the attempt's redactor so it cannot be persisted in events or logs.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from agenttape.domain.errors import ValidationError


class SecretResolver(Protocol):
    def resolve(self, name: str) -> str:
        """Return the secret value or raise :class:`SecretUnavailableError`."""
        ...


class SecretUnavailableError(ValidationError):
    code = "secret_unavailable"

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"Secret '{name}' is unavailable: {reason}", details={"secret": name})


@dataclass(frozen=True)
class SecretLocator:
    provider: str
    locator: str


#: Names the env-var fallback never reads: AgentTape's own configuration (database URL,
#: token settings...) is not a secret an agent run may ask for by name. A registered
#: ``SecretReference`` can still point at one explicitly.
_FALLBACK_REFUSED_PREFIXES = ("AGENTTAPE_",)


class EnvSecretResolver:
    """Resolves ``name`` via an optional reference map, defaulting to env var ``name``.

    The fallback (no ``SecretReference`` named ``name``: read the environment variable
    ``name``) is documented behaviour (docs/ADAPTERS.md ``secret_env``, the demo's
    ``DEMO_CI_TOKEN``), so it stays. It is safe because a secret *name* only ever comes
    from trusted configuration: profile ``secret_env``, prompt templates, operator run
    inputs. Webhook payloads and workflow templates cannot choose one (see
    ``TriggerService._validate`` and docs/SECURITY.md T4b).
    """

    def __init__(
        self,
        references: Mapping[str, SecretLocator] | None = None,
        environ: Mapping[str, str] | None = None,
        providers: Mapping[str, Callable[[str], str | None]] | None = None,
    ) -> None:
        self._refs = dict(references or {})
        self._environ = environ if environ is not None else os.environ
        self._providers: dict[str, Callable[[str], str | None]] = {
            "env": lambda loc: self._environ.get(loc),
            **(providers or {}),
        }
        self.resolved: dict[str, str] = {}

    def resolve(self, name: str) -> str:
        if name not in self._refs and name.upper().startswith(_FALLBACK_REFUSED_PREFIXES):
            raise SecretUnavailableError(
                name, "AgentTape's own settings are only resolved through a secret reference"
            )
        loc = self._refs.get(name, SecretLocator(provider="env", locator=name))
        provider = self._providers.get(loc.provider)
        if provider is None:
            raise SecretUnavailableError(name, f"unknown provider '{loc.provider}'")
        value = provider(loc.locator)
        if not value:
            raise SecretUnavailableError(name, f"{loc.provider} value '{loc.locator}' is not set")
        self.resolved[name] = value
        return value
