"""Portable, sanitized run bundles (``agenttape export`` and ``GET /runs/{id}/export``).

The bundle is plain JSON: the run record, immutable snapshot, attempts, every event,
evaluations, approvals, file changes, artifact metadata with the content of text artifacts
up to a size cap, findings (with every citation's verdict, reproduction, and confirmation),
and challenges (each labelled model judgment, never proof). Everything passes through
redaction again on the way out.

``run_bundle_archive`` packs the same bundle into a zip with every referenced
text artifact as its own file. Export-time redaction uses the credential patterns plus
the current values of ``AGENTTAPE_REDACT_ENV`` variables and of every secret the
workspace references, so a value that reached storage by any path is still removed.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import select

from agenttape import __version__
from agenttape.application.context import AppContext
from agenttape.application.services.finding_review import FindingReviewService
from agenttape.application.services.findings import FindingsService
from agenttape.application.services.runs import RunService
from agenttape.infrastructure.db.models import Artifact, SecretReference
from agenttape.security.redaction import Redactor
from agenttape.security.secrets import EnvSecretResolver, SecretLocator, SecretUnavailableError

BUNDLE_FORMAT = "agenttape.run-bundle"
BUNDLE_VERSION = 1
MAX_INLINE_ARTIFACT_BYTES = 256 * 1024
MAX_ARCHIVE_ARTIFACT_BYTES = 5 * 1024 * 1024


def mask_host_paths(value: Any, prefixes: list[tuple[str, str]]) -> Any:
    """Replace host-identifying path prefixes (the data dir, the home directory) in every
    string, so a shared bundle does not reveal user names or directory layout."""
    if isinstance(value, str):
        for prefix, replacement in prefixes:
            if prefix in value:
                value = value.replace(prefix, replacement)
        return value
    if isinstance(value, dict):
        return {k: mask_host_paths(v, prefixes) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_host_paths(v, prefixes) for v in value]
    return value


class ExportService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def run_bundle(self, run_id: str) -> dict[str, Any]:
        runs = RunService(self.ctx)
        detail = runs.get_detail(run_id)
        events: list[dict[str, Any]] = []
        after = 0
        while True:
            page = runs.list_events(run_id, after_sequence=after, limit=1000)
            events.extend(e.model_dump(mode="json") for e in page.items)
            if page.next_after_sequence is None:
                break
            after = page.next_after_sequence
        artifacts: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        with self.ctx.session() as session:
            findings = FindingsService.findings_for_run(session, run_id)
            rows = session.scalars(select(Artifact).where(Artifact.run_id == run_id)).all()
            for row in rows:
                entry: dict[str, Any] = {
                    "id": row.id,
                    "name": row.name,
                    "kind": row.kind,
                    "media_type": row.media_type,
                    "size_bytes": row.size_bytes,
                    "sha256": row.sha256,
                    "created_at": row.created_at.isoformat(),
                }
                if row.is_text and row.size_bytes <= MAX_INLINE_ARTIFACT_BYTES:
                    try:
                        with self.ctx.artifact_store.open(row.storage_path) as fh:
                            entry["content"] = fh.read().decode("utf-8", errors="replace")
                    except OSError:
                        entry["content_error"] = "artifact file is missing from the store"
                elif row.is_text:
                    entry["content_omitted"] = f"larger than {MAX_INLINE_ARTIFACT_BYTES} bytes"
                artifacts.append(entry)
        bundle = {
            "format": BUNDLE_FORMAT,
            "format_version": BUNDLE_VERSION,
            "exported_at": self.ctx.clock.now().isoformat(),
            "agenttape_version": __version__,
            "run": detail.run.model_dump(mode="json"),
            "snapshot": detail.snapshot,
            "inputs": detail.inputs,
            "rendered_prompt": detail.rendered_prompt,
            "outputs": detail.outputs,
            "attempts": [a.model_dump(mode="json") for a in detail.attempts],
            "steps": [s.model_dump(mode="json") for s in detail.steps],
            "events": events,
            "evaluations": [e.model_dump(mode="json") for e in detail.evaluations],
            "approvals": [a.model_dump(mode="json") for a in detail.approvals],
            "file_changes": [c.model_dump(mode="json") for c in detail.file_changes],
            "artifacts": artifacts,
            "findings": findings,
            "challenges": [
                c.model_dump(mode="json") for c in FindingReviewService(self.ctx).challenges(run_id)
            ],
        }
        sanitized: dict[str, Any] = self.export_redactor(bundle).redact_value(bundle)
        masked: dict[str, Any] = mask_host_paths(sanitized, self.host_path_prefixes())
        return masked

    def host_path_prefixes(self) -> list[tuple[str, str]]:
        """Absolute paths that identify this machine, and what replaces them in exports."""
        pairs = [(str(self.ctx.settings.data_dir), "$AGENTTAPE_DATA_DIR")]
        home = str(Path.home())
        if home not in ("", "/"):
            pairs.append((home, "~"))
        return pairs

    def export_redactor(self, bundle: dict[str, Any] | None = None) -> Redactor:
        """Patterns plus every secret value AgentTape can resolve right now."""
        values = [os.environ.get(name, "") for name in self.ctx.settings.redact_env]
        refs: dict[str, SecretLocator] = {}
        with self.ctx.session() as session:
            for ref in session.scalars(select(SecretReference)):
                refs[ref.name] = SecretLocator(provider=ref.provider, locator=ref.locator)
        names = set(refs)
        prompt = ((bundle or {}).get("snapshot") or {}).get("prompt") or {}
        names.update(str(n) for n in prompt.get("secret_refs") or [] if isinstance(n, str))
        resolver = EnvSecretResolver(references=refs)
        for name in sorted(names):
            try:
                values.append(resolver.resolve(name))
            except SecretUnavailableError:
                continue
        return Redactor([v for v in values if v])

    def run_bundle_archive(self, run_id: str) -> bytes:
        """A zip: ``bundle.json`` plus each text artifact under ``artifacts/`` (redacted)."""
        bundle = self.run_bundle(run_id)
        redactor = self.export_redactor(bundle)
        buf = io.BytesIO()
        index: list[dict[str, Any]] = []
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            with self.ctx.session() as session:
                rows = session.scalars(select(Artifact).where(Artifact.run_id == run_id)).all()
                for row in rows:
                    entry: dict[str, Any] = {"id": row.id, "name": row.name, "kind": row.kind}
                    if not row.is_text:
                        entry["omitted"] = "binary artifacts are not exported"
                    elif row.size_bytes > MAX_ARCHIVE_ARTIFACT_BYTES:
                        entry["omitted"] = f"larger than {MAX_ARCHIVE_ARTIFACT_BYTES} bytes"
                    else:
                        try:
                            with self.ctx.artifact_store.open(row.storage_path) as fh:
                                text = fh.read().decode("utf-8", errors="replace")
                        except OSError:
                            entry["omitted"] = "artifact file is missing from the store"
                        else:
                            safe = "".join(
                                c if c.isalnum() or c in "._-" else "_" for c in row.name
                            )[:120]
                            member = f"artifacts/{row.id[:8]}-{safe or 'artifact'}"
                            zf.writestr(
                                member,
                                mask_host_paths(
                                    redactor.redact_text(text), self.host_path_prefixes()
                                ),
                            )
                            entry["path"] = member
                    index.append(entry)
            bundle["archive_artifacts"] = index
            zf.writestr("bundle.json", json.dumps(bundle, indent=2, default=str) + "\n")
            zf.writestr(
                "README.txt",
                "AgentTape run bundle (sanitized). bundle.json holds the run record, snapshot,\n"
                "attempts, events, evaluations, approvals, and file changes; artifacts/ holds\n"
                "the run's text artifacts. Secret values and credential patterns were redacted\n"
                "at export time.\n",
            )
        return buf.getvalue()
