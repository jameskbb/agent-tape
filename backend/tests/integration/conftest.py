"""Fixtures shared by the integration tests."""

from __future__ import annotations

import pytest

from agenttape.application.context import AppContext
from tests.integration.capture_helpers import reset_mcp_rate_limiters
from tests.integration.helpers import make_repo_project


@pytest.fixture
def repo_project(ctx: AppContext) -> str:
    """A committed ``findings-repo`` (``src/greeter.py``, ``src/other.py``), registered as a
    project."""
    return make_repo_project(ctx)


@pytest.fixture(autouse=True)
def _reset_mcp_rate_limiters() -> None:
    reset_mcp_rate_limiters()
