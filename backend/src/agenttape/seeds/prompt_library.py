"""Prompt library demo prompts: every variable type, validation rules, and a version history.

The core seed already creates the Demo Agent profiles (success, flaky-then-retry,
approval, timeout, fails-checks) and two prompts; this adds prompts that exercise
number/path/enum/boolean variables with validation, plus a second version so the
history and diff views have something to show.
"""

from __future__ import annotations

from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import PromptTemplateCreate, PromptVersionCreate
from agenttape.application.services.prompts import PromptService
from agenttape.domain.enums import VariableType
from agenttape.domain.prompts import VariableDefinition, VariableValidation
from agenttape.infrastructure.db.models import PromptTemplate
from agenttape.seeds.base import SeedReport

ORDER = 20

RELEASE_NOTES = "Draft release notes"
REVIEW_FILE = "Review a file for risks"

_RELEASE_VARS = [
    VariableDefinition(
        name="version",
        type=VariableType.STRING,
        label="Version",
        default="1.2.0",
        help="Semantic version being released",
        validation=VariableValidation(pattern=r"v?\d+\.\d+\.\d+"),
    ),
    VariableDefinition(
        name="audience",
        type=VariableType.ENUM,
        options=["developers", "operators", "end users"],
        default="developers",
        help="Who will read the notes",
    ),
    VariableDefinition(
        name="max_bullets",
        type=VariableType.NUMBER,
        default=6,
        help="Upper bound on bullet points",
        validation=VariableValidation(minimum=1, maximum=20),
    ),
    VariableDefinition(name="include_breaking", type=VariableType.BOOLEAN, default=True),
]

RELEASE_V1 = (
    "Write release notes for version {{ version }} of the demo project for {{ audience }}.\n"
    "Use at most {{ max_bullets }} bullet points.\n"
    "Call out breaking changes: {{ include_breaking }}."
)
RELEASE_V2 = (
    "Write release notes for version {{ version }} of the demo project.\n"
    "Audience: {{ audience }}. Keep the tone plain and specific.\n\n"
    "Use at most {{ max_bullets }} bullet points, grouped as Added / Changed / Fixed.\n"
    "Call out breaking changes: {{ include_breaking }}.\n"
    "Extra context from the maintainer: {{ context }}"
)


def seed(ctx: AppContext, report: SeedReport) -> None:
    service = PromptService(ctx)
    project_id = report.data.get("project_id")
    prompt_ids: dict[str, str] = dict(report.data.get("prompts") or {})

    def existing(name: str) -> str | None:
        with ctx.session() as session:
            row = session.scalars(select(PromptTemplate).where(PromptTemplate.name == name)).first()
            return row.id if row else None

    tid = existing(RELEASE_NOTES)
    if tid is None:
        created = service.create_template(
            PromptTemplateCreate(
                name=RELEASE_NOTES,
                description="Summarize recent changes for a chosen audience.",
                tags=["demo", "writing"],
                owner="AgentTape demo",
                project_id=project_id,
                body=RELEASE_V1,
                variables=_RELEASE_VARS,
                notes="First draft",
            ),
            actor="demo seed",
            is_demo=True,
        )
        service.create_version(
            created.id,
            PromptVersionCreate(
                body=RELEASE_V2,
                variables=[
                    *_RELEASE_VARS,
                    VariableDefinition(
                        name="context",
                        type=VariableType.MULTILINE,
                        required=False,
                        help="Anything the agent should know (optional)",
                    ),
                ],
                notes="Group bullets and accept optional context",
            ),
            actor="demo seed",
        )
        tid = created.id
        report.add("prompt", RELEASE_NOTES, created=True)
    else:
        report.add("prompt", RELEASE_NOTES, created=False)
    prompt_ids[RELEASE_NOTES] = tid

    tid = existing(REVIEW_FILE)
    if tid is None:
        tid = service.create_template(
            PromptTemplateCreate(
                name=REVIEW_FILE,
                description="Ask for a focused review of one file in the project.",
                tags=["demo", "review"],
                owner="AgentTape demo",
                project_id=project_id,
                body=(
                    "Review {{ target_path }} with a focus on {{ focus }}.\n"
                    "Report at most {{ findings }} findings, most severe first."
                ),
                variables=[
                    VariableDefinition(
                        name="target_path",
                        type=VariableType.PATH,
                        default="src/greeter.py",
                        help="Path relative to the project root",
                    ),
                    VariableDefinition(
                        name="focus",
                        type=VariableType.ENUM,
                        options=["correctness", "security", "readability"],
                        default="correctness",
                    ),
                    VariableDefinition(
                        name="findings",
                        type=VariableType.NUMBER,
                        default=5,
                        validation=VariableValidation(minimum=1, maximum=25),
                    ),
                ],
            ),
            actor="demo seed",
            is_demo=True,
        ).id
        report.add("prompt", REVIEW_FILE, created=True)
    else:
        report.add("prompt", REVIEW_FILE, created=False)
    prompt_ids[REVIEW_FILE] = tid
    report.data["prompts"] = prompt_ids
