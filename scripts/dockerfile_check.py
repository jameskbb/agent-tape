#!/usr/bin/env python3
"""Static checks for Dockerfile, docker-compose.yml, and .dockerignore.

Docker is not required: this verifies what can be verified without building. It is
not a substitute for `docker build` (reported separately by check.sh). Exit 1 on failure.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    problems: list[str] = []
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    lines = [line.strip() for line in dockerfile.splitlines() if line.strip() and not line.strip().startswith("#")]
    froms = [line for line in lines if line.upper().startswith("FROM ")]
    if len(froms) < 2:
        problems.append("Dockerfile must be multi-stage")
    for line in froms:
        image = line.split()[1]
        if ":" not in image or image.endswith(":latest"):
            problems.append(f"base image must be pinned to a tag: {image}")
    final = dockerfile.split("FROM ")[-1]
    user = re.findall(r"^USER\s+(\S+)", final, re.M)
    if not user or user[-1] in {"root", "0"}:
        problems.append("final stage must switch to a non-root USER")
    for needed in ("HEALTHCHECK", "EXPOSE 8787", 'CMD ["agenttape", "serve"]', "AGENTTAPE_HOST=0.0.0.0"):
        if needed not in final:
            problems.append(f"final stage is missing {needed}")
    if "pnpm --dir frontend build" not in dockerfile:
        problems.append("image must build the frontend")
    if re.search(r"^ADD\s+https?://", dockerfile, re.M):
        problems.append("use COPY or curl with a checksum instead of ADD <url>")
    if "apt-get install" in dockerfile and "rm -rf /var/lib/apt/lists" not in dockerfile:
        problems.append("apt lists must be removed in the same layer")
    for match in re.finditer(r"^COPY\s+(?!--from)(?:--\S+\s+)*(.+)$", dockerfile, re.M):
        parts = match.group(1).split()
        for src in parts[:-1]:
            if not (ROOT / src).exists():
                problems.append(f"COPY source does not exist: {src}")

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    if "AGENTTAPE_API_TOKEN_HASH" not in compose:
        problems.append("compose must pass AGENTTAPE_API_TOKEN_HASH (the container binds 0.0.0.0)")
    if not re.search(r"^\s+- \S+:/data\s*$", compose, re.M) or "volumes:" not in compose:
        problems.append("compose must mount a persistent volume at /data")
    if '"127.0.0.1:8787:8787"' not in compose:
        problems.append("compose should publish the port on host loopback by default")

    # Git refuses a bind-mounted repository owned by another uid, and the container cannot
    # write to it, so the build must be able to take the host user's ids.
    for arg in ("AGENTTAPE_UID", "AGENTTAPE_GID"):
        if f"ARG {arg}=" not in final or f"{arg}: ${{{arg}:-" not in compose:
            problems.append(f"Dockerfile and compose must let the build set {arg}")

    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for needed in (".git", ".env", "**/node_modules", "**/.venv"):
        if needed not in ignore:
            problems.append(f".dockerignore must exclude {needed}")

    hadolint = shutil.which("hadolint")
    if hadolint:
        result = subprocess.run([hadolint, str(ROOT / "Dockerfile")], check=False)
        if result.returncode != 0:
            problems.append("hadolint reported problems")

    if problems:
        print("dockerfile check: FAILED")
        for p in problems:
            print("  - " + p)
        return 1
    print(f"dockerfile check: ok ({len(froms)} stages, non-root runtime, token-gated bind"
          f"{', hadolint clean' if hadolint else '; hadolint not installed'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
