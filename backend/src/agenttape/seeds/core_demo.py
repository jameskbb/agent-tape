"""Core demo data: the fixture project, Demo Agent profiles, and prompt templates."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from agenttape.adapters.builtin._demo_repo import ensure_fixture_repo
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import (
    AgentProfileCreate,
    ProjectCreate,
    ProjectSettings,
    PromptTemplateCreate,
)
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.projects import ProjectService
from agenttape.application.services.prompts import PromptService
from agenttape.domain.enums import VariableType
from agenttape.domain.prompts import VariableDefinition
from agenttape.infrastructure.db.models import AgentProfile, Project, PromptTemplate
from agenttape.seeds.base import SeedReport

ORDER = 10

DEMO_PROJECT = "AgentTape Demo Project"
IMPROVE_PROMPT = "Improve the greeting service"
TRIAGE_PROMPT = "Triage a failing check"

EXIT_CODE_GATE: list[dict[str, Any]] = [
    {"type": "exit_code", "name": "Exit code is 0", "required": True, "config": {"expected": [0]}}
]

#: scenario -> (profile name, description, extra config)
DEMO_PROFILES: dict[str, tuple[str, str, dict[str, Any]]] = {
    "success": (
        "Demo Agent",
        "Completes a small, safe change in the fixture repository.",
        {},
    ),
    "fail_then_retry": (
        "Demo Agent (flaky, recovers on retry)",
        "First attempt hits a simulated transient error; the automatic retry succeeds.",
        {"max_attempts": 2, "retry": {"backoff_base_seconds": 1}},
    ),
    "needs_approval": (
        "Demo Agent (needs approval)",
        "Plans the change, pauses for human approval, then resumes from its checkpoint.",
        {},
    ),
    "timeout": (
        "Demo Agent (times out)",
        "Starts a process that never finishes; the 6 second wall-clock limit stops it.",
        {"timeout_seconds": 6},
    ),
    "fail": (
        "Demo Agent (fails checks)",
        "Project checks fail before any change is made (validation_failed, not retried).",
        {"max_attempts": 1},
    ),
}


def profile_name(scenario: str) -> str:
    return DEMO_PROFILES[scenario][0]


def seed(ctx: AppContext, report: SeedReport) -> None:
    repo = ensure_fixture_repo(ctx.settings.data_dir)
    with ctx.session() as session:
        project = session.scalars(select(Project).where(Project.name == DEMO_PROJECT)).first()
        project_id = project.id if project else None
    if project_id is None:
        project_id = (
            ProjectService(ctx)
            .create(
                ProjectCreate(
                    name=DEMO_PROJECT,
                    repo_path=str(repo),
                    description="A tiny repository owned by AgentTape. The Demo Agent clones it per run.",
                    tags=["demo"],
                    settings=ProjectSettings(
                        timeout_seconds=300,
                        protected_paths=[".github/**", "*.lock"],
                        validation_commands=[["python", "-m", "compileall", "-q", "src"]],
                    ),
                ),
                actor="demo seed",
                is_demo=True,
            )
            .id
        )
        report.add("project", DEMO_PROJECT, created=True)
    else:
        report.add("project", DEMO_PROJECT, created=False)
    report.data["project_id"] = project_id

    profiles = AgentProfileService(ctx)
    ids: dict[str, str] = {}
    for scenario, (name, description, extra) in DEMO_PROFILES.items():
        with ctx.session() as session:
            existing = session.scalars(
                select(AgentProfile).where(AgentProfile.name == name)
            ).first()
            existing_id = existing.id if existing else None
        if existing_id:
            ids[scenario] = existing_id
            report.add("agent_profile", name, created=False)
            continue
        config: dict[str, Any] = {"scenario": scenario, "evaluators": EXIT_CODE_GATE, **extra}
        created = profiles.create(
            AgentProfileCreate(
                name=name,
                adapter_type="demo",
                description=description,
                project_id=project_id,
                tags=["demo"],
                config=config,
            ),
            actor="demo seed",
            is_demo=True,
        )
        ids[scenario] = created.id
        report.add("agent_profile", name, created=True)
    report.data["profiles"] = ids

    prompts = PromptService(ctx)
    templates = {
        IMPROVE_PROMPT: PromptTemplateCreate(
            name=IMPROVE_PROMPT,
            description="Ask an agent to make a small, tested improvement to the demo service.",
            tags=["demo", "code-change"],
            owner="AgentTape demo",
            project_id=project_id,
            body=(
                "Improve {{ module }} in the demo project.\n\n"
                "Goal: {{ goal }}\n"
                "Add unit tests: {{ add_tests }}\n\n"
                "Keep the change small and explain what you changed."
            ),
            variables=[
                VariableDefinition(
                    name="module",
                    type=VariableType.ENUM,
                    options=["src/greeter.py", "README.md"],
                    default="src/greeter.py",
                    help="File the agent should focus on",
                ),
                VariableDefinition(
                    name="goal",
                    type=VariableType.MULTILINE,
                    default="Validate the name argument and reject empty names.",
                    help="What should change and why",
                ),
                VariableDefinition(name="add_tests", type=VariableType.BOOLEAN, default=True),
            ],
        ),
        TRIAGE_PROMPT: PromptTemplateCreate(
            name=TRIAGE_PROMPT,
            description="Investigate a failing check. Shows how secret references stay redacted.",
            tags=["demo", "triage"],
            owner="AgentTape demo",
            project_id=project_id,
            body=(
                "The check `{{ check_name }}` is failing.\n"
                "Context from the developer: {{ notes }}\n\n"
                "If you need the CI API, authenticate with {{ ci_token }}."
            ),
            variables=[
                VariableDefinition(
                    name="check_name", type=VariableType.STRING, default="unit-tests"
                ),
                VariableDefinition(name="notes", type=VariableType.MULTILINE, required=False),
                VariableDefinition(
                    name="ci_token",
                    type=VariableType.SECRET,
                    default="DEMO_CI_TOKEN",
                    help="Name of a secret reference; its value is never stored",
                ),
            ],
        ),
    }
    prompt_ids: dict[str, str] = {}
    for name, data in templates.items():
        with ctx.session() as session:
            existing_t = session.scalars(
                select(PromptTemplate).where(PromptTemplate.name == name)
            ).first()
            existing_tid = existing_t.id if existing_t else None
        if existing_tid:
            prompt_ids[name] = existing_tid
            report.add("prompt", name, created=False)
            continue
        prompt_ids[name] = prompts.create_template(data, actor="demo seed", is_demo=True).id
        report.add("prompt", name, created=True)
    report.data["prompts"] = prompt_ids
