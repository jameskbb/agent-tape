"""Context for validating agent profiles against local policy before they are saved.

``AgentAdapter.validate_profile(config)`` checks the shape of a config. Process adapters
also need local policy to answer "may this actually run here?": the executable
allowlist (in the data directory), the allowlisted project roots, and the secret
references that exist. The profile service builds a :class:`ProfilePolicy` and calls
``validate_with_policy`` on adapters that implement it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agenttape.adapters.base import ProfileIssue


@dataclass(frozen=True)
class ProfilePolicy:
    data_dir: Path
    allowed_roots: list[Path] = field(default_factory=list)
    #: Names of SecretReference rows in the workspace (values are never loaded).
    secret_names: frozenset[str] = frozenset()


@runtime_checkable
class PolicyAwareAdapter(Protocol):
    def validate_with_policy(
        self, config: dict[str, Any], policy: ProfilePolicy
    ) -> list[ProfileIssue]: ...
