"""Context Pack assembly (policy ``agenttape.default`` v1).

At the start of each attempt AgentTape resolves, deterministically and in a fixed order,
the context it knows about before the agent begins:

1. trigger (source, reference, idempotency key, sanitized input keys)
2. prompt template + immutable version + hash of the redacted rendered prompt
3. prompt variables (secret values never present; credential-shaped values redacted)
4. agent profile: adapter, model, permission, tool, budget, and sandbox settings
5. project instruction files present in the working directory (hashed, no excerpt)
6. GSD planning references (hashed)
7. parent / replay / fork / workflow-node lineage and inputs passed from earlier nodes
8. approved project lessons (the only items AgentTape appends to the prompt)
9. compact repository index (``git ls-files``: count + hash) *before*
10. targeted repository files mentioned in the prompt (hashed references)

plus, as ``available`` (accessible but not placed in the pack): installed skills and MCP
servers declared in ``.mcp.json``. Items considered but left out are recorded under
``excluded`` with a reason.

Stage semantics: ``injected`` means AgentTape itself placed the text verbatim into the
input it handed to the agent (the rendered prompt and the approved-lessons block).
Everything else AgentTape placed in the pack is ``selected``: a reference with a hash,
never a claim that the model read it. The pack is written once per attempt and never
edited; a resumed attempt reuses it.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agenttape.context_evidence.common import (
    INJECTED_DESTINATIONS,
    add_evidence,
    canonical_hash,
    safe_locator,
    sanitize,
    scope_for,
    sha256_text,
    upsert_source,
)
from agenttape.git.snapshot import run_git
from agenttape.infrastructure.db.extra_models.context_evidence import ContextFeedback, ContextPack
from agenttape.infrastructure.db.models import Run
from agenttape.security.redaction import RedactionReport

if TYPE_CHECKING:
    from agenttape.application.context import AppContext
    from agenttape.infrastructure.clock import Clock

log = logging.getLogger(__name__)

POLICY = "agenttape.default"
POLICY_VERSION = 1
INSTRUCTION_FILES = (
    "CLAUDE.md",
    "CLAUDE.local.md",
    ".claude/CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".github/copilot-instructions.md",
    ".cursorrules",
)
PLANNING_FILES = (
    ".planning/PROJECT.md",
    ".planning/ROADMAP.md",
    ".planning/STATE.md",
    ".planning/REQUIREMENTS.md",
)
PROFILE_KEYS = (
    "model",
    "permission_mode",
    "approval_policy",
    "sandbox",
    "allowed_tools",
    "disallowed_tools",
    "max_turns",
    "budget",
    "timeout_seconds",
    "idle_timeout_seconds",
    "prompt_mode",
    "scenario",
)
MAX_TARGETED_FILES = 20
MAX_SKILLS = 50
LESSONS_HEADER = "--- AgentTape: approved project lessons (reviewed by a person) ---"
LESSONS_FOOTER = "--- end of approved project lessons ---"
_PATH_TOKEN = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8})(?![\w/-])")


def _hash_file(path: Path) -> tuple[str | None, int | None]:
    try:
        data = path.read_bytes()
    except OSError:
        return None, None
    return sha256_text(data), len(data)


def _git_index(workdir: Path) -> dict[str, Any] | None:
    if not (workdir / ".git").exists():
        return None
    try:
        # The hardened invoker: repository config the agent can write never runs a program.
        listed = run_git(workdir, "ls-files", "-z", timeout=15)
        if listed.returncode != 0:
            return None
        head = run_git(workdir, "rev-parse", "HEAD", timeout=15).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    names = [n for n in listed.stdout.split("\0") if n]
    return {
        "files": len(names),
        "hash": sha256_text("\n".join(names)),
        "head": head.strip() or None,
    }


class _Builder:
    def __init__(self, workdir: Path | None) -> None:
        self.workdir = workdir
        self.items: list[dict[str, Any]] = []
        self.excluded: list[dict[str, Any]] = []
        self.report = RedactionReport()

    def add(
        self,
        kind: str,
        key: str,
        name: str,
        stage: str,
        reason: str,
        *,
        source_type: str | None = None,
        locator: str | None = None,
        authority: str = "authoritative",
        live: bool | None = None,
        version: str | None = None,
        content_hash: str | None = None,
        chars: int | None = None,
        details: dict[str, Any] | None = None,
        excerpt: str | None = None,
    ) -> None:
        self.items.append(
            {
                "position": len(self.items) + 1,
                "kind": kind,
                "source_key": key,
                "source_type": source_type or kind,
                "name": name,
                "stage": stage,
                "reason": reason,
                "locator": locator,
                "authority": authority,
                "live": live,
                "version": version,
                "content_hash": content_hash,
                "chars": chars,
                "excerpt": excerpt,
                "details": details or {},
            }
        )

    def exclude(self, kind: str, name: str, reason: str, key: str | None = None) -> None:
        self.excluded.append({"kind": kind, "name": name, "reason": reason, "source_key": key})

    def clean(self, value: Any) -> Any:
        return sanitize(value, self.report)


def _prompt_delivered(run: Run) -> tuple[bool, str]:
    """Whether the agent process is handed the rendered prompt (and so the lessons block).

    AgentTape passes the prompt to every adapter; these built-in configurations are known
    not to forward it to the agent they run, so nothing there is recorded ``injected``.
    """
    if not run.rendered_prompt:
        return False, "this run has no prompt"
    config = dict(((run.snapshot or {}).get("agent_profile") or {}).get("config") or {})
    adapter = run.adapter_type
    if adapter == "generic_command" and config.get("prompt_mode") == "none":
        return False, "the agent profile is configured not to receive a prompt"
    if (adapter == "demo" and config.get("scenario") == "full_evidence") or (
        adapter == "herdr" and config.get("agent_kind", "demo") == "demo"
    ):
        return False, "the evidence demo agent runs a scripted task and is not given the prompt"
    return True, ""


def approved_lessons(session: Session, project_id: str | None) -> list[ContextFeedback]:
    if not project_id:
        return []
    return list(
        session.scalars(
            select(ContextFeedback)
            .where(
                ContextFeedback.project_id == project_id,
                ContextFeedback.status == "approved",
                ContextFeedback.lesson_version.is_not(None),
                ContextFeedback.proposed_destination.in_(INJECTED_DESTINATIONS),
            )
            .order_by(ContextFeedback.lesson_version)
        )
    )


def lessons_block(lessons: list[ContextFeedback]) -> str:
    lines = [f"[lesson v{row.lesson_version}] {row.lesson_text or row.text}" for row in lessons]
    return "\n\n" + "\n".join([LESSONS_HEADER, *lines, LESSONS_FOOTER]) + "\n"


def build_items(session: Session, run: Run, workdir: Path | None) -> tuple[_Builder, str]:
    """Resolve the ordered items (pure apart from reading the workdir and the DB)."""
    b = _Builder(workdir)
    snap = dict(run.snapshot or {})

    # 1. trigger ------------------------------------------------------------------
    trigger_ref = run.trigger_id or run.schedule_id or run.source_ref
    b.add(
        "trigger",
        f"trigger:{run.source}:{trigger_ref or 'direct'}",
        f"{run.source.replace('_', ' ').capitalize()} trigger",
        "selected",
        "What started this run",
        authority="authoritative",
        details={
            "source": run.source,
            "source_ref": safe_locator(run.source_ref) if run.source_ref else None,
            "trigger_id": run.trigger_id,
            "schedule_id": run.schedule_id,
            "idempotency_key": run.idempotency_key,
            "input_keys": sorted((run.inputs or {}).keys()),
            "created_by": run.created_by,
        },
    )

    # 2. prompt -------------------------------------------------------------------
    prompt = dict(snap.get("prompt") or {})
    delivered, why_not = _prompt_delivered(run)
    if prompt:
        rendered = run.rendered_prompt or ""
        key = (
            f"prompt:{prompt.get('template_id')}@v{prompt.get('version')}"
            if prompt.get("template_id")
            else f"prompt:inline:{str(prompt.get('content_hash'))[:16]}"
        )
        b.add(
            "prompt",
            key,
            str(prompt.get("name") or "Inline prompt"),
            "injected" if delivered else "selected",
            "Rendered prompt handed to the agent as its input"
            if delivered
            else f"Rendered but not delivered: {why_not}",
            source_type="prompt_template",
            version=f"v{prompt.get('version')}" if prompt.get("version") else "inline",
            content_hash=sha256_text(rendered),
            chars=len(rendered),
            details={
                "template_id": prompt.get("template_id"),
                "version_id": prompt.get("version_id"),
                "template_hash": prompt.get("content_hash"),
                "rendered_prompt_hash": sha256_text(rendered),
                "secret_refs": list(prompt.get("secret_refs") or []),
                "original_request": sanitize(rendered[:2000]) if rendered else None,
            },
        )
    else:
        b.exclude("prompt", "Prompt", "This run has no prompt")

    # 3. variables ----------------------------------------------------------------
    variables = dict(run.inputs or {})
    if variables:
        secret_vars = {
            str(v.get("name"))
            for v in prompt.get("variables") or []
            if isinstance(v, dict) and v.get("type") == "secret"
        }
        masked = {k: ("[secret]" if k in secret_vars else v) for k, v in variables.items()}
        clean = b.clean(masked)
        b.add(
            "variables",
            "variables",
            "Prompt variables",
            "selected",
            "Values rendered into the prompt (secret values are never stored)",
            source_type="prompt_variables",
            authority="user-provided",
            content_hash=canonical_hash(clean),
            details={"values": clean},
        )

    # 4. agent profile ------------------------------------------------------------
    profile = dict(snap.get("agent_profile") or {})
    if profile:
        config = dict(profile.get("config") or {})
        settings = {k: config[k] for k in PROFILE_KEYS if k in config}
        b.add(
            "agent_profile",
            f"profile:{profile.get('profile_id')}@v{profile.get('version')}",
            str(profile.get("name") or "Agent profile"),
            "selected",
            "Adapter, model, permission, tool, and budget settings for this run",
            version=f"v{profile.get('version')}",
            content_hash=str(profile.get("config_hash") or canonical_hash(config)),
            details={
                "adapter_type": profile.get("adapter_type"),
                "settings": b.clean(settings),
                "other_keys": sorted(k for k in config if k not in settings),
                "options": b.clean(
                    {
                        k: v
                        for k, v in (run.options or {}).items()
                        if k in ("timeout_seconds", "idle_timeout_seconds", "retry")
                    }
                ),
                "budget": b.clean(run.budget) if run.budget else None,
            },
        )

    head = str((snap.get("git") or {}).get("head") or "") or None
    wd = workdir if workdir is not None and workdir.is_dir() else None

    # 5. instruction files / 6. planning refs -------------------------------------
    if wd is not None:
        for rel in INSTRUCTION_FILES:
            path = wd / rel
            if path.is_file():
                digest, size = _hash_file(path)
                b.add(
                    "instruction_file",
                    f"file:{rel}",
                    rel,
                    "selected",
                    "Project instruction file present in the working directory; the agent "
                    "CLI loads it itself, so AgentTape records it by hash (no excerpt stored)",
                    locator=rel,
                    version=head,
                    content_hash=digest,
                    chars=size,
                )
        for rel in PLANNING_FILES:
            path = wd / rel
            if path.is_file():
                digest, size = _hash_file(path)
                b.add(
                    "planning_ref",
                    f"file:{rel}",
                    rel,
                    "selected",
                    "GSD planning artifact present in the working directory",
                    locator=rel,
                    authority="reference",
                    content_hash=digest,
                    chars=size,
                )
    else:
        b.exclude("instruction_file", "Instruction files", "No working directory to inspect")

    # 7. lineage / workflow inputs -------------------------------------------------
    if run.parent_run_id:
        b.add(
            "lineage",
            f"run:{run.parent_run_id}",
            f"{(run.parent_relation or 'parent').replace('_', ' ')} of run {run.parent_run_id[:8]}",
            "selected",
            "This run was created from another run",
            source_type="run",
            details={"relation": run.parent_relation, "parent_run_id": run.parent_run_id},
        )
    if run.parent_relation == "workflow_node":
        clean = b.clean(dict(run.inputs or {}))
        b.add(
            "workflow_inputs",
            f"workflow:{run.parent_run_id}:{run.parent_node_key}",
            f"Inputs for workflow node {run.parent_node_key}",
            "selected",
            "Outputs of earlier workflow nodes passed into this node",
            source_type="workflow_output",
            authority="generated",
            content_hash=canonical_hash(clean),
            details={"node_key": run.parent_node_key, "keys": sorted(clean)},
        )

    # 8. approved lessons ---------------------------------------------------------
    lessons = approved_lessons(session, run.project_id)
    injected_text = ""
    if lessons and delivered:
        injected_text = lessons_block(lessons)
    for lesson in lessons:
        key = f"lesson:{lesson.id}@v{lesson.lesson_version}"
        name = f"Approved lesson v{lesson.lesson_version}"
        if not delivered:
            b.exclude("lesson", name, f"Not injected: {why_not}", key)
            continue
        text = lesson.lesson_text or lesson.text
        b.add(
            "lesson",
            key,
            name,
            "injected",
            f"Approved by {lesson.reviewed_by or 'a reviewer'}; appended to the prompt",
            source_type="approved_lesson",
            authority="user-provided",
            version=f"v{lesson.lesson_version}",
            content_hash=sha256_text(text),
            chars=len(text),
            excerpt=text,
            details={
                "feedback_id": lesson.id,
                "origin_run_id": lesson.run_id,
                "destination": lesson.proposed_destination,
                "classification": lesson.classification,
            },
        )

    # 9. compact index, then 10. targeted files -----------------------------------
    if wd is not None:
        index = _git_index(wd)
        if index is not None:
            b.add(
                "repository_index",
                "index:git-ls-files",
                "Repository file index (git ls-files)",
                "selected",
                "Compact index of tracked files, recorded before any targeted file",
                source_type="repository_index",
                version=index["head"],
                content_hash=index["hash"],
                details={"files": index["files"]},
            )
        else:
            b.exclude("repository_index", "Repository file index", "Not a Git working tree")
        seen: set[str] = set()
        for match in _PATH_TOKEN.finditer(run.rendered_prompt or ""):
            token = match.group(1).strip(".")
            if token in seen or "://" in token:
                continue
            seen.add(token)
            candidate = (wd / token).resolve()
            try:
                rel = candidate.relative_to(wd.resolve()).as_posix()
            except ValueError:
                b.exclude("repository_file", token, "Outside the run's working directory")
                continue
            if not candidate.is_file():
                if "/" in token:
                    b.exclude(
                        "repository_file", token, "Mentioned in the prompt but not found", None
                    )
                continue
            if len([i for i in b.items if i["kind"] == "repository_file"]) >= MAX_TARGETED_FILES:
                b.exclude("repository_file", rel, "Targeted-file limit reached", f"file:{rel}")
                continue
            digest, size = _hash_file(candidate)
            b.add(
                "repository_file",
                f"file:{rel}",
                rel,
                "selected",
                "Mentioned in the prompt; recorded as a hashed reference (no copy)",
                locator=rel,
                version=head,
                content_hash=digest,
                chars=size,
            )

        # available, not selected: skills and declared MCP servers
        skills = sorted((wd / ".claude" / "skills").glob("*/SKILL.md"))[:MAX_SKILLS]
        for skill in skills:
            rel = skill.relative_to(wd).as_posix()
            digest, size = _hash_file(skill)
            b.add(
                "skill",
                f"skill:{skill.parent.name}",
                skill.parent.name,
                "available",
                "Installed skill in the working directory; invocation is only known if the "
                "adapter reports it",
                locator=rel,
                content_hash=digest,
                chars=size,
            )
        mcp = wd / ".mcp.json"
        if mcp.is_file():
            try:
                servers = json.loads(mcp.read_text()).get("mcpServers") or {}
            except (OSError, ValueError, AttributeError):
                servers = {}
            for name in sorted(servers)[:MAX_SKILLS]:
                b.add(
                    "mcp_server",
                    f"mcp:{name}",
                    f"MCP server {name}",
                    "available",
                    "Declared in .mcp.json (command and environment are never recorded)",
                    source_type="mcp_server",
                    live=True,
                    locator=f".mcp.json#{name}",
                )
    return b, injected_text


#: Identifiers that differ per run or per review, not per content: left out of the hash so
#: two runs handed the same context get the same pack hash.
_UNHASHED_DETAILS = (
    "source_ref",
    "trigger_id",
    "schedule_id",
    "idempotency_key",
    "created_by",
    "feedback_id",
    "origin_run_id",
)


def _hashed(item: dict[str, Any]) -> dict[str, Any]:
    details = {k: v for k, v in item["details"].items() if k not in _UNHASHED_DETAILS}
    key = f"trigger:{details.get('source')}" if item["kind"] == "trigger" else item["source_key"]
    return {**item, "source_key": key, "details": details}


def assemble_pack(
    session: Session, clock: Clock, run_id: str, attempt_id: str, workdir: Path | None
) -> ContextPack:
    """Create the pack for an attempt, or return the existing one (packs are immutable)."""
    existing = session.scalars(
        select(ContextPack).where(ContextPack.attempt_id == attempt_id)
    ).first()
    if existing is not None:
        return existing
    run = session.get(Run, run_id)
    if run is None:
        raise LookupError(run_id)
    builder, injected_text = build_items(session, run, workdir)
    now = clock.now()
    version = (session.scalar(select(func.count()).where(ContextPack.run_id == run_id)) or 0) + 1
    content_hash = canonical_hash(
        {
            "policy": [POLICY, POLICY_VERSION],
            "items": [_hashed(item) for item in builder.items],
            "excluded": builder.excluded,
            "injected_text": injected_text,
        }
    )
    pack = ContextPack(
        run_id=run_id,
        attempt_id=attempt_id,
        node_key=run.parent_node_key,
        version=version,
        policy=POLICY,
        policy_version=POLICY_VERSION,
        created_at=now,
        content_hash=content_hash,
        token_estimate=None,
        token_method=None,
        items=builder.items,
        excluded=builder.excluded,
        redaction=builder.report.as_metadata(),
        injected_text=injected_text or None,
    )
    session.add(pack)
    session.flush()
    scope = scope_for(run.project_id)
    for item in builder.items:
        source = upsert_source(
            session,
            scope=scope,
            key=item["source_key"],
            source_type=item["source_type"],
            name=item["name"],
            now=now,
            provider="agenttape",
            locator=item["locator"],
            authority=item["authority"],
            live=item["live"],
            version=item["version"],
        )
        add_evidence(
            session,
            run_id=run_id,
            dedupe_key=f"pack:{pack.id}:{item['position']}",
            ts=now,
            stage=item["stage"],
            quality="assembled",
            source=source,
            attempt_id=attempt_id,
            node_key=run.parent_node_key,
            position=item["position"],
            version=item["version"],
            reason=item["reason"],
            details={
                "pack_id": pack.id,
                "pack_version": version,
                "kind": item["kind"],
                "content_hash": item["content_hash"],
            },
        )
    for n, item in enumerate(builder.excluded, start=1):
        add_evidence(
            session,
            run_id=run_id,
            dedupe_key=f"pack:{pack.id}:x{n}",
            ts=now,
            stage="excluded",
            quality="assembled",
            source=None,
            attempt_id=attempt_id,
            node_key=run.parent_node_key,
            position=len(builder.items) + n,
            reason=item["reason"],
            details={"pack_id": pack.id, "kind": item["kind"], "name": item["name"]},
        )
    return pack


def prepare_attempt_context(
    ctx: AppContext, run_id: str, attempt_id: str, workdir: Path | None
) -> str:
    """Worker entry point: assemble (or reuse) the pack; return the text AgentTape must
    append to the prompt (approved lessons), or ``""``. Never fails the attempt."""
    try:
        with ctx.session() as session:
            pack = assemble_pack(session, ctx.clock, run_id, attempt_id, workdir)
            return pack.injected_text or ""
    except Exception:  # pragma: no cover - defensive: context capture never blocks a run
        log.exception("context pack assembly failed for run %s", run_id)
        return ""
