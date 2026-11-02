"""Demo workflow data: the "Safe Code Change" workflow, its evaluation suite, and one run
that pauses at the approval node.

The run is created through ``WorkflowService.start_run`` with a fixed idempotency key, so
seeding again never creates a second one. It is created ``queued``: the worker executes
it for real (prepare, the Demo Agent, the parallel baseline check) until it pauses at
"Approve the change" - it is not a hand-written row.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.schemas.evaluation_catalog import (
    EvaluationDefinitionCreate,
    EvaluationSuiteCreate,
    SuiteMember,
)
from agenttape.application.schemas.workflows import WorkflowCreate, WorkflowRunCreate
from agenttape.application.services.evaluation_catalog import EvaluationCatalogService
from agenttape.application.services.workflows import WorkflowService
from agenttape.domain.enums import RunSource
from agenttape.infrastructure.db.models import EvaluationDefinition, EvaluationSuite, Workflow
from agenttape.seeds.base import SeedReport
from agenttape.seeds.core_demo import IMPROVE_PROMPT

ORDER = 40

WORKFLOW_NAME = "Safe Code Change"
SUITE_NAME = "Safe change checks"
RUN_KEY = "demo-seed:safe-code-change:waiting-approval"

#: name -> (evaluator type, config, required, description)
DEFINITIONS: dict[str, tuple[str, dict[str, Any], bool, str]] = {
    "Agent exited cleanly": (
        "exit_code",
        {"expected": [0]},
        True,
        "The agent process exited with code 0.",
    ),
    "Small diff, no protected paths": (
        "git_diff",
        {"max_files": 10, "max_lines": 400},
        True,
        "At most 10 files and 400 changed lines; nothing under the project's protected paths.",
    ),
    "Change report produced": (
        "artifact_produced",
        {"name": "*.md", "min_count": 1},
        True,
        "The agent left a Markdown report of what it changed.",
    ),
    "Finishes within 10 minutes": (
        "duration",
        {"max_seconds": 600, "warn_seconds": 300},
        False,
        "Warns after 5 minutes, fails after 10.",
    ),
    "Token budget": (
        "usage_cost",
        {"max_total_tokens": 50_000},
        False,
        "Skipped as unavailable when usage is not reported (or only simulated).",
    ),
}


def definition(project_id: str, profile_id: str, prompt_id: str, suite_id: str) -> dict[str, Any]:
    return {
        "inputs": [
            {
                "name": "goal",
                "type": "multiline",
                "default": "Validate the name argument and reject empty names.",
                "help": "What the agent should change",
            }
        ],
        "settings": {"max_parallel": 3, "project_id": project_id},
        "nodes": [
            {
                "key": "prepare",
                "type": "command",
                "name": "Record the starting commit",
                "description": "Deterministic preparation: read the repository state.",
                "config": {"argv": ["git", "log", "--oneline", "-1"]},
                "timeout_seconds": 60,
            },
            {
                "key": "implement",
                "type": "agent_run",
                "name": "Implement the change",
                "config": {
                    "agent_profile_id": profile_id,
                    "prompt_template_id": prompt_id,
                },
                "inputs": {
                    "module": "src/greeter.py",
                    "goal": "{{ inputs.goal }}",
                    "add_tests": True,
                },
                "timeout_seconds": 900,
            },
            {
                "key": "baseline",
                "type": "command",
                "name": "List the tracked sources",
                "description": "Read-only; runs in parallel with the agent on the untouched repository.",
                "config": {"argv": ["git", "ls-files", "src", "tests"]},
                "timeout_seconds": 120,
            },
            {
                "key": "approve",
                "type": "approval",
                "name": "Approve the change",
                "config": {
                    "title": "Approve the change: {{ inputs.goal }}",
                    "rationale": "The agent changed {{ nodes.implement.outputs.files_changed }} "
                    "file(s) starting from {{ nodes.prepare.outputs.stdout }}. Approve to "
                    "validate and evaluate the change.",
                    "risk_level": "medium",
                    "requested_action": {
                        "summary": "{{ nodes.implement.outputs.summary }}",
                        "child_run_id": "{{ nodes.implement.outputs.child_run_id }}",
                    },
                },
            },
            {
                "key": "validate",
                "type": "command",
                "name": "Check the agent's diff",
                "description": "Read-only: fails on conflict markers or whitespace errors.",
                "config": {
                    "argv": ["git", "diff", "--check"],
                    "cwd": "{{ nodes.implement.outputs.workdir }}",
                },
                "timeout_seconds": 120,
            },
            {
                "key": "evaluate",
                "type": "evaluation_gate",
                "name": "Evaluate the change",
                "config": {"target": "implement", "suite_id": suite_id},
            },
            {
                "key": "collect",
                "type": "collect",
                "name": "Collect the results",
                "config": {
                    "include": ["prepare", "implement", "approve", "validate", "evaluate"],
                    "artifact_name": "safe-code-change.json",
                },
            },
        ],
        "edges": [
            {"from": "prepare", "to": "implement"},
            {"from": "prepare", "to": "baseline"},
            {"from": "implement", "to": "approve"},
            {"from": "baseline", "to": "approve"},
            {"from": "approve", "to": "validate"},
            {"from": "implement", "to": "validate"},
            {"from": "validate", "to": "evaluate"},
            {"from": "implement", "to": "evaluate"},
            {"from": "evaluate", "to": "collect"},
        ],
    }


def _existing_id(ctx: AppContext, model: Any, name: str) -> str | None:
    with ctx.session() as session:
        row = session.scalars(select(model).where(model.name == name)).first()
        return row.id if row else None


def seed(ctx: AppContext, report: SeedReport) -> None:
    catalog = EvaluationCatalogService(ctx)
    definition_ids: dict[str, str] = {}
    for name, (type_, config, required, description) in DEFINITIONS.items():
        existing = _existing_id(ctx, EvaluationDefinition, name)
        if existing:
            definition_ids[name] = existing
            report.add("evaluation_definition", name, created=False)
            continue
        definition_ids[name] = catalog.create_definition(
            EvaluationDefinitionCreate(
                name=name,
                description=description,
                evaluator_type=type_,
                config=config,
                required=required,
            ),
            actor="demo seed",
            is_demo=True,
        ).id
        report.add("evaluation_definition", name, created=True)

    suite_id = _existing_id(ctx, EvaluationSuite, SUITE_NAME)
    if suite_id is None:
        suite_id = catalog.create_suite(
            EvaluationSuiteCreate(
                name=SUITE_NAME,
                description="Checks a Demo Agent code change must pass before it is kept.",
                members=[SuiteMember(definition_id=i) for i in definition_ids.values()],
            ),
            actor="demo seed",
            is_demo=True,
        ).id
        report.add("evaluation_suite", SUITE_NAME, created=True)
    else:
        report.add("evaluation_suite", SUITE_NAME, created=False)

    workflows = WorkflowService(ctx)
    workflow_id = _existing_id(ctx, Workflow, WORKFLOW_NAME)
    if workflow_id is None:
        workflow_id = workflows.create(
            WorkflowCreate(
                name=WORKFLOW_NAME,
                description=(
                    "Prepare, let the Demo Agent make a change while a baseline check runs in "
                    "parallel, pause for approval, then validate and evaluate the change."
                ),
                tags=["demo", "code-change"],
                project_id=report.data["project_id"],
                definition=definition(
                    report.data["project_id"],
                    report.data["profiles"]["success"],
                    report.data["prompts"][IMPROVE_PROMPT],
                    suite_id,
                ),
            ),
            actor="demo seed",
            is_demo=True,
        ).id
        report.add("workflow", WORKFLOW_NAME, created=True)
    else:
        report.add("workflow", WORKFLOW_NAME, created=False)
    report.data["workflow_id"] = workflow_id

    if not report.include_runs:
        return
    result = workflows.start_run(
        workflow_id,
        WorkflowRunCreate(
            idempotency_key=RUN_KEY,
            title="Safe Code Change: validate names (waiting for approval)",
            tags=["demo"],
        ),
        source=RunSource.DEMO,
        actor="demo seed",
        is_demo=True,
    )
    report.add("workflow_run", result.run.title, created=result.created)
    report.data["workflow_run_id"] = result.run.id
