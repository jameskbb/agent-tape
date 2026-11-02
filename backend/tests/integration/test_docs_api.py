"""docs/API.md lists every endpoint the app serves (and nothing that no longer exists)."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

API_DOC = Path(__file__).resolve().parents[3] / "docs" / "API.md"


def test_api_doc_matches_openapi(client: TestClient) -> None:
    schema = client.get("/api/openapi.json").json()
    served = {
        (method.upper(), path.removeprefix("/api/v1"))
        for path, ops in schema["paths"].items()
        for method in ops
    }
    documented = set(
        re.findall(r"^\| (GET|POST|PUT|PATCH|DELETE) \| `([^`]+)` \|", API_DOC.read_text(), re.M)
    )
    assert served - documented == set(), f"undocumented endpoints: {sorted(served - documented)}"
    assert documented - served == set(), f"documented but not served: {sorted(documented - served)}"
